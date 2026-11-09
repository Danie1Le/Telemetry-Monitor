#!/usr/bin/env bash
# Removes the `aws` overlay from the kind cluster. Run before `terraform destroy`; it needs aws.env from scripts/aws-up.sh.
# The aws-credentials Secret and the IAM access key are removed by `scripts/aws-key.sh --delete`.
# Usage: scripts/aws-down.sh [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --dry-run) flags=(--dry-run=client) ;;
  "") flags=() ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
[ -f k8s/overlays/aws/aws.env ] || { echo "k8s/overlays/aws/aws.env missing: nothing to render (run scripts/aws-up.sh first)" >&2; exit 1; }

kubectl --context kind-telemetry delete -k k8s/overlays/aws --ignore-not-found "${flags[@]}"
