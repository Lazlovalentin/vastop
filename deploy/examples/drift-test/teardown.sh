#!/usr/bin/env bash
# Cleanup the drift-test demo resources.
set -euo pipefail
NS=${NS:-default}

kubectl delete vastinstance drift-inst -n "$NS" --ignore-not-found
kubectl delete vastorder drift-order -n "$NS" --ignore-not-found
kubectl delete vasttemplate ubuntu-drift -n "$NS" --ignore-not-found
kubectl delete secret vastai-credentials -n "$NS" --ignore-not-found
echo "torn down."
