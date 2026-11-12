#!/usr/bin/env bash
# Creates the Kubernetes Secret ingest-api-key: the shared key every ingest client sends in X-Api-Key.
# Generated once; re-runs keep the existing key. Called by scripts/kind-up.sh and scripts/aws-up.sh.
# The key goes through a pipe only: never to disk, stdout or a command line.
set -euo pipefail

k() { kubectl --context kind-telemetry "$@"; }
# 32 random bytes as hex. Discarded if the Secret already exists.
key=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
# Create first and treat AlreadyExists as success: a `get` first would hide real errors (wrong
# context, RBAC, no Namespace) and race a concurrent run. printf is a builtin, so the key never
# appears in a process argument list. `create`, not client-side apply, which would copy the
# plaintext into the last-applied-configuration annotation. Only stderr is kept, and it names the
# Secret, never its data.
if err=$(printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: ingest-api-key\n  namespace: telemetry\nstringData:\n  key: "%s"\n' "$key" |
  k create -f - 2>&1 >/dev/null); then
  echo "Secret ingest-api-key created"
elif [[ $err == *AlreadyExists* ]]; then
  echo "Secret ingest-api-key exists: keeping its key"
else
  echo "$err" >&2
  exit 1
fi
