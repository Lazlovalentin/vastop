<!--
  AGENTS.md is the canonical agent guide for this repo (committed to git).
  CLAUDE.md is a symlink to this file, so Claude Code and any AGENTS.md-aware
  tool read identical instructions. Edit AGENTS.md; CLAUDE.md follows.
-->

# Agent guide (AGENTS.md / CLAUDE.md)

This file provides guidance to Claude Code (claude.ai/code) and other coding
agents when working with code in this repository.

## What this repo is

A Kubernetes operator written in Python that reconciles five custom resources
in the `vast.ai/v1alpha1` API group against the [Vast.ai](https://vast.ai) GPU
cloud:

- **`VastOrder`** — a *search query* for offers. Its controller runs the
  Vast.ai offer search on a timer and publishes the matching offer list (plus
  the cheapest) into `.status`. It never creates an instance. The spec uses
  nested filter blocks (`gpu`/`machine`/`location`/`price`/`rental`); the old
  flat fields (`gpuName`, `diskGB`, …) still work as legacy aliases, with the
  nested form winning when both are present. `rental.type: interruptible`
  searches the bid market; `rental.fallbackToOnDemand: true` reruns the search
  on-demand when zero bid offers match. The market actually used is published
  to `status.rentalTypeInUse` (+ `fellBackToOnDemand`, `effectiveBidPerHour`).
- **`VastTemplate`** — *managed server-side Vast.ai template*. Its controller
  registers a real template at Vast.ai via `create_template`, stores the
  returned `hash_id` + numeric `id` in `.status`. **On spec change, the
  controller does NOT call `update_template`** — that API path rejects updates
  from any caller whose user id wasn't injected as `creator_id` (the SDK
  doesn't populate it, so 400 every time). Instead, on spec change the
  controller `create_template`s a *new* template, writes the new hash to
  `.status`, then `delete_template`s the old one. The hash changes on every
  mutation; the Instance drift detector picks up the generation bump and
  recreates the rental.  On delete, the controller calls `delete_template`
  (uses numeric id; Vast.ai rejects hash-only delete). `VastInstance` launches
  via `template_hash`, so the Vast.ai-side template is the single source of
  truth at launch time. Resolver waits for `status.phase=Ready` before
  allowing Instance launch.
- **`VastInstance`** — the *actual rental*. Its controller resolves
  `spec.templateRef` and `spec.orderRef` (same namespace only), picks the
  cheapest offer from the Order's status, and launches an instance using the
  Template's config (placing a bid when the Order resolved to interruptible).
  Periodic timer mirrors instance state into `.status`. If `spec.healthCheck`
  is set, a *dedicated* probe timer (every `VAST_HEALTH_PROBE_INTERVAL`,
  default 24s = 5 probes per 2 minutes) probes the worker over HTTP
  (`publicIp:externalPort/path`, external port resolved from the Vast.ai
  docker port map by the sync timer) and maintains `status.workerHealthy` —
  `null` until first settled verdict, flips to `false` only after
  `failureThreshold` consecutive failures. If `spec.rollover` is set, the
  sync timer also does **graceful pre-expiry rollover**: when the rental's
  remaining lifetime (`status.rentalEndTime`, mirrored from Vast.ai
  `end_date`) drops below `beforeExpirySeconds` (default 600 = 10 min), it
  launches a *replacement* instance alongside the old one and destroys the
  old rental **only after** the replacement reports healthy (or reaches
  Running when `requireHealthy: false`/no `healthCheck`). Zero-downtime
  handoff; the failure paths (terminated/stopped/unhealthy) stay with
  VastAlert recreate.
- **`VastAlert`** — *watcher + action policy*. Its controller polls the
  referenced VastInstance's status, classifies transitions
  (InstanceTerminated, RentalExpired, InstanceFailed, InstanceStopped, plus
  WorkerUnhealthy/WorkerHealthy driven by `status.workerHealthy` flips), and
  on a fire optionally (a) POSTs to a Slack incoming webhook and
  (b) recreates the instance by calling the shared `launch_instance` helper
  and patching the VastInstance status directly. The recreate path destroys
  the old Vast.ai rental first if it still exists (matters for
  WorkerUnhealthy, where the rental is alive but the worker is dead).
- **`VastEnvVar`** — *account-level Vast.ai environment variable* (Vast.ai
  calls these "secrets", REST path `/secrets/`). One CR = one key/value entry
  on the **account**, injected into every instance launched on it. The
  controller `create_env_var`s on create, `update_env_var`s on value change,
  and on a **key rename** (`status.vastKey` != new `spec.key`) `create`s the
  new key then `delete`s the old. `on_delete` calls `delete_env_var`. Value
  comes from `spec.value` (inline) or `spec.valueFrom.secretKeyRef` (k8s
  Secret); only a `sha256` hash is written to `status.valueHash` (never the
  value). A reconcile timer (every `VAST_SYNC_INTERVAL`) recreates the entry
  if it vanished from the account and re-pushes the value when the freshly
  resolved hash drifts from `status.valueHash` — this heals a rotated source
  Secret even though kopf never sees a spec change (RBAC is `get`-only on
  Secrets, so the operator cannot watch them). The Vast.ai key is the
  identity and must be unique per account; two VastEnvVars with the same key
  fight.

Built on:

- **Kopf** (`kopf>=1.37`) — handler framework. Handlers live in
  `src/vastai_operator/handlers/` (a package, one module per kind).
  Entrypoint: `python -m vastai_operator`.
- **Vast.ai Python SDK** (`vastai`; falls back to deprecated `vastai-sdk`) —
  wrapped by `src/vastai_operator/vast_client.py`. The wrapper exists so
  handlers can be unit-tested without network access and so SDK shape
  differences are isolated in one place.

## Common commands

```bash
# Install (editable) with dev deps
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the operator against your current kubeconfig context
export VAST_API_KEY=...
kopf run -m vastai_operator.handlers --verbose --all-namespaces
# Alternative entrypoint baked into the package:
vastai-operator run --standalone --all-namespaces --verbose

# Tests (pytest-asyncio in auto mode; see pyproject.toml)
pytest
pytest tests/test_handlers.py::test_instance_on_create_resolves_refs_and_launches

# Lint + types
ruff check src tests
mypy src

# Container image
docker build -t vastai-operator:dev .

# Security scanning (Trivy)
# Filesystem: deps + secrets + IaC misconfig. The ignorefile carries the
# accepted KSV-0041 (operator must read Secrets cross-namespace, get-only).
trivy fs --scanners vuln,secret,misconfig --ignorefile .trivyignore.yaml .
# Image: --ignore-unfixed so only *fixable* base-image CVEs gate; the Python
# dependency layer must stay clean (0 vulns).
trivy image --ignore-unfixed --ignorefile .trivyignore.yaml vastai-operator:dev

# Static analysis (SonarQube). Needs a server + token; run a throwaway local one:
#   docker run -d --name sq -p 9000:9000 -e SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true sonarqube:community
# Then generate coverage and scan (config in sonar-project.properties):
pytest --cov=vastai_operator --cov-report=xml:coverage.xml
sonar-scanner -Dsonar.host.url=http://localhost:9000 -Dsonar.token=<TOKEN>
```

## Architecture

Reconciliation is split across these files; understanding all of them is
required before changing behavior:

1. **`vast_client.py`** — `VastClient` wraps the synchronous `vastai.VastAI`
   SDK. Every SDK call is dispatched through `asyncio.to_thread` because Kopf
   handlers are async and the SDK is blocking. Returns dataclasses (`Offer`,
   `InstanceState`) rather than raw dicts; `_extract_instance_id` and
   `_as_list` exist because the SDK has shifted response shapes across
   versions. Raises `VastAPIError` for any non-recoverable API failure.
   `search_offers` takes an `OfferFilters` dataclass (not kwargs);
   `OfferFilters.to_query()` builds the Vast.ai query string and
   `sdk_offer_type` maps `interruptible` → SDK `type="bid"`.
   `create_instance(bid_price_per_hour=...)` forwards the bid as the SDK
   `price` param. `InstanceState.ports` carries the docker port map
   (`{container_port: external_port}`) used by the health prober.

2. **`resolver.py`** — cross-resource lookup. `fetch_template` /
   `fetch_order_pick` use `CustomObjectsApi` to read the referenced
   `VastTemplate` / `VastOrder` and return typed dataclasses with a
   `resolved_marker` (e.g. `"tmpl-a@3"` or `"order-a@5#999"`) that is written
   to `VastInstance.status` so drift is observable. `resolve_api_key` reads
   the Secret named by `apiKeySecretRef`, with the operator's `VAST_API_KEY`
   env var as fallback; both it and `VastEnvVar` value-sourcing go through the
   shared `read_secret_value(ns, name, key)` (404 → TemporaryError, missing
   key → PermanentError).

3. **`handlers/__init__.py`** — imports the per-kind handler modules so kopf
   decorators register at startup. Owns the global `@kopf.on.startup`
   (finalizer, kubeconfig loading).

4. **`handlers/order.py`** — registers against `vastorders`.
   - `@on.create` + `@on.update(field="spec")` run a search.
   - `@kopf.timer(interval=CONFIG.order_refresh_seconds, idle=30)` is the
     primary signal; it short-circuits if `spec.refreshIntervalSeconds` says
     the last result is still fresh. The global kopf interval is the *floor*;
     per-Order spec only throttles further, never speeds up.
   - `_filters_from_spec` merges the nested blocks
     (`gpu`/`machine`/`location`/`price`/`rental`) with the legacy flat
     fields into one `OfferFilters`; nested wins.
   - Interruptible flow: search `type=bid`; if zero offers and
     `rental.fallbackToOnDemand`, rerun the same filters on-demand.
     `_effective_bid` = `rental.bidPricePerHour` else `price.maxPerHour`.
   - Status fields: `phase` (Pending/Ready/NoMatch/Failed), `matchCount`,
     `matchingOffers[]` (now incl. `minBid`, `inetUp`, `cpuCores`, `ramGB`,
     `vramGB`, `geolocation`), `cheapestOfferId`, `cheapestPricePerHour`,
     `lastSearchTime`, `rentalTypeInUse`, `fellBackToOnDemand`,
     `effectiveBidPerHour`.

5. **`handlers/instance.py`** — registers against `vastinstances`.
   - `@on.create` and `@on.update` go through `_launch_and_patch`, which
     wraps the shared `launcher.launch_instance` helper and writes
     `resolvedTemplate` / `resolvedOrder` markers.
   - `@on.update` only triggers recreation when `spec.templateRef.name` or
     `spec.orderRef.name` changes, honoring `spec.recreateOnRefChange`.
   - `@on.delete` destroys the Vast.ai instance **and** any in-flight
     `status.rolloverInstanceId` replacement, so a delete mid-rollover never
     leaks a rental.
   - `@kopf.timer` (sync_interval_seconds) does **four** jobs each tick:
     (a) `_check_template_drift` — fetch the referenced Template, compare
     its `name@generation` marker against `status.resolvedTemplate`. If the
     Template's spec was patched after launch and `spec.recreateOnTemplateUpdate`
     is true, destroy the current Vast.ai instance and call
     `_launch_and_patch` to provision a new one with the new image/env/etc.
     (b) Otherwise mirror Vast.ai live state into `.status` (incl.
     `rentalEndTime` from `end_date`).
     (c) Persist `status.healthExternalPort` (the Vast.ai port mapping for
     `healthCheck.port`) so the probe timer never needs a Vast.ai API call.
     (d) `_maybe_rollover` — pre-expiry graceful replacement (see below).
   - **`_maybe_rollover` / `_finish_rollover`** (`spec.rollover`): when a
     fixed-term rental is within `beforeExpirySeconds` of `end_date`,
     `launch_instance` provisions a replacement and its ids/markers are
     parked under `status.rollover*` (old instance untouched). On subsequent
     sync ticks `_finish_rollover` probes the replacement; once it's healthy
     (or Running when `requireHealthy: false`) it destroys the old rental and
     promotes the replacement into `instanceId`/`offerId`/`resolvedTemplate`/
     `resolvedOrder`/`lastLaunchTime`. The old rental is kept alive until the
     replacement is ready, unless it already vanished (then the replacement is
     promoted as-is). A planned id swap like this must NOT trip the alert
     classifier — see the `_classify_event` note below.
   - **Second timer `probe_health`** (`CONFIG.health_probe_seconds`, env
     `VAST_HEALTH_PROBE_INTERVAL`, default 24s → **5 probes per 2 min**):
     reads `phase`/`publicIp`/`healthExternalPort` mirrored into status,
     GETs the worker endpoint, maintains `status.workerHealthy` /
     `healthFailureCount` / `workerHealthMessage` / `lastHealthProbeTime`.
     Honors `initialDelaySeconds` measured from `status.lastLaunchTime`
     (written by `_launch_and_patch` and the alert recreate path).
     `spec.healthCheck.intervalSeconds` (default 24) only throttles further —
     same floor pattern as Order refresh. Detection latency at defaults:
     3 × 24s ≈ 72s to `workerHealthy=false`, + ≤30s alert poll to fire.

5a. **`launcher.py`** — kopf-free `launch_instance(spec, namespace)` that
    resolves Template + Order and creates the Vast.ai instance. Used by both
    the VastInstance handler and the VastAlert recreate path. Returns a
    `LaunchResult` dataclass; the caller is responsible for writing IDs into
    the owning VastInstance status (via kopf Patch or CustomObjectsApi).

5b. **`handlers/alert.py`** — registers against `vastalerts`.
    - `@on.create` initializes counters and sets phase Watching.
    - `@kopf.timer(30s)` reads the referenced VastInstance via
      `CustomObjectsApi`, calls `_classify_event` to detect a transition vs
      the last observed phase/instanceId, and respects `cooldownSeconds` so
      the same event does not fire twice in quick succession.
    - On fire: `_do_notify` reads the Slack webhook from the configured
      Secret and POSTs via `slack.send`; `_do_recreate` first destroys the
      old Vast.ai rental if `status.instanceId` still points at one, then
      calls `launch_instance` and patches the VastInstance status with the
      new instanceId so the regular Instance controller picks up from there.
    - Worker events: the watch timer records
      `lastObservedWorkerHealthy`; `_classify_event` fires `WorkerUnhealthy`
      on a `*→false` flip and `WorkerHealthy` on a `false→true` recovery.
      `null` (never probed) never fires either.
    - Recreate eligibility: every subscribed event except the
      `WorkerHealthy` recovery signal. `InstanceStopped` matters here —
      Vast.ai *pauses* outbid interruptible instances (phase Stopped, rental
      still exists), so spot keepalive = events `[InstanceStopped, …]` +
      `actions.recreate: true`; the recreate path destroys the paused rental
      before relaunching.

5f. **`handlers/envvar.py`** — registers against `vastenvvars`. CRUD an
    account-level Vast.ai env var via `VastClient.create_env_var` /
    `update_env_var` / `delete_env_var` / `list_env_vars` (the SDK's
    `/secrets/` calls). `_env_key(spec, name)` = `spec.key` else the resource
    name; `_resolve_value` reads `spec.value` or `spec.valueFrom.secretKeyRef`
    (via `resolver.read_secret_value`). `@on.create` registers + records
    `vastKey`/`valueHash`; `@on.update(field=spec)` does value-update vs
    key-rename (create-new-then-delete-old, same identity-swap reasoning as
    the Template controller); `@on.delete` removes it; the `@kopf.timer`
    reconcile recreates on external deletion and re-pushes on value-hash
    drift (heals Secret rotation). No cross-resource refs — it's standalone.

5c. **`slack.py`** — minimal incoming-webhook client. `SlackMessage`
    dataclass + `send()` (httpx POST) + `render(template, **tokens)` that
    falls back to a default message if the template is missing or rejects
    a token. The rich `render_payload` puts the Vast.ai logo
    (`VAST_LOGO_URL = https://vast.ai/apple-touch-icon.png`) as a Block Kit
    image element in the title context block — incoming webhooks ignore
    `icon_url`/`username` overrides, so an in-message image is the only way
    to brand the message. `customTitle` replaces the text next to the logo.

5d. **`health.py`** — kopf-free worker health probing. `HealthCheckConfig`
    parses `VastInstance.spec.healthCheck` (port/path/scheme/timeouts/
    thresholds); `probe(url)` returns `(healthy, detail)` where any HTTP 2xx
    counts as healthy and transport errors are unhealthy. The instance
    handler owns the threshold/initial-delay state machine.

5e. **`timeutils.py`** — `seconds_since(iso) -> float | None` shared by every
    timer throttle (order refresh, alert poll, health probe/initial-delay). A
    missing or unparseable timestamp returns `None` so callers uniformly treat
    it as "no usable prior time → don't throttle". Use this instead of
    re-inlining `try: dt.fromisoformat ... except ValueError: pass`.

6. **`config.py`** — `CONFIG` (an `OperatorConfig`) is the single source of
   runtime knobs. Reads `VAST_SYNC_INTERVAL`, `VAST_SEARCH_TIMEOUT`,
   `VAST_ORDER_REFRESH`, `VAST_HEALTH_PROBE_INTERVAL` from env. Don't add
   ad-hoc env reads elsewhere.

7. **`deploy/crds/*.yaml`** — schemas. Any new spec field must be added to
   the CRD *and* used in handlers; kopf will not surface fields the API
   server strips. `additionalPrinterColumns` is what shows up in
   `kubectl get ...`.

### How a VastInstance reaches Running

```
VastInstance.spec.{templateRef, orderRef}
    │
    ├── resolver.fetch_template(ns, name)         → TemplateSpec
    │        (image, disk, env, onstart, sshKey)
    │
    ├── resolver.fetch_order_pick(ns, name)       → OrderPick
    │        reads VastOrder.status.cheapestOfferId
    │        (raises TemporaryError if Order has no matches yet)
    │
    └── VastClient.create_instance(offer_id, …)   → instance_id
             │
             └── written to VastInstance.status.{instanceId, offerId,
                  resolvedTemplate, resolvedOrder}
```

`VastInstance` therefore depends on `VastOrder.status` being populated. If
the operator boots and an Instance is reconciled before its Order has had a
search round, `fetch_order_pick` raises `TemporaryError(delay=60)` and kopf
will retry.

### API key resolution

Order, implemented in `resolver.resolve_api_key`:

1. If `spec.apiKeySecretRef` is set, read that namespaced Secret (default key
   `VAST_API_KEY`). Missing secret → `TemporaryError` (retry); missing key →
   `PermanentError`.
2. Otherwise fall back to the operator pod's `VAST_API_KEY` env var.

### Phase semantics

`VastInstance.status.phase` is derived from Vast.ai's `actual_status` by
`_phase_from_status`. Unknown statuses map to `Creating` so the loop keeps
polling rather than declaring failure. If you add phases, update the CRD
enum and the mapping together.

`VastOrder.status.phase` is one of `Pending`, `Ready`, `NoMatch`, `Failed`
and is set directly by the handler.

## Testing conventions

- `tests/conftest.py` stubs the `vastai` module **at import time** (module
  level, not in a fixture). This must run before any test module does
  `from vastai_operator.vast_client import ...`, so don't move that stub into
  a fixture — tests will then hit the real Vast.ai API.
- Handler tests use a `_Patch` shim instead of `kopf.Patch` (kopf's Patch is
  not constructible standalone). The shim only exposes `.status` because
  that's all the handlers touch.
- Resolver tests monkeypatch `resolver._custom` to return a fake
  `CustomObjectsApi`; no kubernetes config is loaded.
- `pytest-asyncio` is in `auto` mode (`pyproject.toml`), so `async def`
  tests don't need a marker.
- `tests/test_e2e_recreate.py` is the controller-chain harness: it drives
  the *real* handler functions (`order.refresh_timer`, `instance.on_create`/
  `sync_status`, `alert.watch`) against an in-memory world — `FakeVast`
  (offers per market + provisioned instances) and `FakeCustom` (dict-backed
  CustomObjectsApi shared by resolver and alert). Only transport edges are
  faked; query building, offer picking, markers, bid propagation,
  classification and recreate run production code. Scenarios covered: host
  reclaims an on-demand instance → InstanceTerminated → recreate on the new
  cheapest offer; interruptible instance outbid (paused) → InstanceStopped →
  destroy + relaunch via on-demand fallback without a bid; reclaim with an
  empty market → recreate raises TemporaryError and succeeds once offers
  return; graceful pre-expiry rollover → replacement launched alongside,
  promoted only after its health probe passes, old rental destroyed, alert
  stays silent on the planned id swap. To add a scenario: seed CRs with
  `_seed_cr` (pass `instance_spec_extra` for healthCheck/rollover), mutate
  `FakeVast` (`reclaim`/`outbid`/`add_offer`/`remove_offer`/`set_expiry`/
  `set_ports`), then `_tick_*` in the order the real timers would fire.

## Things that will bite you

- **Kopf finalizer.** `settings.persistence.finalizer = "vast.ai/instance-finalizer"`
  is set in `handlers/__init__.py`. If you change the finalizer string,
  existing CRs in clusters will be stuck until manually patched.
- **Blocking SDK in async handler.** Never call `self._sdk.xxx()` directly
  from a handler — always go through `VastClient` so the call is dispatched
  via `asyncio.to_thread`. A blocking call freezes the entire kopf event loop.
- **Cross-namespace refs are forbidden.** Both `templateRef` and `orderRef`
  are `{name}`-only by design (RBAC simplicity). Don't add a `namespace`
  field without first expanding the ClusterRole in `deploy/rbac.yaml`.
- **Order timer cadence.** The kopf timer runs every
  `CONFIG.order_refresh_seconds` (default 300). `spec.refreshIntervalSeconds`
  only throttles further — it cannot make a single Order refresh faster than
  the global interval. To poll faster, lower the env var.
- **`on_update` for VastInstance calls `_launch` directly**, bypassing kopf's
  normal delivery. That means it shares the same `patch` object and runs in
  the same handler invocation; don't refactor `_launch` to depend on
  decorator-injected kwargs that aren't passed in `on_update`.
- **Vast.ai search query is a single string.** `OfferFilters.to_query()`
  builds a space-separated `key=value` / `key>=value` query. Adding new
  filters means extending `OfferFilters` + `to_query()` — the SDK parses the
  string with its own field whitelist (`vastai/api/query.py: offers_fields`);
  a field not in that set raises. List filters use `field in [A,B]` (no
  spaces inside brackets; underscores in GPU names become spaces SDK-side).
  RAM filters (`cpu_ram`, `gpu_ram`) are written in GB — the SDK multiplies
  by 1000 itself; do NOT pre-convert.
- **`verified=any` vs `verified=true`.** The SDK seeds
  `verified=true rentable=true external=false` defaults into every string
  query. `verifiedOnly: false` therefore must emit `verified=any` to *clear*
  the default — omitting the term keeps the default and silently filters.
- **Legacy vs nested Order fields.** `_filters_from_spec` reads the nested
  blocks first, flat fields second. The CRD defaults flat fields
  (`numGpus=1`, `diskGB=32`, `minDownloadMbps=100`, `rentalType=on-demand`),
  so a nested-only manifest still gets those defaults injected by the API
  server — that's fine because nested values, when present, always win.
- **Bid price flows through Order status, not spec.** Launcher reads
  `OrderPick.bid_price_per_hour` which comes from
  `VastOrder.status.effectiveBidPerHour`. After an on-demand fallback,
  `rentalTypeInUse=on-demand` and no bid is placed — even though
  `spec.rental.type` still says interruptible. Always branch on the status
  field, not on spec.
- **healthCheck.port is the container port.** The prober translates it to
  the external port via the instance's docker port map (`InstanceState.ports`)
  and falls back to the container port only when no mapping exists. The
  port must be EXPOSEd by the image or published via template `ports`,
  otherwise Vast.ai never maps it and probes hit a closed port until
  `failureThreshold` flips `workerHealthy=false`.
- **WorkerUnhealthy recreate destroys a *live* rental.** Unlike
  Terminated/RentalExpired (instance already gone), the unhealthy-worker
  path destroys the still-running instance before relaunching. If you add
  recreate-eligible events, check whether the old rental needs destroying.
- **Slack logo is a hotlink.** `VAST_LOGO_URL` points at
  `vast.ai/apple-touch-icon.png`; if Vast.ai moves it, messages render with
  a broken image but still deliver. Incoming webhooks ignore `icon_url`, so
  there is no webhook-level fallback.
- **Tests must not import `vastai_operator.vast_client` before conftest
  runs.** If you reorganize into subpackages, double-check load order; the
  current module-level stub in conftest depends on conftest running before
  test collection.
- **VastAlert event classification is purely heuristic** —
  `_classify_event` distinguishes RentalExpired from InstanceTerminated by
  string-matching `status.message` for "rental". If you change the message
  written by `handlers/instance.py:sync_status`, the alert classifier needs
  to be updated in lockstep or rental-expiry detection silently breaks.
- **A planned id swap must not fire InstanceTerminated.** `_classify_event`
  fires `InstanceTerminated` only when the id is *lost* (`last_id` set,
  current id falsy), NOT on a change `last_id → new valid id`. Rollover,
  template-drift recreate and alert recreate all swap `instanceId` to a fresh
  value; treating that as a termination would notify (and possibly recreate)
  on every healthy handoff. If you re-add id-change detection, exclude these
  planned-swap paths.
- **VastAlert recreate bypasses the VastInstance handler.** It calls
  `launch_instance` directly and `patch_namespaced_custom_object_status` on
  the VastInstance. The Instance handler's `@on.update` won't fire because
  spec didn't change — the Instance's own timer is what picks up the new
  instanceId. Don't add side effects that only the Instance handler runs
  (event emission, finalizer setup) on the create path without also wiring
  them into the launcher or alert recreate path.

## Operational edge cases (live cluster verified, kind 1.35.0)

These have been exercised end-to-end against a real Vast.ai account.

### Verified working

| Scenario | Observed behavior |
|---|---|
| **Instance created before Order has results** | `fetch_order_pick` raises `TemporaryError(delay=60)`. Kopf retries `on_create` every 60s. Once Order's `@kopf.timer` runs and writes `cheapestOfferId`, next Instance retry succeeds. Verified path: Order Pending → Ready (2 offers found) → Instance Resolving → Creating → Running. |
| **Instance references missing Template** | `fetch_template` raises `TemporaryError(delay=30)` (404 from k8s API). Instance phase stays `Resolving`. Log message: `Handler 'on_create' failed temporarily: VastTemplate {ns}/{name} not found`. Kopf retries indefinitely. |
| **Order with impossible `maxPricePerHour` (e.g. $0.0001)** | Vast.ai returns 0 offers. Status: `phase=NoMatch`, `matchCount=0`, `cheapestOfferId=null`. No exception. Downstream Instances using this Order get `TemporaryError` from `fetch_order_pick`. |
| **VastAlert created with `instanceRef` that points at a non-existent Instance** | `_read_instance` returns `None`. Classifier sees `last_id=None` (first observation), returns no event. Alert stays in `Watching` with `notifyCount=0`. Does NOT fire `InstanceTerminated` — that only fires after at least one observation of a real Instance. |
| **Full lifecycle: Instance launched → user `kubectl delete` → Alert fires Slack** | Sequence: (1) Instance reaches Running, Alert observes `lastObservedInstanceId=<id>`. (2) User deletes VastInstance; `on_delete` calls `destroy_instance` on Vast.ai. (3) Next Alert poll: `_read_instance` returns `None`, classifier sees prev_id=N + current=None → returns `InstanceTerminated`. (4) `_do_notify` POSTs to Slack webhook → 200 OK. notifyCount=1, phase=Triggered. |
| **VastTemplate spec change → Instance auto-recreates** | Sequence: (1) VastInstance Running with `resolvedTemplate=tmpl@1`. (2) `kubectl patch vasttemplate ... '{"spec":{"diskGB":20}}'` bumps generation to 2. (3) Within one `sync_interval_seconds` cycle (default 60s), `_check_template_drift` returns true. (4) Operator log: `VastTemplate drift detected (was tmpl@1); destroying instance N and recreating`. (5) New Vast.ai instance launched with new spec. Verified ~46s from patch to relaunch. |
| **Worker health probing against a real instance** (2026-06-12) | Image `valentynandreishyn/vast-healthcheck-test:latest` (source: `deploy/healthcheck-test/`) launched on the cheapest verified offer (GTX 1070 Ti, $0.048/hr, `runtype=args`). Vast.ai mapped the EXPOSEd port: `ports: {"8080/tcp": [{"HostPort": "50708"}]}` — exactly the shape `InstanceState._parse_ports` expects. External probe of `publicIp:50708/healthz` returned 503 during the 2-min warm-up, then 200 at ~130s uptime. Full operator probe path validated end-to-end. Instance destroyed after (~$0.005 total). |
| **Interruptible (bid) search via `OfferFilters`** (2026-06-12) | `search_offers(type="bid")` with `gpu_name in [RTX_3090,RTX_4090] gpu_ram>=20 dph_total<=0.25` returned 59 offers with `min_bid` populated (cheapest: $0.0147/hr, min_bid $0.0133). Impossible bid search (B200 ≤ $0.01) returns 0 offers — the fallback branch triggers as designed. Live *launch* of a bid instance not yet exercised (see TODO). |
| **Updated CRDs apply cleanly** (2026-06-12) | All four CRDs `kubectl apply`d to the kind cluster (context `kind-crossplane`); examples 12 + 17 pass `--dry-run=server` against the new schemas. |

### Known bugs / sharp edges

- **Vast.ai does NOT track Docker HEALTHCHECK.** Verified live (2026-06-12):
  an image with a proper `HEALTHCHECK` directive (503 → 200 after 120s) ran
  on a real instance; the full `show_instance` key inventory contains *zero*
  health/check fields, and `actual_status` stayed `running` the whole time
  the container was failing its own healthcheck. Docker-level
  healthy/starting/unhealthy state is invisible through the Vast.ai API.
  This is exactly why the operator probes the worker externally
  (`spec.healthCheck` → `health.probe` over `publicIp:externalPort`) instead
  of relying on the platform. Test harness: `deploy/healthcheck-test/`.

- **Vast.ai `update_template` is unusable.** PUT `/template/` returns 400
  `"Invalid Creator ID"` unless the payload includes `creator_id` matching
  the API key's user. The `vastai` SDK doesn't set this field, so any
  `update_template` call from a normal user 400s. Workaround in
  `handlers/template.py:on_update` — recreate the template (new hash) and
  delete the old one. Side effect: the Vast.ai-side template hash changes on
  every k8s VastTemplate spec mutation. Confirm any consumer that caches
  hashes (none today) reads `status.vastTemplateHash` afresh on each launch.

- **`runtype` is required for the instance to actually run.** Plain
  `ubuntu:22.04` (or any image without a Vast.ai-aware entrypoint) launches
  with `actual_status=running` from Vast.ai's perspective but the Vast UI
  shows "Template not found - this template is not creating" and the
  container has no PID 1. `VastTemplate.spec.runtype` (default `ssh`) is
  forwarded to `create_instance`; valid values: `ssh`, `ssh_proxy`,
  `jupyter`, `jupyter_proxy`, `args`. For `ssh` Vast.ai injects its own sshd
  and the container stays up. For `args` you must supply an `onstart` that
  blocks (e.g. `sleep infinity`) or the container exits immediately.
- **Bad offer trap.** When Vast.ai returns 400 for `create_instance` on the
  `cheapestOfferId` (offer taken, region unavailable, etc.), the operator
  retries the *same* offer until the Order's next refresh cycle (default 5
  min). It does not advance to the second-cheapest. Workaround until fixed:
  drop the Order's `refreshIntervalSeconds`, or implement offer skip on
  `create_instance` failure.
