#!/usr/bin/env bash
# Creates the `telemetry` kind cluster and deploys the `local` overlay. Safe to re-run.
# Needs docker, kind and kubectl. Ingest is then reachable through the Gateway at http://localhost:8080.
set -euo pipefail
cd "$(dirname "$0")/.."

ENVOY_GATEWAY=v1.9.3
KEDA=v2.21.0
k() { kubectl --context kind-telemetry "$@"; }

existed=true
kind get clusters 2>/dev/null | grep -qx telemetry || { existed=false; kind create cluster --name telemetry --config k8s/kind.yaml; }

for s in simulator ingest processor; do
  docker build -t "telemetry/$s:local" -f "$s/Dockerfile" .
  kind load docker-image --name telemetry "telemetry/$s:local"
done

# Envoy Gateway's release manifest also installs the Gateway API CRDs.
k apply --server-side -f "https://github.com/envoyproxy/gateway/releases/download/$ENVOY_GATEWAY/install.yaml"
k -n envoy-gateway-system wait deployment/envoy-gateway --for=condition=Available --timeout=10m

# KEDA scales the processor on queue depth; its CRDs are too large for client-side apply.
k apply --server-side -f "https://github.com/kedacore/keda/releases/download/$KEDA/keda-${KEDA#v}.yaml"
k -n keda wait deployment --all --for=condition=Available --timeout=10m

# These ConfigMaps come from files outside k8s/, which Kustomize cannot read; creating them
# here keeps one copy of each file (symlinks are not reliable on Windows checkouts).
k create namespace telemetry --dry-run=client -o yaml | k apply -f -
configmap() { k -n telemetry create configmap "$1" --from-file="$2" --dry-run=client -o yaml | k apply -f -; }
configmap sensors sensors.yaml
configmap elasticmq-conf local/elasticmq.conf
configmap init-script local/init.sh
# Generated, never committed: the key ingest's clients send.
scripts/ingest-key.sh

# init.sh is idempotent. A finished Job would not rerun, and its pod template is immutable.
k -n telemetry delete job init --ignore-not-found
k apply -k k8s/overlays/local
k -n telemetry wait job/init --for=condition=Complete --timeout=10m
# subPath ConfigMap mounts and rebuilt :local images never reach running pods on their own.
# A new cluster's pods already have both, and a 2-CPU CI runner has no room for surge pods.
if $existed; then k -n telemetry rollout restart deployment/simulator deployment/ingest deployment/processor; fi
for d in $(k -n telemetry get deployments -o name); do k -n telemetry rollout status "$d" --timeout=10m; done
k -n telemetry wait gateway/telemetry --for=condition=Programmed --timeout=10m
echo "Ready: curl http://localhost:8080/actuator/health"
