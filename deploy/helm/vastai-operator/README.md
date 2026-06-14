# vastai-operator Helm chart

Deploys the Vast.ai Kubernetes operator (CRDs + RBAC + Deployment).

## Install

From the published OCI chart (GitHub Container Registry):

```bash
helm install vastai-operator oci://ghcr.io/lazlovalentin/vastai-operator \
  --version 0.1.0 \
  --namespace vastai-operator-system --create-namespace
```

Or from a local checkout:

```bash
helm install vastai-operator deploy/helm/vastai-operator \
  --namespace vastai-operator-system --create-namespace
```

The default image is `ghcr.io/lazlovalentin/vastop` (tag falls back to the chart
appVersion). Override with `--set image.repository=… --set image.tag=…`.

Wire a global Vast.ai API key (optional — CRs can carry their own
`spec.apiKeySecretRef`):

```bash
# let the chart create the Secret
helm upgrade vastai-operator deploy/helm/vastai-operator -n vastai-operator-system \
  --reuse-values --set vastApiKey.create=true --set vastApiKey.value=YOUR_KEY

# OR reference an existing Secret holding key VAST_API_KEY
helm upgrade vastai-operator deploy/helm/vastai-operator -n vastai-operator-system \
  --reuse-values --set vastApiKey.existingSecret=vastai-credentials
```

## CRDs

The four CRDs ship under `crds/`. Helm installs them on first `helm install`
but **does not upgrade them** on `helm upgrade` (standard Helm behaviour). When
the CRD schema changes, re-apply manually:

```bash
kubectl apply -f deploy/crds/
```

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `replicaCount` | `1` | Keep at 1 — kopf peering not configured. |
| `image.repository` | `vastai-operator` | Operator image. |
| `image.tag` | `""` | Falls back to `Chart.AppVersion`. |
| `config.syncIntervalSeconds` | `60` | `VAST_SYNC_INTERVAL`. |
| `config.healthProbeIntervalSeconds` | `24` | `VAST_HEALTH_PROBE_INTERVAL` (5 probes / 2 min). |
| `config.searchTimeoutSeconds` | `""` | `VAST_SEARCH_TIMEOUT` (empty = operator default). |
| `config.orderRefreshSeconds` | `""` | `VAST_ORDER_REFRESH` (empty = operator default). |
| `vastApiKey.create` / `.value` | `false` / `""` | Chart-rendered Secret. |
| `vastApiKey.existingSecret` | `""` | Reference a pre-existing Secret. |
| `serviceAccount.create` | `true` | |
| `rbac.create` | `true` | Cluster-scoped (cross-namespace Secrets + CRs). |
| `createNamespace` | `false` | Prefer `--create-namespace`. |

The operator watches **all namespaces** (the entrypoint hard-codes kopf
`clusterwide=True`), which is why the RBAC is a ClusterRole with `get` on
Secrets cluster-wide.

## Uninstall

```bash
helm uninstall vastai-operator -n vastai-operator-system
# CRDs are NOT removed by helm uninstall — delete them explicitly if desired:
kubectl delete -f deploy/crds/
```