- **Instance pre-create cost.** Kopf's `@on.create` for VastInstance runs
  almost immediately after `kubectl apply`. If the spec is wrong (missing
  Template, bad image), the *first* attempt may still cause `create_instance`
  to be called on the Vast.ai side once Template+Order resolve — meaning a
  real rental can start before you notice the misconfig. Always validate the
  example manifests with `kubectl apply --dry-run=server` first.
- **kubectl status patches race kopf.** Trying to inject test state via
  `kubectl patch --subresource=status` while kopf is also writing status
  causes lost updates. For test/observation purposes, use a real Instance
  lifecycle rather than synthesizing status.

### Deployment quirks

- Default base image (`python:3.14-slim`; quirk verified on 3.11 and 3.14)
  ships with a group named `operator` already; the Dockerfile must explicitly
  create a uniquely-named user (`vastop`). Renaming back to `operator` will
  fail with `useradd: group operator exists`.
- Container verified on Python 3.14 (2026-06-12): image builds, all 98 tests
  pass inside `python:3.14-slim`, entrypoint reaches kopf startup (fails only
  on missing kubeconfig outside a cluster, as expected). Direct deps pinned
  at latest: kopf 1.44, kubernetes 36.0, vastai 1.0, pydantic 2.13,
  httpx 0.28; floors in `pyproject.toml` match. `uv lock --upgrade` +
  `uv sync --all-extras` is the upgrade path — the repo is uv-managed.
