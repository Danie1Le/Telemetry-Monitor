"""Measures the numbers in RESULTS.md on the kind cluster from
scripts/kind-up.sh. Every command writes its raw output to results/<UTC date>/.

  scale [--steps 5,10,20,40] [--step-seconds 120]
      k6 ramp (load/ramp.js) of batches/s: p95, failures and dropped batches per step, with
      the queue depth and processor replicas sampled throughout, and KEDA scale-up and
      scale-down times.
  sustained --rate R [--duration 600]
      k6 at a constant rate (load/sustained.js), sampled the same way. The rate is sustained
      if k6 passes (no non-2xx, no rejected reading, no dropped batch) and the queue depth is
      flat: its last-minute average is at most its first-minute average + FLAT_TOLERANCE.
      DynamoDB Local never throttles, so throttling is checked on AWS only.
  detection [--runs 10] [scenario ...]
      flies each fault scenario N times through e2e.run_scenario(): median and max detection
      time from the fault's start to the alert's openedAt, and to when GET /alerts showed it.
  chaos [--duration 60] [--rate 10]
      sustained load in which every engine's chamber temperature spikes once, while a random
      processor pod is force-deleted every KILL_EVERY seconds and the queue's visibility
      timeout is cut to CHAOS_VISIBILITY seconds (restored afterwards). Expected: one
      `readings` item per sensor for each distinct (engine, second) batch that ingest
      accepted (202, nothing rejected), and exactly the spike scenario's alerts on each
      engine that had an accepted spiked batch. Stored items and alerts must equal these,
      and every batch must have been accepted, or the expected counts are unsure. Missing
      and extra readings and alerts are reported separately.

Queue depth and replicas are sampled every ~3-4 s (SAMPLE_SECONDS plus the two kubectl
calls), so scale times are +/- ~3-4 s.

k6 runs in Docker (grafana/k6, nothing installed on the host) on kind's Docker network and
posts to the Gateway's NodePort, so the same command works on Windows, macOS and Linux.

Run from the repo root: uv run --project simulator scripts/measure.py <command> ...
"""

import argparse
import gzip
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from e2e import EXPECTED, kubectl, print_table, run_scenario

from simulator.contracts import load_sensors
from simulator.flight import start_flight

ROOT = Path(__file__).resolve().parents[1]
K6 = "grafana/k6:2.3.0"
GATEWAY = "http://telemetry-control-plane:30080"
SAMPLE_SECONDS = 2
SETTLE_SECONDS = 600
FLAT_TOLERANCE = 10  # messages
KILL_EVERY = 10
CHAOS_VISIBILITY = 5
# Past every rule's grace after the flight's first second, so the spike can open alerts.
SPIKE_AT = 20
FAULTS = [scenario for scenario, alerts in EXPECTED.items() if alerts]

# Python run in a processor pod, which has boto3 and the queue and table names in its env.
DEPTHS = """
sqs = boto3.client('sqs')
for queue in ('QUEUE_URL', 'DLQ_URL'):
    a = sqs.get_queue_attributes(QueueUrl=os.environ[queue], AttributeNames=['All'])
    a = a['Attributes']
    print(int(a['ApproximateNumberOfMessages']) + int(a['ApproximateNumberOfMessagesNotVisible']))
"""
VISIBILITY = """
sqs, url = boto3.client('sqs'), os.environ['QUEUE_URL']
if len(sys.argv) > 1:
    sqs.set_queue_attributes(QueueUrl=url, Attributes={'VisibilityTimeout': sys.argv[1]})
a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=['VisibilityTimeout'])
print(a['Attributes']['VisibilityTimeout'])
"""
STORED = """
from boto3.dynamodb.conditions import Attr, Key
ddb, flight = boto3.resource('dynamodb'), sys.argv[1]
count, page = 0, {'FilterExpression': Attr('flightId').eq(flight), 'Select': 'COUNT'}
while True:
    response = ddb.Table(os.environ['READINGS_TABLE']).scan(**page)
    count += response['Count']
    if 'LastEvaluatedKey' not in response:
        break
    page['ExclusiveStartKey'] = response['LastEvaluatedKey']
# shortcut: one query page (1 MB) of alerts, far more than one flight raises.
alerts = ddb.Table(os.environ['ALERTS_TABLE']).query(
    KeyConditionExpression=Key('flightId').eq(flight))['Items']
print(json.dumps({'readings': count, 'alerts': sorted([a['sensorId'], a['rule']] for a in alerts)}))
"""


