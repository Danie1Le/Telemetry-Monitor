# Results

Every number here was measured, and links to its raw output in `results/<date>/`.
The tools: `load/` (k6 scripts)
and `scripts/measure.py` (see its docstring for each command and what it counts).

## Metrics

| Metric | Value | Environment | Date | Commit | Config | Hardware | Raw output |
|---|---|---|---|---|---|---|---|
| <a id="throughput"></a>Sustained readings/s (10 min, queue depth flat, no 5xx; throttling not checkable on DynamoDB Local) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="p95"></a>Ingest p95 latency at the sustained rate | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="ramp"></a>Ramp: highest step with no failures (readings/s) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="detect-spike"></a>Detection time, spike: median / max (10 runs) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="detect-dropout"></a>Detection time, dropout: median / max (10 runs) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="detect-stuck"></a>Detection time, stuck: median / max (10 runs) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="detect-drift"></a>Detection time, drift: median / max (10 runs) | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="scale-up"></a>KEDA scale-up: queue over target to second replica Ready | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="scale-down"></a>KEDA scale-down: queue drained to one replica | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="chaos"></a>Chaos: stored readings and alerts vs expected, pods killed | TBD | kind | TBD | TBD | TBD | TBD | TBD |
| <a id="aws-throughput"></a>Capped-rate 10 min run: readings/s, p95 | TBD | AWS | TBD | TBD | TBD | n/a | TBD |
| <a id="aws-detection"></a>Detection time per fault: median / max (**3 runs per fault**) | TBD | AWS | TBD | TBD | TBD | n/a | TBD |
| <a id="coverage-rules"></a>Coverage, anomaly rules | TBD | local | TBD | TBD | TBD | n/a | TBD |
| <a id="coverage"></a>Coverage, overall | TBD | local | TBD | TBD | TBD | n/a | TBD |

Config names the command and its arguments (for example `measure.py sustained --rate 40`),
and any setting changed from the committed one. Hardware is CPU, RAM and the Docker Desktop
resource limits. Scale-up and scale-down times are sampled every ~3-4 s, so they are
+/- ~3-4 s. DynamoDB Local never throttles: "no throttling" is only checkable on AWS.

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
