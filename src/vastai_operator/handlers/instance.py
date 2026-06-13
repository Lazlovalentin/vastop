"""Handlers for VastInstance — resolve Template+Order then launch a Vast.ai instance."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import kopf

from ..config import CONFIG
from ..health import HealthCheckConfig, probe
from ..launcher import LaunchError, LaunchResult, launch_instance
from ..resolver import fetch_template, resolve_api_key
from ..timeutils import seconds_since
from ..vast_client import InstanceState, VastAPIError, VastClient


@dataclass(frozen=True)
class RolloverConfig:
    """Graceful pre-expiry replacement of a rental.

    When the current rental has less than ``before_expiry_seconds`` of
    lifetime left, a replacement instance is launched alongside it. The old
    rental is destroyed only after the replacement reports healthy
    (``require_healthy`` + spec.healthCheck), or reaches Running when no
    health check is configured.
    """

    before_expiry_seconds: int = 600
    require_healthy: bool = True

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> RolloverConfig | None:
        raw = spec.get("rollover")
        # An empty `rollover: {}` block still enables the feature with
        # defaults; only an absent key or `enabled: false` disables it.
        if raw is None or raw.get("enabled") is False:
            return None
        return cls(
            before_expiry_seconds=int(raw.get("beforeExpirySeconds", 600)),
            require_healthy=bool(raw.get("requireHealthy", True)),
        )

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vastinstances"


def _client_for(spec: dict[str, Any], namespace: str) -> VastClient:
    return VastClient(api_key=resolve_api_key(spec, namespace))


def _phase_from_status(status: str) -> str:
    s = status.lower()
    if s in {"running"}:
        return "Running"
    if s in {"loading", "scheduling", "created", "starting"}:
        return "Creating"
    if s in {"stopped", "exited", "offline"}:
        return "Stopped"
    if s in {"failed", "error"}:
        return "Failed"
    return "Creating"


async def _launch_and_patch(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
) -> LaunchResult:
    patch.status["phase"] = "Resolving"
    try:
        result = await launch_instance(spec, namespace)
    except LaunchError as exc:
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    patch.status["instanceId"] = result.instance_id
    patch.status["offerId"] = result.offer_id
    patch.status["resolvedTemplate"] = result.template.resolved_marker
    patch.status["resolvedOrder"] = result.order.resolved_marker
    patch.status["phase"] = "Creating"
    patch.status["message"] = f"Provisioning from offer {result.offer_id}"
    patch.status["lastLaunchTime"] = dt.datetime.now(dt.UTC).isoformat()
    # New rental, new worker: forget previous health observations.
    patch.status["workerHealthy"] = None
    patch.status["healthFailureCount"] = 0
    patch.status["healthExternalPort"] = None
    logger.info("VastInstance launched: %s", result.instance_id)
    return result


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> dict[str, Any]:
    result = await _launch_and_patch(spec, patch, namespace, logger)
    return {"offerId": result.offer_id, "instanceId": result.instance_id}


@kopf.on.update(GROUP, VERSION, PLURAL)
async def on_update(
    spec: dict[str, Any],
    status: dict[str, Any],
    diff: kopf.Diff,
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    instance_id = status.get("instanceId")
    if not instance_id:
        return

    watched = {
        ("spec", "templateRef", "name"),
        ("spec", "orderRef", "name"),
    }
    changed = {tuple(d.field) for d in diff if d.field}
    if not changed.intersection(watched):
        return

    if not spec.get("recreateOnRefChange", True):
        logger.warning("Refs changed but recreateOnRefChange=false; ignoring")
        return

    client = _client_for(spec, namespace)
    logger.info("Refs changed (%s); destroying instance %s and recreating", changed, instance_id)
    await client.destroy_instance(int(instance_id))
    patch.status["instanceId"] = None
    patch.status["phase"] = "Resolving"
    patch.status["message"] = "Recreating after ref change"
    await _launch_and_patch(spec, patch, namespace, logger)


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def on_delete(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    instance_id = status.get("instanceId")
    rollover_id = status.get("rolloverInstanceId")
    if not instance_id and not rollover_id:
        logger.info("No instanceId in status; nothing to destroy")
        return
    client = _client_for(spec, namespace)
    if instance_id:
        await client.destroy_instance(int(instance_id))
        logger.info("Destroyed Vast.ai instance %s", instance_id)
    if rollover_id:
        # A rollover was in flight: the replacement rental must die too.
        await client.destroy_instance(int(rollover_id))
        logger.info("Destroyed in-flight rollover instance %s", rollover_id)


def _check_template_drift(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
) -> bool:
    """Return True if the referenced Template's generation has bumped since last launch."""
    if not spec.get("recreateOnTemplateUpdate", True):
        return False
    template_ref = spec.get("templateRef") or {}
    name = template_ref.get("name")
    if not name:
        return False
    try:
        template = fetch_template(namespace, name)
    except kopf.TemporaryError:
        return False
    resolved = status.get("resolvedTemplate")
    return bool(resolved) and resolved != template.resolved_marker


