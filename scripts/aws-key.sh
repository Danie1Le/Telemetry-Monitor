#!/usr/bin/env bash
# Creates the demo IAM access key and writes it straight into the Kubernetes Secret aws-credentials.
# The key goes through a pipe only: never to disk, stdout or a command line, and never into Terraform state.
# Usage: scripts/aws-key.sh [--dry-run]   create the key and the Secret (--dry-run: only say what would happen)
#        scripts/aws-key.sh --delete      delete the Secret and every access key of the demo user
# Needs terraform state in terraform/main (for the demo user name), the aws CLI and kubectl.
set -euo pipefail
cd "$(dirname "$0")/.."

case "${1:-}" in
  -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  --delete) mode=delete ;;
  --dry-run) mode=dry ;;
  "") mode=create ;;
  *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
esac
for t in terraform aws kubectl; do command -v "$t" >/dev/null || { echo "$t not found" >&2; exit 1; }; done

user=$(terraform -chdir=terraform/main output -raw demo_user_name)
k() { kubectl --context kind-telemetry "$@"; }
# Assigned on its own line so a failed list stops the script (set -e ignores it inside a for word list).
keys=$(aws iam list-access-keys --user-name "$user" --query 'AccessKeyMetadata[].AccessKeyId' --output text)

case "$mode" in
  dry) echo "would create an access key for IAM user $user and apply Secret aws-credentials in namespace telemetry" ;;
  create)
    # A second key would leave the first active and stored nowhere.
    [ -z "$keys" ] || { echo "$user already has access key(s) $keys: run scripts/aws-key.sh --delete first" >&2; exit 1; }
    k create namespace telemetry --dry-run=client -o yaml | k apply -f - >/dev/null
    # printf is a builtin, so the secret never appears in a process argument list.
    # Server-side apply: client-side apply would copy the plaintext into the last-applied-configuration annotation.
    aws iam create-access-key --user-name "$user" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text |
      { read -r id secret; printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: aws-credentials\n  namespace: telemetry\nstringData:\n  AWS_ACCESS_KEY_ID: "%s"\n  AWS_SECRET_ACCESS_KEY: "%s"\n' "$id" "$secret"; } |
      k apply --server-side -f - >/dev/null
    echo "Secret aws-credentials created for $user" ;;
  delete)
    k -n telemetry delete secret aws-credentials --ignore-not-found
    for id in $keys; do
      aws iam delete-access-key --user-name "$user" --access-key-id "$id"
      echo "deleted access key $id"
    done ;;
esac
