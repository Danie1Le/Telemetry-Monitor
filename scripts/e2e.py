"""Runs fault scenarios end to end on the kind cluster from scripts/kind-up.sh.

For each scenario: restart the simulator on it, read the new flight's id from its log, poll
GET /alerts through the Gateway, and assert the flight raises exactly the expected alerts.
Prints each alert's detection time in seconds after its fault starts: `opened` is the alert's
openedAt, `appeared` is when GET /alerts first returned it (polled every second, on this
machine's clock: on Docker Desktop that can run a second or two behind the cluster's).

Run from the repo root: uv run --project simulator scripts/e2e.py [scenario ...]
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from simulator.contracts import load_sensors
from simulator.faults import load_scenario
from simulator.flight import flight_seconds

ALERTS_URL = "http://localhost:8080/alerts?flightId="
SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
# Seconds from a fault's start until its alert must be readable, and how long after the last
# fault ends a late alert is still awaited. The rules alone take up to 13 s (drift: rolling
# window plus sustain); the rest covers simulator -> ingest -> SQS -> processor -> DynamoDB.
BOUND = 30
# (sensorId, rule) of every alert a scenario's flight raises, and nothing else.
EXPECTED = {
    "spike": [
        ("engine-1.chamber_temp", "rate_of_change"),
        ("engine-1.chamber_temp", "threshold"),
    ],
    "dropout": [("engine-1.tank_pressure", "dropout")],
    "stuck": [("engine-1.fuel_flow", "flatline")],
    "drift": [("engine-1.vibration", "drift")],
    "corrupt": [],
    "none": [],
}


def kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", "--context", "kind-telemetry", "-n", "telemetry", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def start_flight(scenario: str) -> str:
    """Restarts the simulator on a scenario and returns the id of the flight it starts."""
    # restartedAt forces a new pod even when SCENARIO is unchanged (a repeated scenario).
    restarted = datetime.now(UTC).isoformat()
    path = f"/etc/telemetry/scenarios/{scenario}.yaml"
    template = {
        "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted}},
        "spec": {
            "containers": [
                {"name": "simulator", "env": [{"name": "SCENARIO", "value": path}]}
            ]
        },
    }
    patch = json.dumps({"spec": {"template": template}})
    kubectl("patch", "deployment/simulator", "-p", patch)
    kubectl("rollout", "status", "deployment/simulator", "--timeout=5m")
    # The old pod may still be terminating, and starting flights: read only the newest pod.
    newest = "--sort-by=.metadata.creationTimestamp"
    pods = kubectl("get", "pods", "-l", "app=simulator", "-o", "name", newest)
    pod = pods.split()[-1]
    for _ in range(60):
        if found := re.search(r"flight (\S+) started", kubectl("logs", pod)):
            return found[1]
        time.sleep(1)
    raise AssertionError(f"{pod} started no flight within 60 s")


def alerts(flight_id: str) -> list[dict]:
    with urllib.request.urlopen(ALERTS_URL + flight_id, timeout=5) as response:
        return json.load(response)


def run_scenario(scenario: str) -> list[tuple]:
    """Flies one scenario; returns (scenario, sensorId, rule, opened, appeared) per alert.

    Raises AssertionError unless the flight raises exactly EXPECTED[scenario], each within
    BOUND of its fault's start.
    """
    cfg = load_sensors()
    faults = load_scenario(SCENARIOS / f"{scenario}.yaml", cfg)
    flight_id = start_flight(scenario)
    stamp = datetime.strptime(flight_id[:16], "%Y%m%dT%H%M%SZ")
    start = stamp.replace(tzinfo=UTC).timestamp()
    fault_start = start + min((f["startSecond"] for f in faults), default=0)
    # No faults: watch the whole flight, through every phase change.
    last = max(
        (f["startSecond"] + f["durationSeconds"] for f in faults),
        default=flight_seconds(cfg),
    )
    until = start + last + BOUND
    print(f"{scenario}: flight {flight_id}, {until - time.time():.0f} s", flush=True)
    appeared = {}
    while (now := time.time()) < until:
        try:
            for a in alerts(flight_id):
                appeared.setdefault((a["sensorId"], a["rule"], a["openedAt"]), now)
        except OSError as e:  # a missed poll only delays `appeared`
            print(f"  GET /alerts failed: {e}", flush=True)
        time.sleep(1)
    rows = [
        (scenario, sensor, rule, opened / 1000 - fault_start, seen - fault_start)
        for (sensor, rule, opened), seen in sorted(appeared.items())
    ]
    got = sorted((sensor, rule) for _, sensor, rule, _, _ in rows)
    assert got == sorted(EXPECTED[scenario]), f"{scenario}: alerts {got}"
    late = [r for r in rows if r[4] > BOUND]
    assert not late, f"{scenario}: alerts later than {BOUND} s: {late}"
    return rows


def print_table(rows: list[tuple]) -> None:
    print(
        f"{'scenario':<8}  {'sensor':<22}  {'rule':<14}  {'opened':>6}  {'appeared':>8}"
    )
    for scenario, sensor, rule, opened, seen in rows:
        print(
            f"{scenario:<8}  {sensor:<22}  {rule:<14}  {opened:>5.1f}s  {seen:>7.1f}s"
        )


def main() -> int:
    rows, failed = [], []
    try:
        for scenario in sys.argv[1:] or EXPECTED:
            try:
                scenario_rows = run_scenario(scenario)
            except AssertionError as e:
                print(f"FAIL {e}", flush=True)
                failed.append(scenario)
                continue
            print(f"PASS {scenario}: {len(scenario_rows)} alert(s)", flush=True)
            rows += scenario_rows
    finally:
        kubectl("set", "env", "deployment/simulator", "SCENARIO-")
    print_table(rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
