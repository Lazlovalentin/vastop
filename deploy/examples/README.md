# Examples

Numbered manifests demonstrating each CRD and common usage patterns.

| File | What it shows |
|------|---------------|
| `01-secrets.yaml` | Vast.ai API key + Slack webhook secrets (placeholders). |
| `02-template-ubuntu.yaml` | Minimal SSH-only Ubuntu template. |
| `03-template-pytorch.yaml` | PyTorch + Jupyter Lab template (env, onstart). |
| `08-template-llm-inference.yaml` | vLLM OpenAI-compatible inference server. |
| `09-template-stable-diffusion.yaml` | ComfyUI for Stable Diffusion. |
| `10-template-args-only.yaml` | One-shot training job (`runtype: args`). |
| `04-order-cheap-any.yaml` | Cheap any-GPU search (≤ $0.30/hr). |
| `05-order-rtx4090.yaml` | Verified RTX 4090 search. |
| `11-order-h100-train.yaml` | 8x H100 SXM with RAM/CPU/network floors. |
| `12-order-interruptible.yaml` | Interruptible (bid) order, nested filter blocks, on-demand fallback. |
| `06-instance.yaml` | Default Instance (auto-recreate on Template + ref change). |
| `13-instance-no-recreate.yaml` | Pinned Instance (no recreate — production). |
| `14-instance-interruptible.yaml` | Spot Instance referencing the spot Order. |
| `07-alert.yaml` | Slack alert on terminate/expired/failed. |
| `15-alert-auto-recreate.yaml` | Spot keepalive — alert + recreate. |
| `16-alert-notify-only.yaml` | Production-style alert (notify only, long cooldown). |
| `17-instance-healthcheck.yaml` | Worker HTTP health probe + WorkerUnhealthy alert with recreate. |
| `18-instance-rollover.yaml` | Graceful pre-expiry rollover — launch + health-gate replacement before the rental ends. |

## Apply order

CRDs and the operator must already be installed (`deploy/crds/`,
`deploy/rbac.yaml`, `deploy/deployment.yaml`). Then for any flow:

1. Apply Secrets first (`01-secrets.yaml` — fill in real values).
2. Apply at least one Template (`02-…`, `03-…`, `08-…`, etc.).
3. Apply at least one Order (`04-…`, `05-…`, etc.).
4. Wait until Template `phase=Ready` (operator registers it on Vast.ai and
   stores the hash in `.status`) and Order `phase=Ready`.
5. Apply an Instance — its `templateRef.name` + `orderRef.name` must match the
   ones above, in the same namespace.
6. Optionally apply an Alert pointing at the Instance.

```bash
kubectl get vasttemplates,vastorders,vastinstances,vastalerts -n default
```

## Drift recreate flow

1. Apply `02-template-ubuntu.yaml` + `04-order-cheap-any.yaml` + `06-instance.yaml`
   (with `templateRef.name: ubuntu-ssh` and `orderRef.name: cheap-any-gpu`).
2. Wait for the Instance to reach `phase=Running` and note
   `.status.resolvedTemplate` (e.g. `ubuntu-ssh@1`).
3. Mutate the Template:
   ```bash
   kubectl patch vasttemplate ubuntu-ssh --type merge \
     -p '{"spec":{"diskGB":24,"description":"bumped"}}'
   ```
   `metadata.generation` jumps to 2; the operator calls `update_template` on
   Vast.ai.
4. Within ~60s (default `VAST_SYNC_INTERVAL`) the Instance's `sync_status`
   timer detects drift, destroys the old Vast.ai instance, and re-launches
   from the updated template. `.status.resolvedTemplate` now reads `…@2`.

Set `spec.recreateOnTemplateUpdate: false` on the Instance to opt out
(see `13-instance-no-recreate.yaml`).

## End-to-end demos

- `drift-test/` — Mutate a `VastTemplate` and watch the operator auto-recreate
  the running Vast.ai instance. Includes a `run.sh` that asserts the flow.

## Cleanup

Deleting a `VastInstance` destroys the Vast.ai instance.
Deleting a `VastTemplate` deletes the Vast.ai template (by numeric id).
Deleting a `VastOrder` only removes the k8s object — no Vast.ai-side state.
Deleting a `VastAlert` stops watching.