def in_processor(code: str, *args: str) -> str:
    for attempt in range(10):
        try:
            return kubectl(
                "exec",
                "deploy/processor",
                "--",
                "python",
                "-c",
                "import json, os, sys, boto3\n" + code,
                *args,
            )
        except subprocess.CalledProcessError:
            if attempt == 9:
                raise
            time.sleep(3)  # during chaos the pod may be gone, or not started yet


def new_prefix(name: str) -> Path:
    now = datetime.now(UTC)
    directory = ROOT / "results" / f"{now:%Y-%m-%d}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{name}-{now:%H%M%S}"


def write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))
    print(f"wrote {path.relative_to(ROOT)}", flush=True)


def observe() -> dict:
    hpa, deploy = json.loads(
        kubectl("get", "hpa/keda-hpa-processor", "deploy/processor", "-o", "json")
    )["items"]
    queue, dlq = map(int, in_processor(DEPTHS).split())
    return {
        "t": time.time(),
        "queue": queue,
        "dlq": dlq,
        "desired": hpa["status"].get("desiredReplicas"),
        "ready": deploy["status"].get("readyReplicas", 0),
    }


def idle(s: dict) -> bool:
    return (s["queue"], s["desired"], s["ready"]) == (0, 1, 1)


def sample(stop: threading.Event, timeline: list) -> None:
    """One sample per ~3-4 s: SAMPLE_SECONDS plus the two kubectl calls."""
    while not stop.wait(SAMPLE_SECONDS):
        try:
            timeline.append(observe())
        except (
            subprocess.CalledProcessError
        ) as e:  # a missed sample only lowers resolution
            print(f"  sample failed: {e.stderr}", flush=True)


def load(script: str, name: str, env: dict, raw=False, during=lambda k6: None):
    """Waits for an empty queue and one processor replica, runs k6 on load/<script> while
    sampling, then samples on until that state is back. Returns the output prefix and the
    run's record."""
    prefix = new_prefix(name)
    env = {
        "FLIGHT_ID": start_flight(int(time.time()), random.Random()).flight_id,
        **{k: str(v) for k, v in env.items()},
    }
    # As the caller's user on Linux, so results/ stays writable; Docker Desktop maps it.
    user = [f"--user={os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    cmd = [
        "docker",
        "run",
        "--rm",
        f"--name={prefix.name}",
        "--network=kind",
        *user,
        "-v",
        f"{ROOT / 'load'}:/load:ro",
        "-v",
        f"{prefix.parent}:/out",
        "-e",
        f"BASE_URL={GATEWAY}",
        "-e",
        f"SENSORS={json.dumps(load_sensors())}",
        *(arg for k, v in env.items() for arg in ("-e", f"{k}={v}")),
        K6,
        "run",
        f"--summary-export=/out/{prefix.name}-k6-summary.json",
        *([f"--out=json=/out/{prefix.name}-k6.json.gz"] if raw else []),
        f"/load/{script}",
    ]
    deadline = time.time() + SETTLE_SECONDS
    while not idle(state := observe()):
        if time.time() > deadline:
            raise SystemExit(
                f"not idle after {SETTLE_SECONDS}s, not starting k6: {state}"
            )
        print(f"waiting for empty queue and one replica: {state}", flush=True)
        time.sleep(SAMPLE_SECONDS)
    timeline, stop = [], threading.Event()
    sampler = threading.Thread(target=sample, args=(stop, timeline))
    sampler.start()
    start = time.time()
    k6 = None
    print(f"{name}: k6 {script} {env}", flush=True)
    try:
        with open(f"{prefix}-k6.txt", "w") as log:
            k6 = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            during(k6)
            k6.wait()
        end = time.time()
        while time.time() < end + SETTLE_SECONDS:
            last = timeline[-1] if timeline else {"t": 0}
            if last["t"] > end and idle(last):
                break
            time.sleep(SAMPLE_SECONDS)
    finally:
        if k6 and k6.poll() is None:  # an exception escaped: never leave k6 running
            subprocess.run(
                ["docker", "kill", prefix.name], check=False, capture_output=True
            )
            k6.wait()
        stop.set()
        sampler.join()
    record = {
        "script": script,
        "env": env,
        "k6": K6,
        "k6Exit": k6.returncode,
        "start": start,
        "loadEnd": end,
        "timeline": timeline,
    }
    return prefix, record


