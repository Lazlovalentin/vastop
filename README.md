# vastai-operator

A Kubernetes operator that provisions and manages GPU instances on
[Vast.ai](https://vast.ai) declaratively, via a `VastInstance` custom resource.

Built with [Kopf](https://kopf.readthedocs.io/) and the
[`vastai`](https://github.com/vast-ai/vast-cli) Python SDK.

## What it does

You apply a `VastInstance` manifest describing the GPU, image, and constraints
you want. The operator:

1. Searches Vast.ai for matching rentable offers.
2. Picks the cheapest one within your `maxPricePerHour`.
3. Creates the instance with your image, env, and `onstart` script.
4. Keeps `.status` reconciled with the real instance state (IP, SSH port,
   price, phase) on a periodic timer.
5. Destroys the instance when you `kubectl delete` the resource.

Spec changes to image / GPU / disk trigger a destroy + recreate (controlled by
`spec.recreateOnSpecChange`).

## Quick start

```bash
# 1. Install CRD + RBAC + operator
kubectl apply -f deploy/namespace.yaml
kubectl apply -f deploy/crds/
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/deployment.yaml

# 2. Create a Secret with your Vast.ai API key
kubectl -n default create secret generic vastai-credentials \
    --from-literal=VAST_API_KEY=YOUR_KEY

# 3. Apply an example VastInstance
kubectl apply -f deploy/examples/vastinstance-example.yaml

# 4. Watch it
kubectl get vastinstances -w
```

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the operator against your current kubeconfig context
export VAST_API_KEY=...
kopf run -m vastai_operator.handlers --verbose --all-namespaces

# Tests
pytest

# Lint / typecheck
ruff check src tests
mypy src
```

## VastInstance spec

| Field                  | Type     | Default      | Notes                                              |
|------------------------|----------|--------------|----------------------------------------------------|
| `image`                | string   | (required)   | Docker image to run.                               |
| `gpuName`              | string   | any          | e.g. `RTX_4090`, `H100_SXM`.                       |
| `numGpus`              | int      | `1`          |                                                    |
| `diskGB`               | int      | `32`         |                                                    |
| `minDownloadMbps`      | number   | `100`        | Filter for slow nodes.                             |
| `maxPricePerHour`      | number   | unbounded    | USD; offer rejected above this.                    |
| `rentalType`           | enum     | `on-demand`  | `on-demand` or `interruptible`.                    |
| `env`                  | map      | `{}`         | Passed as `-e KEY=VALUE` to the container.         |
| `onstart`              | string   | empty        | Shell script run at instance boot.                 |
| `sshKey`               | string   | empty        | Authorized SSH public key.                         |
| `recreateOnSpecChange` | bool     | `true`       | Destroy + recreate when image/gpu/disk changes.    |
| `apiKeySecretRef`      | object   | env fallback | `{ name, key }` of a Secret with `VAST_API_KEY`.   |

Status fields: `phase`, `instanceId`, `offerId`, `publicIp`, `sshPort`,
`actualCostPerHour`, `message`.

## License

MIT
