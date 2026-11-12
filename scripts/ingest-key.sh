#!/usr/bin/env bash
# Creates the Kubernetes Secret ingest-api-key: the shared key every ingest client sends in X-Api-Key.
# Generated once; re-runs keep the existing key. Called by scripts/kind-up.sh and scripts/aws-up.sh.
# The key goes through a pipe only: never to disk, stdout or a command line.
set -euo pipefail

k() { kubectl --context kind-telemetry "$@"; }
k -n telemetry get secret ingest-api-key >/dev/null 2>&1 && exit 0
# 32 random bytes as hex.
key=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
# printf is a builtin, so the key never appears in a process argument list. `create`, not
# client-side apply, which would copy the plaintext into the last-applied-configuration annotation.
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: ingest-api-key\n  namespace: telemetry\nstringData:\n  key: "%s"\n' "$key" |
  k create -f - >/dev/null
echo "Secret ingest-api-key created"
