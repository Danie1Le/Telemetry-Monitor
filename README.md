# Telemetry Monitor

A simulated launch vehicle streams Readings; the system detects Anomalies with Rules and raises Alerts. Terms (Reading, Batch, Flight, Phase, Fault, Alert...) are defined in [GLOSSARY.md](GLOSSARY.md). Design decisions: [docs/adr/](docs/adr/).

```
simulator --HTTP Batch--> ingest --> SQS --> processor --> DynamoDB (readings, alerts) + SNS
                                                 |
                                       Valkey (Rule state)     GET /alerts (ingest) <-- DynamoDB
```

## Layout

| Path | What |
|---|---|
| `simulator/` | Python. Flies Flights, injects Faults from `scenarios/` or `POST /faults` |
| `ingest/` | Java (Spring Boot). Validates Batches, drops Corrupt readings, queues to SQS, serves `GET /alerts` |
| `processor/` | Python. Stores Readings, runs the Rules, records Alerts, publishes to SNS |
| `contracts/`, `sensors.yaml`, `scenarios/` | Shared config and JSON schemas, no shared code |
| `k8s/` | Base manifests, `local` (emulators) and `aws` overlays |
| `terraform/` | `bootstrap` (budgets) and `main` (queues, tables, topics, demo IAM user) |
| `load/`, `scripts/` | k6 scripts and the scripts below |
| `docs/adr/`, `docs/runbook.md` | Decisions and the one-time AWS deploy |

## Prerequisites

Docker, `kind`, `kubectl`, [uv](https://docs.astral.sh/uv/) (Python 3.14), and a JDK (the Gradle wrapper provisions Java 25). Terraform and the AWS CLI only for the AWS runbook.

## Local loop (kind)

```bash
scripts/kind-up.sh                                      # cluster, images, emulators; ingest at http://localhost:8080
uv run --project simulator scripts/e2e.py [scenario]    # fly each Scenario, assert its Alerts
uv run --project simulator scripts/measure.py --help    # load, detection and chaos measurements
scripts/kind-down.sh
```

`curl "http://localhost:8080/alerts?flightId=<id>"` reads a Flight's Alerts. Flight ids are in `kubectl --context kind-telemetry -n telemetry logs deploy/simulator`.

## Run one service on the host

`docker compose up -d` starts only the emulators: ElasticMQ (SQS, 9324), DynamoDB Local (8000), moto (SNS, 5000), Valkey (6379), and an `init` container that creates the `readings` and `alerts` tables and the `alerts` and `ops` topics. Ingest's defaults already point at them:

```bash
docker compose up -d
(cd ingest && ./gradlew bootRun)               # :8080
uv run --project simulator python -m simulator # posts to http://localhost:8080/readings
```

## Tests

```bash
cd simulator && uv run pytest
cd processor && uv run pytest -m "not integration"
cd processor && uv run pytest -m integration              # Testcontainers: needs Docker, not docker compose
cd ingest && ./gradlew spotlessCheck test
cd ingest && ./gradlew integrationTest
```

## CI

`.github/workflows/ci.yml`: ruff, pytest with coverage gates (simulator, processor), Gradle checks (ingest), image builds, the `kind` e2e run, and terraform fmt/validate/tflint.

## More

- [RESULTS.md](RESULTS.md): measured numbers, each with how it was measured
