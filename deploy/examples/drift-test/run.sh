#!/usr/bin/env bash
# End-to-end drift recreate demo.
# Prereq: operator + CRDs installed, kubectl context set, VAST_API_KEY set.
set -euo pipefail

NS=${NS:-default}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ -z "${VAST_API_KEY:-}" ]]; then
    echo "Set VAST_API_KEY env var first." >&2
    exit 1
fi

step() { echo; echo "==> [$(date -u +%H:%M:%S)] $*"; }

step "creating Secret"
kubectl -n "$NS" create secret generic vastai-credentials \
    --from-literal=VAST_API_KEY="$VAST_API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -

step "applying Template v1 (ubuntu:22.04) + Order"
kubectl apply -f "$HERE/01-template-v1.yaml"
kubectl apply -f "$HERE/02-order.yaml"

step "waiting for Template Ready + Order Ready"
until kubectl get vasttemplate ubuntu-drift -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null | grep -q Ready \
      && kubectl get vastorder drift-order -n "$NS" -o jsonpath='{.status.cheapestOfferId}' 2>/dev/null | grep -qE '^[0-9]+$'; do
    sleep 3
done
HASH1=$(kubectl get vasttemplate ubuntu-drift -n "$NS" -o jsonpath='{.status.vastTemplateHash}')
OFFER=$(kubectl get vastorder drift-order -n "$NS" -o jsonpath='{.status.cheapestOfferId}')
echo "Template hash=$HASH1  Order offer=$OFFER"

step "launching Instance"
kubectl apply -f "$HERE/03-instance.yaml"
until kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.instanceId}' 2>/dev/null | grep -qE '^[0-9]+$'; do
    sleep 3
done
ID1=$(kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.instanceId}')
MARK1=$(kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.resolvedTemplate}')
echo "v1 launched id=$ID1 marker=$MARK1 (ubuntu:22.04)"

step "patching Template → ubuntu:24.04"
kubectl patch vasttemplate ubuntu-drift -n "$NS" --type merge \
    --patch-file "$HERE/04-template-v2-patch.yaml"
GEN=$(kubectl get vasttemplate ubuntu-drift -n "$NS" -o jsonpath='{.metadata.generation}')
echo "Template now gen=$GEN"

step "waiting for Template re-Ready (new hash)"
until kubectl get vasttemplate ubuntu-drift -n "$NS" -o jsonpath='{.status.syncedGeneration}' 2>/dev/null \
      | grep -q "^$GEN\$"; do
    sleep 3
done
HASH2=$(kubectl get vasttemplate ubuntu-drift -n "$NS" -o jsonpath='{.status.vastTemplateHash}')
echo "Template re-Ready new_hash=$HASH2"

step "waiting for Instance auto-recreate"
until kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.resolvedTemplate}' 2>/dev/null \
      | grep -q "@$GEN"; do
    sleep 5
    CUR=$(kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.resolvedTemplate}')
    echo "  current marker=$CUR"
done
ID2=$(kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.instanceId}')
MARK2=$(kubectl get vastinstance drift-inst -n "$NS" -o jsonpath='{.status.resolvedTemplate}')
echo "v2 launched id=$ID2 marker=$MARK2 (ubuntu:24.04)"

step "drift recreate complete: $ID1 → $ID2"

if [[ "${KEEP:-0}" == "1" ]]; then
    echo "KEEP=1 → leaving resources in place. Clean up with: $HERE/teardown.sh"
    exit 0
fi

step "cleanup"
kubectl delete vastinstance drift-inst -n "$NS"
kubectl delete vastorder drift-order -n "$NS"
kubectl delete vasttemplate ubuntu-drift -n "$NS"
kubectl delete secret vastai-credentials -n "$NS"
echo "done."
