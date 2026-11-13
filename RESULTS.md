# Results

Every number here was measured, and links to its raw output in `results/<date>/`. The tools:
`load/` (k6 scripts) and `scripts/measure.py` (see its docstring for each command and what it
counts). The two AWS rows are from the one-time AWS run in [docs/runbook.md](docs/runbook.md)
(step 9): real SQS, DynamoDB and SNS in us-east-2, with ingest, the processor, the simulator and
KEDA still on the kind cluster. [See the AWS note](#aws-note).

## Metrics

| Metric | Value | Environment | Date | Commit | Config | Hardware | Raw output |
|---|---|---|---|---|---|---|---|
| <a id="throughput"></a>Sustained readings/s (10 min, no 5xx, queue not growing; throttling not checkable on DynamoDB Local) | 1,500 (30 batches/s): k6 passed (0 non-2xx, 0 rejected, 0 dropped), nothing dead-lettered. Queue depth is a 0-698 sawtooth as KEDA scales the processor between 1 and 5 replicas, not a flat line; the run counts as not growing because the last-minute mean (178.0) is at most the first-minute mean (236.6) + 10. [Every attempt](#sustained-attempts) | kind | 2026-11-10 | 4b5664e | `measure.py sustained --rate 30 --duration 600` (30 batches/s), on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/sustained-064954.json](results/2026-11-10/sustained-064954.json) ([k6](results/2026-11-10/sustained-064954-k6.txt)) |
| <a id="p95"></a>Ingest p95 latency at the sustained rate | 9.13 ms (max 47.3 ms) at 1,500 readings/s | kind | 2026-11-10 | 4b5664e | `measure.py sustained --rate 30 --duration 600` (30 batches/s), on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/sustained-064954.json](results/2026-11-10/sustained-064954.json) ([k6](results/2026-11-10/sustained-064954-k6.txt)) |
| <a id="ramp"></a>Ramp: highest step with no failures (readings/s) | 2,000 (40 batches/s) in both ramps, p95 14.5 / 13.3 ms. At 80 batches/s k6 dropped 404 / 73 batches | kind | 2026-11-10 | 4b5664e | `measure.py scale --steps 5,10,20,40,80 --step-seconds 120`, run twice | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [ramp 1](results/2026-11-10/scale-021913.json) ([k6](results/2026-11-10/scale-021913-k6.txt)), [ramp 2](results/2026-11-10/scale-023501.json) ([k6](results/2026-11-10/scale-023501-k6.txt)) |
| <a id="detect-spike"></a>Detection time, spike: median / max (10 runs) | openedAt 0.0 s / 0.0 s (threshold and rate_of_change; 0 by construction, [see note](#appeared)). In GET /alerts -0.6 s / -0.0 s by the host clock, which is clock offset, not a negative latency: [see note](#appeared) | kind | 2026-11-10 | 0707122 | `measure.py detection --runs 10`, on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/detection-095403.json](results/2026-11-10/detection-095403.json) |
| <a id="detect-dropout"></a>Detection time, dropout: median / max (10 runs) | openedAt 0.0 s / 0.0 s; in GET /alerts 2.3 s / 2.8 s | kind | 2026-11-10 | 0707122 | `measure.py detection --runs 10`, on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/detection-095403.json](results/2026-11-10/detection-095403.json) |
| <a id="detect-stuck"></a>Detection time, stuck: median / max (10 runs) | openedAt 0.0 s / 0.0 s; in GET /alerts 5.4 s / 5.9 s | kind | 2026-11-10 | 0707122 | `measure.py detection --runs 10`, on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/detection-095403.json](results/2026-11-10/detection-095403.json) |
| <a id="detect-drift"></a>Detection time, drift: median / max (10 runs) | openedAt 8.0 s / 8.0 s; in GET /alerts 11.5 s / 14.0 s | kind | 2026-11-10 | 0707122 | `measure.py detection --runs 10`, on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/detection-095403.json](results/2026-11-10/detection-095403.json) |
| <a id="scale-up"></a>KEDA scale-up: queue over target to second replica Ready | 21.6 s / 102.3 s (two ramps). In ramp 2 the queue passed 100 for one sample at 171 s and fell back before KEDA scaled; from its next crossing, 22.7 s | kind | 2026-11-10 | 4b5664e | `measure.py scale --steps 5,10,20,40,80 --step-seconds 120`, run twice | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [ramp 1](results/2026-11-10/scale-021913.json) ([k6](results/2026-11-10/scale-021913-k6.txt)), [ramp 2](results/2026-11-10/scale-023501.json) ([k6](results/2026-11-10/scale-023501-k6.txt)) |
| <a id="scale-down"></a>KEDA scale-down: queue drained to one replica | 44.6 s / 56.9 s (two ramps) | kind | 2026-11-10 | 4b5664e | `measure.py scale --steps 5,10,20,40,80 --step-seconds 120`, run twice | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [ramp 1](results/2026-11-10/scale-021913.json) ([k6](results/2026-11-10/scale-021913-k6.txt)), [ramp 2](results/2026-11-10/scale-023501.json) ([k6](results/2026-11-10/scale-023501-k6.txt)) |
| <a id="chaos"></a>Chaos: stored readings and alerts vs expected, pods killed | Pass: 15,000 / 15,000 stored `readings` items (3,000 batches x 5 sensors = 150,000 Readings) and 20 / 20 alerts (10 engines x threshold and rate_of_change), none missing or extra; 29 processor pods force-deleted; all 3,001 requests accepted (3,000 distinct batches, [see note](#chaos-note)); nothing dead-lettered | kind | 2026-11-10 | 0707122 | `measure.py chaos --duration 300` (10 batches/s, visibility timeout 5 s, one pod killed every 10 s), on a new cluster | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-10/chaos-101220.json](results/2026-11-10/chaos-101220.json) ([k6](results/2026-11-10/chaos-101220-k6.txt)) |
| <a id="aws-throughput"></a>Capped-rate 10 min run: readings/s, p95 | 150 (3 batches/s, the cap for the 20 WCU `readings` table): k6 passed (1,800 / 1,800 batches accepted, 0 rejected, 0 failed requests, 0 dropped), p95 124.29 ms (median 72.21 ms, max 686.36 ms), nothing dead-lettered, 0 DynamoDB throttle events. Not sustained by `measure.py`'s rule: queue depth is a 0-146 sawtooth as KEDA scales the processor between 1 and 2 replicas, and the last-minute mean (101.6) is above the first-minute mean (8.8) + 10. [See note](#aws-note) | AWS | 2026-11-13 | c3b0806 | `measure.py sustained --rate 3 --duration 600` (3 batches/s), simulator scaled to 0; throttling from the runbook step 9 CloudWatch loop | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-13/sustained-024818.json](results/2026-11-13/sustained-024818.json) ([k6](results/2026-11-13/sustained-024818-k6.txt), [k6 summary](results/2026-11-13/sustained-024818-k6-summary.json)) |
| <a id="aws-detection"></a>Detection time per fault: median / max (**3 runs per fault**) | Spike (2 of 3 runs, [see note](#aws-note)): openedAt 0.0 s / 0.0 s, in GET /alerts 24.0 s / 26.4 s (threshold and rate_of_change). Dropout: openedAt 0.0 s / 0.0 s, in GET /alerts 15.1 s / 28.9 s. Stuck: openedAt 0.0 s / 0.0 s, in GET /alerts 5.5 s / 18.7 s. Drift: openedAt 8.0 s / 8.0 s, in GET /alerts 14.3 s / 14.7 s | AWS | 2026-11-13 | c3b0806 | `measure.py detection --runs 3`, right after the capped-rate run, simulator back at 1 replica | i7-9700F, 16 GB host; Docker Desktop 8 CPU, 7.7 GiB | [results/2026-11-13/detection-032120.json](results/2026-11-13/detection-032120.json) |
| <a id="coverage-rules"></a>Coverage, anomaly rules | 100% of statements (`rules.py`, 115) | local | 2026-11-10 | 0707122 | `cd processor && uv run pytest -q --cov=processor` (167 passed) | n/a | [results/2026-11-10/coverage-processor.txt](results/2026-11-10/coverage-processor.txt) |
| <a id="coverage"></a>Coverage, overall | Processor 99% of statements (378); ingest 97% of instructions, 79% of branches (JaCoCo) | local | 2026-11-10 | 0707122 | `cd processor && uv run pytest -q --cov=processor` (167 passed); `cd ingest && ./gradlew test jacocoTestReport -q` (32 passed) | n/a | [processor](results/2026-11-10/coverage-processor.txt), [ingest](results/2026-11-10/coverage-ingest.txt) |

Config names the command and its arguments (for example `measure.py sustained --rate 40`),
and any setting changed from the committed one. Hardware is CPU, RAM and the Docker Desktop
resource limits. Scale-up and scale-down times are sampled every ~3-4 s, so they are
+/- ~3-4 s. DynamoDB Local never throttles: "no throttling" is only checkable on AWS.

Throughput, p95, ramp and scale rows ran at 4b5664e. The next commit, 0707122, only
changes when rate_of_change takes its baseline after a phase grace, not the ingest or queue
path. That the load rows stand is argued from the diff, not measured: they were not re-run at
0707122. Detection, chaos and processor coverage ran at 0707122. The ingest coverage run (32
tests) was at 4b5664e and not repeated; 0707122 does not touch ingest.

<a id="chaos-note"></a>Chaos units: the stored `readings` table holds one item per sensor per
batch, so 15,000 items is 3,000 batches x 5 sensors, which is 150,000 Readings. k6 sent 3,001
requests (3,001 iterations at 10/s for 300 s, the first at t=0). Two of them carry the same
engine and second (engine-1005, 1794305688) in the raw k6 output, so they are one batch key,
and the expected count is per distinct key: 3,000 batches.

<a id="appeared"></a>Detection time is from the fault's start (flight start from the Flight
id, plus the fault's start second) to the alert's openedAt, and to the first GET /alerts poll
(every ~1 s) that showed it. openedAt is event time (flight time, set in the cluster), so it is 0 s by
construction for spike, dropout and stuck: the alert opens at the fault's first reading. It
is not a latency. "In GET /alerts" is the host's clock, and the host and flight clocks differ
by up to ~1 s. The bias is one-sided: all 10 spike values are negative, and an alert cannot
appear before its fault, so the offset makes every "in GET /alerts" time, including dropout,
stuck and drift, read too low by up to ~1 s. Read them as understated by up to ~1 s. The
offset was not investigated and nothing was re-run.

<a id="aws-note"></a>AWS run, 2026-11-13: the queue, tables and topic were real (SQS, DynamoDB
and SNS in us-east-2), while ingest, the processor, the simulator and KEDA ran on the local kind
cluster, as [docs/runbook.md](docs/runbook.md) designs it. Images were built from c3b0806; the
only local change during the run was a line-ending fix to `scripts/aws-key.sh` (#8), which
deploys nothing. The rate is capped at 3 batches/s (150 readings/s): the `readings` table has 20
provisioned WCU and one batch writes 5 items, so it throttles above 4 batches/s. The simulator
was scaled to 0 so it added no writes. Nothing throttled: the runbook's CloudWatch loop (Sum,
period 60, 02:47:51Z to 03:24:25Z, re-run after CloudWatch's lag) gave 0 WriteThrottleEvents
and 0 ReadThrottleEvents on both `readings` and `alerts`. Its output was not saved to
`results/`.

The capped-rate run fails `measure.py`'s queue-not-growing check even though k6 passed. One
processor replica fell behind, the queue passed the KEDA target of 100 messages per replica,
KEDA added a second replica (three times: about 177 s, 403 s and 598 s in), the two replicas
drained the queue, and KEDA went back to one. The queue was at 100 or more in 36 of 125
samples, and was empty 48 s after the load stopped. The likely cause, not proven: the processor
runs on the laptop and makes a round trip to us-east-2 for every SQS receive and delete and
every DynamoDB write, so one replica drains far fewer batches/s than on kind, where the same
code held 30 batches/s against ElasticMQ and DynamoDB Local in the cluster. The same round
trip, from ingest's send to SQS, likely explains the higher ingest p95 (124.29 ms against
9.13 ms on kind).

Detection ran right after, with the simulator back at 1 replica. Spike has 2 of 3 runs: run 2
saw no alert in GET /alerts before its poll window closed, 32 s after the fault started
(`spike: alerts []`). The likely cause is that the alerts appeared after the window, since run
1's appeared at 26.4 s; this was not confirmed. The GET /alerts times are slower and more
spread out than on kind (dropout median 15.1 s against 2.3 s, max 28.9 s), likely from the same
round trips, and the [clock offset](#appeared) applies to them too. openedAt is event time and
matches kind.

The runbook's demo (step 8) also worked: a spike posted 75 s into a flight returned 202, GET
/alerts showed threshold and rate_of_change on `engine-1.chamber_temp` 5 s later, and the alert
email arrived. Afterwards `terraform destroy` removed 11 of 11 resources,
`scripts/aws-cleanup-check.sh` found nothing tagged and no demo user or keys left, and the
billing page showed $0.00. Neither was saved to `results/`.

<a id="sustained-attempts"></a>Every sustained attempt, in order. Only the last passed:

| Run | Rate (batches/s) | Cluster | Result |
|---|---|---|---|
| [sustained-025102](results/2026-11-10/sustained-025102.json) | 40 | after both ramps | Fail: 16 batches dropped, queue grew to 7,315 |
| [sustained-030625](results/2026-11-10/sustained-030625.json) | 30 | same cluster, after the run above | Fail: 13 batches dropped, queue grew to 3,550; after the load, 4,098 messages were dead-lettered |
| [sustained-061631](results/2026-11-10/sustained-061631.json) | 30 | new | Fail: 31 batches dropped (one request took 2.8 s); last-minute queue mean 382.2 vs 278.3 first, queue empty at the end |
| sustained-062820 | 30 | new | Lost: Docker Desktop's WSL VM crashed 42 s in; raw output discarded |
| [sustained-064954](results/2026-11-10/sustained-064954.json) | 30 | new | Pass |

Earlier detection and chaos attempts:

| Run | Commit | Cluster | Result |
|---|---|---|---|
| [detection-081113](results/2026-11-10/detection-081113.json) | 4b5664e | new | Fail, 39/40: dropout run 4 also raised a false `engine-1.valve_position` rate_of_change alert at ascent's grace end. Fixed by 0707122 |
| [chaos-095430](results/2026-11-10/chaos-095430-console.txt) | 0707122 | after detection-095403 | No result: k6 passed (3,000 / 3,000 accepted), then DynamoDB Local ran out of Java heap ([log](results/2026-11-10/chaos-095430-dynamodb.log)), was restarted by its liveness probe and lost its in-memory tables, so the stored counts could not be read |

## Reproduce

From the repo root, on the cluster from `scripts/kind-up.sh`:

```sh
uv run --project simulator scripts/measure.py scale --steps 5,10,20,40,80 --step-seconds 120
uv run --project simulator scripts/measure.py sustained --rate <highest passing step> --duration 600
uv run --project simulator scripts/measure.py detection --runs 10
uv run --project simulator scripts/measure.py chaos --duration 300
```

k6 runs as the `grafana/k6` Docker image on kind's Docker network, so nothing is installed on
the host. One batch is one engine's second: 5 sensors x 10 readings = 50 readings.
