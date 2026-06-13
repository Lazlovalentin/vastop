# drift-test

End-to-end demonstration that mutating a `VastTemplate` automatically
recreates the `VastInstance` that references it.

## Prereqs

- Operator + CRDs installed (`deploy/crds/`, `deploy/rbac.yaml`,
  `deploy/deployment.yaml`).
- Cluster reachable; `kubectl config current-context` points at the right
  cluster (this WILL spend Vast.ai credits — a real GPU rental is created).
- `VAST_API_KEY` env var set to a working Vast.ai API key.

## Manifests

| File | Purpose |
|---|---|
| `01-template-v1.yaml` | VastTemplate with `image: ubuntu:22.04`. |
| `02-order.yaml` | Cheap any-GPU order (≤ $0.30/hr). |
| `03-instance.yaml` | VastInstance referencing both; `recreateOnTemplateUpdate: true`. |
| `04-template-v2-patch.yaml` | Strategic merge patch flipping the image to `ubuntu:24.04`. Apply with `kubectl patch --patch-file`. |
| `run.sh` | Drives the full flow and asserts the recreate happened. |
| `teardown.sh` | Deletes every resource the demo creates. |

## Running

```bash
chmod +x run.sh teardown.sh   # first time only
export VAST_API_KEY=<your-key>
./run.sh              # cleans up at the end
KEEP=1 ./run.sh       # leave resources running for inspection
./teardown.sh         # explicit cleanup if KEEP=1 was used

# or invoke directly without chmod:
bash run.sh
```

## What you should see

1. `Template Ready hash=<h1>` — server-side template created on Vast.ai.
2. `Order Ready offer=<N>` — search finished, cheapest offer picked.
3. `v1 launched id=<I1> marker=ubuntu-drift@1` — Vast.ai instance running
   ubuntu:22.04.
4. Template patched → `gen=2`.
5. `Template re-Ready new_hash=<h2>` — operator created a **new** Vast.ai
   template (different hash) and deleted the old one.
6. ~30–60s later: `v2 launched id=<I2> marker=ubuntu-drift@2` — Instance
   sync_status timer detected drift, destroyed `<I1>`, launched `<I2>`
   from the v2 template.

## Why the hash changes on update

Vast.ai's `PUT /template/` endpoint requires `creator_id` in the body, and
the `vastai` SDK does not populate it — every update call returns
`400 "Invalid Creator ID"`. The operator works around this by
`create_template`-ing a new template (new hash) and `delete_template`-ing
the old one on every spec mutation. See `CLAUDE.md → Known bugs`.

## Cost

The demo rents a GPU for the duration of `run.sh`. At the default
`VAST_SYNC_INTERVAL=60`, expect ~90–120s total runtime: ≈ $0.002 at
$0.05/hr. Spot offers (`rentalType: interruptible`) cost less but can be
preempted.
