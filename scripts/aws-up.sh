#!/usr/bin/env bash
# Deploys the `aws` overlay to the kind cluster: real SQS, DynamoDB and SNS from `terraform/main`.
# Run after `terraform apply` and scripts/aws-key.sh. The kind cluster, images, Envoy Gateway and KEDA come from scripts/kind-up.sh (a cluster created before KEDA was added must re-run it first).
# Usage: scripts/aws-up.sh [--dry-run]   (--dry-run writes aws.env and prints the rendered manifests only)
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --dry-run) dry=1 ;;
  "") dry=0 ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
for t in terraform kubectl; do command -v "$t" >/dev/null || { echo "$t not found" >&2; exit 1; }; done

# One assignment per output: a failed `terraform output` inside echo's arguments would not trip set -e.
tf() { terraform -chdir=terraform/main output -raw "$1"; }
queue=$(tf readings_queue_url)
dlq=$(tf dlq_url)
topic=$(tf alerts_topic_arn)
printf 'TELEMETRY_QUEUE_URL=%s\nQUEUE_URL=%s\nDLQ_URL=%s\nALERTS_TOPIC_ARN=%s\n' "$queue" "$queue" "$dlq" "$topic" > k8s/overlays/aws/aws.env

if [ "$dry" = 1 ]; then kubectl kustomize k8s/overlays/aws; exit 0; fi

k() { kubectl --context kind-telemetry "$@"; }
k -n telemetry get secret aws-credentials >/dev/null || { echo "Secret aws-credentials missing: run scripts/aws-key.sh first" >&2; exit 1; }
# Only what the local overlay adds on top of base. `delete -k k8s/overlays/local` would also delete the
# Namespace and the Secret in it; `apply -k k8s/overlays/aws` replaces the base objects anyway.
k -n telemetry delete -f k8s/overlays/local/emulators.yaml -f k8s/overlays/local/init-job.yaml --ignore-not-found
# Created outside Kustomize, as in scripts/kind-up.sh (the file lives outside k8s/).
k -n telemetry create configmap sensors --from-file=sensors.yaml --dry-run=client -o yaml | k apply -f -
k apply -k k8s/overlays/aws
# subPath ConfigMap mounts do not reach running pods on their own.
k -n telemetry rollout restart deployment/ingest deployment/processor
for d in ingest processor; do k -n telemetry rollout status "deployment/$d" --timeout=5m; done
echo "Ready: curl http://localhost:8080/actuator/health"
