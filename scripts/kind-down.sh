#!/usr/bin/env bash
# Deletes the `telemetry` kind cluster and everything in it.
set -euo pipefail
kind delete cluster --name telemetry