- `kopf.cli.main` is not exposed in `kopf>=1.37`. The entrypoint uses
  `kopf.run(standalone=True, clusterwide=True)` directly — do not put kopf
  CLI subcommands (`run`, `--all-namespaces`) into Deployment args; the
  module-level entrypoint already encodes those.
- `kubectl apply -f deploy/examples/vastinstance-example.yaml` includes a
  placeholder `Secret`. Apply the real Secret with `kubectl create secret
  generic vastai-credentials --from-literal=VAST_API_KEY=...` **first**, then
  apply the example with the `Secret` block filtered out, e.g.:

  ```bash
  python -c 'import yaml,sys; \
    docs=[d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d["kind"]!="Secret"]; \
    print("---\n".join(yaml.safe_dump(d) for d in docs))' \
    deploy/examples/vastinstance-example.yaml | kubectl apply -f -
  ```

### Wire-level observations

- Vast.ai SDK's `search_offers` and `create_instance` raise
  `requests.HTTPError`, not a SDK-typed exception. `VastClient` wraps these
  in `VastAPIError` so handlers can `except VastAPIError` cleanly. If you add
  a new SDK call, wrap it the same way or kopf will see an uncaught
  exception and the status patch you intended (e.g. `phase=Failed`) will
  never write.
- The wrapped `VastAPIError` message contains the raw URL with the API key
  appended as a query param. Do not log it unredacted — strip
  `?api_key=...` before forwarding messages to Slack or events.