def scaling(record: dict) -> dict:
    """Seconds from the queue first passing KEDA's target per replica to a second replica
    Ready (scale-up), and from the queue draining after the load to one replica (down).
    Resolution is the sampling cadence, ~3-4 s."""
    target = int(
        kubectl(
            "get",
            "scaledobject/processor",
            "-o",
            "jsonpath={.spec.triggers[0].metadata.queueLength}",
        )
    )
    timeline = record["timeline"]

    def first(after, test):
        return next((s["t"] for s in timeline if s["t"] >= after and test(s)), None)

    over = first(0, lambda s: s["queue"] > target)
    up = over and first(over, lambda s: s["ready"] > 1)
    drained = first(record["loadEnd"], lambda s: s["queue"] == 0)
    down = up and drained and first(drained, lambda s: s["ready"] == 1)
    return {
        "queueTarget": target,
        "maxReady": max((s["ready"] for s in timeline), default=0),
        "scaleUpSeconds": up and round(up - over, 1),
        "scaleDownSeconds": down and round(down - drained, 1),
    }


def steps(prefix: Path) -> list[dict]:
    """Per ramp step from the k6 summary: rate, p95 (ms), failed share, dropped batches."""
    metrics = json.loads(Path(f"{prefix}-k6-summary.json").read_text())["metrics"]
    rows = []
    for name, m in metrics.items():
        if name.startswith("http_req_duration{scenario:rate_"):
            step = name[len("http_req_duration") :]
            rows.append(
                {
                    "batchesPerSecond": int(step[len("{scenario:rate_") : -1]),
                    "p95Ms": round(m["p(95)"], 1),
                    "failed": metrics[f"http_req_failed{step}"]["value"],
                    "dropped": metrics[f"dropped_iterations{step}"]["count"],
                }
            )
    return sorted(rows, key=lambda r: r["batchesPerSecond"])


def scale(step_list: str, step_seconds: int) -> int:
    env = {"STEPS": step_list, "STEP_SECONDS": step_seconds}
    prefix, record = load("ramp.js", "scale", env)
    record |= {"steps": steps(prefix), "scaling": scaling(record)}
    write(Path(f"{prefix}.json"), record)
    for row in record["steps"]:
        print(row)
    print(record["scaling"])
    return record["k6Exit"]


def sustained(rate: int, duration: int) -> int:
    prefix, record = load(
        "sustained.js", "sustained", {"RATE": rate, "DURATION": f"{duration}s"}
    )
    timeline = [s for s in record["timeline"] if s["t"] <= record["loadEnd"]]
    queue = [s["queue"] for s in timeline]
    first = [s["queue"] for s in timeline if s["t"] < record["start"] + 60]
    last = [s["queue"] for s in timeline if s["t"] > record["loadEnd"] - 60]
    # Averages of the first and last minute; a run under 2 minutes has overlapping windows.
    first_mean = statistics.fmean(first) if first else 0
    last_mean = statistics.fmean(last) if last else 0
    flat = last_mean <= first_mean + FLAT_TOLERANCE
    record["queueDepth"] = {
        "first": queue[:1],
        "last": queue[-1:],
        "max": max(queue, default=0),
        "firstMinuteMean": round(first_mean, 1),
        "lastMinuteMean": round(last_mean, 1),
        "flat": flat,
    }
    passed = record["k6Exit"] == 0 and flat
    write(Path(f"{prefix}.json"), record | {"passed": passed})
    print(f"k6 exit {record['k6Exit']}, queue depth {record['queueDepth']}")
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


def detection(runs: int, scenarios: list[str]) -> int:
    rows, raw, failures = [], [], []
    try:
        for scenario in scenarios or FAULTS:
            for run in range(1, runs + 1):
                try:
                    found = run_scenario(scenario)
                except AssertionError as e:
                    print(f"FAIL {e}", flush=True)
                    failures.append({"scenario": scenario, "run": run, "error": str(e)})
                    continue
                rows += found
                raw += [
                    dict(
                        zip(
                            ("scenario", "sensorId", "rule", "opened", "appeared"),
                            r,
                            strict=True,
                        )
                    )
                    | {"run": run}
                    for r in found
                ]
    finally:
        kubectl("set", "env", "deployment/simulator", "SCENARIO-")
    summary = []
    for key in sorted({(r["scenario"], r["sensorId"], r["rule"]) for r in raw}):
        mine = [r for r in raw if (r["scenario"], r["sensorId"], r["rule"]) == key]
        summary.append(
            dict(zip(("scenario", "sensorId", "rule"), key, strict=True))
            | {"runs": len(mine)}
            | {
                f"{field}{stat.__name__.title()}": round(
                    stat(r[field] for r in mine), 1
                )
                for field in ("opened", "appeared")
                for stat in (statistics.median, max)
            }
        )
    write(
        Path(f"{new_prefix('detection')}.json"),
        {"runs": runs, "rows": raw, "failures": failures, "summary": summary},
    )
    print_table(rows)
    for row in summary:
        print(row)
    return 1 if failures else 0