@kopf.timer(GROUP, VERSION, PLURAL, interval=CONFIG.sync_interval_seconds, idle=10)
async def sync_status(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    instance_id = status.get("instanceId")
    if not instance_id:
        return

    if _check_template_drift(spec, status, namespace):
        logger.info(
            "VastTemplate drift detected (was %s); destroying instance %s and recreating",
            status.get("resolvedTemplate"),
            instance_id,
        )
        client = _client_for(spec, namespace)
        try:
            await client.destroy_instance(int(instance_id))
        except VastAPIError as exc:
            logger.warning("Destroy failed during template-drift recreate: %s", exc)
        patch.status["instanceId"] = None
        patch.status["phase"] = "Resolving"
        patch.status["message"] = "Recreating after VastTemplate update"
        await _launch_and_patch(spec, patch, namespace, logger)
        return

    client = _client_for(spec, namespace)
    state: InstanceState | None = await client.get_instance(int(instance_id))
    if state is None:
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Instance {instance_id} not found on Vast.ai"
        return

    patch.status["phase"] = _phase_from_status(state.status)
    patch.status["publicIp"] = state.public_ip
    patch.status["sshPort"] = state.ssh_port
    patch.status["actualCostPerHour"] = state.dph_total
    patch.status["message"] = f"Vast status: {state.status}"
    patch.status["rentalEndTime"] = (
        dt.datetime.fromtimestamp(state.end_date, tz=dt.UTC).isoformat()
        if state.end_date
        else None
    )

    # Persist the external port for the health prober: the dedicated
    # probe_health timer runs much more often than this sync and must not
    # burn a Vast.ai API call per probe just to learn the port mapping.
    hc = HealthCheckConfig.from_spec(spec)
    if hc is not None:
        external = state.external_port(hc.port)
        if external is not None:
            patch.status["healthExternalPort"] = external

    await _maybe_rollover(spec, status, patch, state, client, namespace, logger)


async def _maybe_rollover(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    state: InstanceState,
    client: VastClient,
    namespace: str,
    logger: logging.Logger,
) -> None:
    cfg = RolloverConfig.from_spec(spec)
    if cfg is None:
        return

    if status.get("rolloverInstanceId"):
        await _finish_rollover(spec, status, patch, cfg, client, logger)
        return

    # Start a rollover only from a healthy steady state: the failure paths
    # (terminated, stopped, unhealthy) belong to VastAlert recreate.
    if _phase_from_status(state.status) != "Running" or state.end_date is None:
        return
    remaining = state.end_date - dt.datetime.now(dt.UTC).timestamp()
    if remaining > cfg.before_expiry_seconds:
        return

    logger.info(
        "Rental %s expires in %.0fs (<= %ds); launching replacement",
        state.id, remaining, cfg.before_expiry_seconds,
    )
    try:
        result = await launch_instance(spec, namespace)
    except LaunchError as exc:
        raise kopf.TemporaryError(f"Rollover launch failed: {exc}", delay=60) from exc

    patch.status["rolloverInstanceId"] = result.instance_id
    patch.status["rolloverOfferId"] = result.offer_id
    patch.status["rolloverLaunchTime"] = dt.datetime.now(dt.UTC).isoformat()
    patch.status["rolloverResolvedTemplate"] = result.template.resolved_marker
    patch.status["rolloverResolvedOrder"] = result.order.resolved_marker
    patch.status["message"] = (
        f"Rollover in progress: replacement {result.instance_id} launched, "
        f"waiting for it to become healthy"
    )


async def _replacement_ready(
    new_state: InstanceState,
    hc: HealthCheckConfig | None,
    require_healthy: bool,
    new_id: int,
    logger: logging.Logger,
) -> tuple[bool, int | None]:
    """Is the rollover replacement safe to promote? Returns (ready, externalPort).

    Ready = Running, and — when health-gated — passing its probe. The external
    port is returned even on an unhealthy probe so the caller can persist it.
    """
    if _phase_from_status(new_state.status) != "Running":
        return False, None
    if not (require_healthy and hc is not None):
        return True, None
    if not new_state.public_ip:
        return False, None
    external = new_state.external_port(hc.port) or hc.port
    healthy, detail = await probe(
        hc.url(new_state.public_ip, external), timeout_seconds=hc.timeout_seconds
    )
    if not healthy:
        logger.info("Rollover replacement %s not healthy yet: %s", new_id, detail)
    return healthy, external


async def _finish_rollover(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    cfg: RolloverConfig,
    client: VastClient,
    logger: logging.Logger,
) -> None:
    """Promote the replacement once it's ready; destroy the old rental.

    The old rental is kept alive until the replacement reports healthy —
    unless the old one already expired, in which case the replacement is
    promoted as-is (better a booting worker than none).
    """
    new_id = int(status["rolloverInstanceId"])
    new_state = await client.get_instance(new_id)
    if new_state is None:
        logger.warning("Rollover replacement %s disappeared; will relaunch", new_id)
        _clear_rollover(patch)
        return

    hc = HealthCheckConfig.from_spec(spec)
    health_gated = cfg.require_healthy and hc is not None
    ready, new_external = await _replacement_ready(
        new_state, hc, cfg.require_healthy, new_id, logger
    )

    raw_old_id = status.get("instanceId")
    old_id = int(raw_old_id) if raw_old_id else None
    old_state = await client.get_instance(old_id) if old_id is not None else None

    if not ready and old_state is not None:
        return  # old still alive; keep waiting for the replacement

    if old_state is not None and old_id is not None:
        await client.destroy_instance(old_id)
        logger.info("Rollover complete: destroyed old rental %s", old_id)
    else:
        logger.warning(
            "Old rental %s already gone; promoting replacement %s immediately",
            old_id, new_id,
        )

    patch.status["instanceId"] = new_id
    patch.status["offerId"] = status.get("rolloverOfferId")
    patch.status["resolvedTemplate"] = status.get("rolloverResolvedTemplate")
    patch.status["resolvedOrder"] = status.get("rolloverResolvedOrder")
    patch.status["lastLaunchTime"] = status.get("rolloverLaunchTime")
    patch.status["phase"] = _phase_from_status(new_state.status)
    patch.status["publicIp"] = new_state.public_ip
    patch.status["sshPort"] = new_state.ssh_port
    patch.status["workerHealthy"] = True if ready and health_gated else None
    patch.status["healthFailureCount"] = 0
    patch.status["healthExternalPort"] = new_external
    patch.status["message"] = f"Rolled over to instance {new_id} before rental expiry"
    _clear_rollover(patch)


def _clear_rollover(patch: kopf.Patch) -> None:
    patch.status["rolloverInstanceId"] = None
    patch.status["rolloverOfferId"] = None
    patch.status["rolloverLaunchTime"] = None
    patch.status["rolloverResolvedTemplate"] = None
    patch.status["rolloverResolvedOrder"] = None


@kopf.timer(GROUP, VERSION, PLURAL, interval=CONFIG.health_probe_seconds, idle=10)
async def probe_health(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Dedicated worker health prober.

    Runs at CONFIG.health_probe_seconds (default 24s → 5 probes per 2 min);
    spec.healthCheck.intervalSeconds can only throttle further. Reads
    publicIp/phase/healthExternalPort mirrored into status by sync_status, so
    a probe costs one HTTP GET to the worker and zero Vast.ai API calls.
    """
    hc = HealthCheckConfig.from_spec(spec)
    if hc is None or not status.get("instanceId"):
        return
    await _probe_worker(hc, status, patch, logger)


async def _probe_worker(
    hc: HealthCheckConfig,
    status: dict[str, Any],
    patch: kopf.Patch,
    logger: logging.Logger,
) -> None:
    """Probe the worker's health endpoint and settle the verdict.

    workerHealthy transitions: None (not yet probed / in initial delay) →
    True on the first 2xx → False only after failureThreshold consecutive
    failures, so a single network blip doesn't fire an alert.
    """
    public_ip = status.get("publicIp")
    if status.get("phase") != "Running" or not public_ip:
        return

    now = dt.datetime.now(dt.UTC)

    if hc.initial_delay_seconds > 0:
        since_launch = seconds_since(status.get("lastLaunchTime"))
        if since_launch is not None and since_launch < hc.initial_delay_seconds:
            return

    # Per-instance throttle: the kopf timer is the floor, spec only slows.
    since_probe = seconds_since(status.get("lastHealthProbeTime"))
    if since_probe is not None and since_probe < hc.interval_seconds - 1:
        return

    external = int(status.get("healthExternalPort") or hc.port)
    url = hc.url(str(public_ip), external)
    healthy, detail = await probe(url, timeout_seconds=hc.timeout_seconds)

    patch.status["lastHealthProbeTime"] = now.isoformat()
    if healthy:
        patch.status["workerHealthy"] = True
        patch.status["healthFailureCount"] = 0
        patch.status["workerHealthMessage"] = detail
        return

    failures = int(status.get("healthFailureCount") or 0) + 1
    patch.status["healthFailureCount"] = failures
    patch.status["workerHealthMessage"] = detail
    if failures >= hc.failure_threshold:
        patch.status["workerHealthy"] = False
        logger.warning(
            "Worker unhealthy on %s (%d consecutive failures): %s", url, failures, detail
        )