- `show_instance` exposes `is_bid` and `min_bid` for interruptible rentals,
  and the `ports` map lists each mapping twice (IPv4 `0.0.0.0` + IPv6 `::`
  bindings with the same HostPort); `InstanceState._parse_ports` takes the
  first binding.
- For `runtype=args` you must pass the container command explicitly
  (`create_instance(..., args=[...])`); the image's own `CMD` is not enough.
  Verified live: `args=["python3", "/healthserver.py"]` →
  `status_msg: "success, running <image>"`.

## TODO (not finished in the 2026-06-12 session)

- **Live rollover e2e.** Graceful pre-expiry rollover is fully unit- and
  controller-chain-tested (`tests/test_rollover.py`,
  `tests/test_e2e_recreate.py`) but never run against a real fixed-term
  Vast.ai rental. Confirm `show_instance.end_date` is populated for an
  interruptible/reserved rental and that the 10-min window triggers a real
  replacement launch + health-gated swap. Cost: two overlapping rentals for
  the handoff window.
- **Live bid-instance launch e2e.** Bid *search* is live-verified; actually
  renting an interruptible offer (`create_instance` with `price=`),
  confirming `is_bid=true`, and observing what an outbid/pause looks like in
  `actual_status` (so `_classify_event` maps it sensibly — today it would
  land on `InstanceStopped` at best) has not been done with real money yet.