def expected(prefix: Path) -> dict:
    """What ingest accepted, from k6's raw output: see `chaos` in the module docstring."""
    accepted, spiked, unsure = set(), set(), 0
    with gzip.open(f"{prefix}-k6.json.gz", "rt") as f:
        for line in f:
            point = json.loads(line)
            # One check per batch: 1 if ingest answered 202 and rejected no reading.
            if point["type"] != "Point" or point["metric"] != "checks":
                continue
            meta = point["data"]["metadata"]
            if not point["data"]["value"]:
                unsure += 1
                continue
            accepted.add((meta["engineId"], meta["second"]))
            if meta["spike"] == "true":
                spiked.add(meta["engineId"])
    sensors = len(load_sensors()["sensorTypes"])
    alerts = sorted(
        [f"{engine}.{sensor_id.split('.', 1)[1]}", rule]
        for engine in spiked
        for sensor_id, rule in EXPECTED["spike"]
    )
    return {
        "readings": len(accepted) * sensors,
        "alerts": alerts,
        # Not 202, or some reading rejected: ingest may or may not have queued them.
        "unsure": unsure,
    }


def chaos(duration: int, rate: int) -> int:
    kills = []

    def kill(k6):
        while True:
            try:
                return k6.wait(timeout=KILL_EVERY)
            except subprocess.TimeoutExpired:
                pass
            pods = kubectl(
                "get",
                "pods",
                "-l",
                "app=processor",
                "--field-selector=status.phase=Running",
                "-o",
                "name",
            ).split()
            if not pods:  # all restarting: skip this kill
                continue
            pod = random.choice(pods)
            kubectl("delete", pod, "--grace-period=0", "--force", "--wait=false")
            kills.append({"t": time.time(), "pod": pod})
            print(f"  killed {pod}", flush=True)

    visibility = in_processor(VISIBILITY).strip()
    in_processor(VISIBILITY, str(CHAOS_VISIBILITY))
    try:
        env = {"RATE": rate, "DURATION": f"{duration}s", "SPIKE_AT": SPIKE_AT}
        prefix, record = load("sustained.js", "chaos", env, raw=True, during=kill)
    finally:
        in_processor(VISIBILITY, visibility)
    want = expected(prefix)
    got = json.loads(in_processor(STORED, record["env"]["FLIGHT_ID"]))
    dlq = [s["dlq"] for s in record["timeline"]]
    wanted, stored = (
        Counter(map(tuple, want["alerts"])),
        Counter(map(tuple, got["alerts"])),
    )
    diff = {
        "missingAlerts": sorted((wanted - stored).elements()),
        "extraAlerts": sorted((stored - wanted).elements()),
        "readingsDelta": got["readings"] - want["readings"],  # stored minus expected
    }
    passed = (
        want["unsure"] == 0
        and not diff["missingAlerts"]
        and not diff["extraAlerts"]
        and diff["readingsDelta"] == 0
    )
    record |= {
        "visibilityTimeout": CHAOS_VISIBILITY,
        "kills": kills,
        "expected": want,
        "stored": got,
        "diff": diff,
        "dlqAdded": dlq[-1] - dlq[0],
        "passed": passed,
    }
    write(Path(f"{prefix}.json"), record)
    print(f"expected {want}\nstored   {got}\ndiff     {diff}")
    print(f"{len(kills)} pods killed, {record['dlqAdded']} messages dead-lettered")
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("scale")
    p.add_argument("--steps", default="5,10,20,40")
    p.add_argument("--step-seconds", type=int, default=120)
    p = commands.add_parser("sustained")
    p.add_argument("--rate", type=int, required=True)
    p.add_argument("--duration", type=int, default=600)
    p = commands.add_parser("detection")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("scenarios", nargs="*", choices=FAULTS)
    p = commands.add_parser("chaos")
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--rate", type=int, default=10)
    args = parser.parse_args()
    if args.command == "scale":
        return scale(args.steps, args.step_seconds)
    if args.command == "sustained":
        return sustained(args.rate, args.duration)
    if args.command == "detection":
        return detection(args.runs, args.scenarios)
    return chaos(args.duration, args.rate)


if __name__ == "__main__":
    sys.exit(main())