- **Guard interruptible Orders without a price.** If `rental.type:
  interruptible` is set but neither `rental.bidPricePerHour` nor
  `price.maxPerHour` is, `effectiveBidPerHour` is null and launch places no
  bid (SDK `price=None`). Add a handler-side `PermanentError` or CRD CEL
  validation requiring one of the two.
- **WorkerUnhealthy full loop against a live cluster.** Unit-tested +
  probe path live-verified, but the complete chain (operator running in
  kind → real instance goes unhealthy → Alert fires Slack → recreate
  destroys old rental and relaunches) hasn't been run end-to-end. Needs a
  real Slack webhook secret. Also visually confirm the Vast.ai-logo context
  block renders in Slack (only mock-tested).
- **Bad offer trap (pre-existing).** On `create_instance` 400 for the
  cheapest offer, advance to the next-cheapest from
  `status.matchingOffers` instead of retrying the same offer until the next
  Order refresh.
- **Pre-upgrade instances lack `status.lastLaunchTime`**, so
  `healthCheck.initialDelaySeconds` is skipped for them and probing starts
  immediately. Harmless (threshold still applies) but worth backfilling on
  first sync if it ever bites.
- **README.md (repo root)** still describes the old flat VastOrder spec; the
  examples README is updated, the root one is not.
