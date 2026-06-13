"""Handlers for VastAlert — watch a VastInstance and react to lifecycle events.

The alert controller runs a periodic timer that:
  1. Reads the referenced VastInstance via the k8s API.
  2. Compares its current ``status.phase`` / ``status.instanceId`` to what we
     last observed (stored in ``VastAlert.status``).
  3. Classifies any change as one of the events from ``spec.events`` and, if
     within cooldown, ignores it. Otherwise fires the configured actions.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from typing import Any

import kopf
from kubernetes import client as k8s_client

from .. import slack
from ..config import CONFIG
from ..launcher import LaunchError, launch_instance
from ..resolver import resolve_api_key
from ..timeutils import seconds_since
from ..vast_client import VastAPIError, VastClient

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vastalerts"


def _custom() -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi()


def _read_instance(namespace: str, name: str) -> dict[str, Any] | None:
    try:
        return _custom().get_namespaced_custom_object(
            group=CONFIG.api_group,
            version=CONFIG.api_version,
            namespace=namespace,
            plural="vastinstances",
            name=name,
        )
    except k8s_client.ApiException as exc:
        if exc.status == 404:
            return None
        raise kopf.TemporaryError(
            f"Cannot read VastInstance {namespace}/{name}: {exc}", delay=30
        ) from exc


def _read_webhook(namespace: str, ref: dict[str, Any]) -> str:
    secret_name = ref["name"]
    secret_key = ref.get("key", "SLACK_WEBHOOK_URL")
    core = k8s_client.CoreV1Api()
    try:
        secret = core.read_namespaced_secret(name=secret_name, namespace=namespace)
    except k8s_client.ApiException as exc:
        raise kopf.TemporaryError(
            f"Cannot read Slack secret {namespace}/{secret_name}: {exc}", delay=30
        ) from exc
    raw = (secret.data or {}).get(secret_key)
    if not raw:
        raise kopf.PermanentError(
            f"Secret {namespace}/{secret_name} missing key {secret_key}"
        )
    return base64.b64decode(raw).decode("utf-8").strip()


def _classify_failed_phase(
    cur_message: str, last_phase: Any, enabled: set[str]
) -> str | None:
    """Disambiguate a Failed phase into Terminated / RentalExpired / Failed."""
    if "not found on vast.ai" in cur_message and "InstanceTerminated" in enabled:
        return "InstanceTerminated"
    if "rental" in cur_message and "RentalExpired" in enabled:
        return "RentalExpired"
    if "InstanceFailed" in enabled and last_phase != "Failed":
        return "InstanceFailed"
    return None


def _classify_worker_health(
    status: dict[str, Any], prev: dict[str, Any], enabled: set[str]
) -> str | None:
    """Fire on a workerHealthy flip. `None` (never probed) fires nothing."""
    cur = status.get("workerHealthy")
    last = prev.get("lastObservedWorkerHealthy")
    if cur is False and last is not False and "WorkerUnhealthy" in enabled:
        return "WorkerUnhealthy"
    if cur is True and last is False and "WorkerHealthy" in enabled:
        return "WorkerHealthy"
    return None


def _classify_event(
    enabled: set[str],
    prev: dict[str, Any],
    current_instance: dict[str, Any] | None,
) -> str | None:
    """Return the event name to fire, or None if nothing meaningful happened."""
    last_phase = prev.get("lastObservedPhase")
    last_id = prev.get("lastObservedInstanceId")

    if current_instance is None:
        if last_id is not None and "InstanceTerminated" in enabled:
            return "InstanceTerminated"
        return None

    status = current_instance.get("status") or {}
    cur_phase = status.get("phase")
    cur_id = status.get("instanceId")

    if cur_phase == "Failed":
        event = _classify_failed_phase(
            (status.get("message") or "").lower(), last_phase, enabled
        )
        if event:
            return event

    if cur_phase == "Stopped" and last_phase != "Stopped" and "InstanceStopped" in enabled:
        return "InstanceStopped"

    # Lost id entirely → terminated. An id *change* to a new valid id is a
    # planned replacement (rollover / drift / alert recreate) and must not fire.
    if last_id and not cur_id and "InstanceTerminated" in enabled:
        return "InstanceTerminated"

    return _classify_worker_health(status, prev, enabled)


def _within_cooldown(
    status: dict[str, Any], cooldown_seconds: int, event: str
) -> bool:
    if cooldown_seconds <= 0:
        return False
    last_event = status.get("lastEvent")
    last_time = status.get("lastEventTime")
    if last_event != event or not last_time:
        return False
    try:
        last_dt = dt.datetime.fromisoformat(last_time)
    except ValueError:
        return False
    return (dt.datetime.now(dt.UTC) - last_dt).total_seconds() < cooldown_seconds


async def _do_notify(
    *,
    spec: dict[str, Any],
    namespace: str,
    event: str,
    instance_name: str,
    instance_obj: dict[str, Any] | None,
    logger: logging.Logger,
) -> None:
    webhook = _read_webhook(namespace, spec["slackWebhookSecretRef"])
    status = (instance_obj or {}).get("status") or {}
    ctx = slack.AlertContext(
        event=event,
        instance=instance_name,
        namespace=namespace,
        instance_id=status.get("instanceId"),
        public_ip=status.get("publicIp"),
        phase=status.get("phase"),
        cluster=spec.get("clusterName"),
        custom_title=spec.get("customTitle"),
        custom_summary_template=spec.get("customSummaryTemplate"),
    )
    payload = slack.render_payload(ctx)
    try:
        await slack.send_payload(webhook, payload)
        logger.info("Slack notify ok for event %s on %s", event, instance_name)
    except slack.SlackError as exc:
        raise kopf.TemporaryError(f"Slack send failed: {exc}", delay=60) from exc


async def _do_recreate(
    *,
    namespace: str,
    instance_name: str,
    instance_obj: dict[str, Any] | None,
    logger: logging.Logger,
) -> int | None:
    if instance_obj is None:
        logger.warning("Cannot recreate: VastInstance %s no longer exists", instance_name)
        return None
    instance_spec = instance_obj.get("spec") or {}

    # The old rental may still be running (e.g. WorkerUnhealthy fires while
    # the container is up). Destroy it first so we don't pay for two.
    old_id = (instance_obj.get("status") or {}).get("instanceId")
    if old_id:
        try:
            client = VastClient(api_key=resolve_api_key(instance_spec, namespace))
            await client.destroy_instance(int(old_id))
            logger.info("Destroyed stale Vast.ai instance %s before recreate", old_id)
        except VastAPIError as exc:
            logger.warning("Destroy of old instance %s failed: %s", old_id, exc)

    try:
        result = await launch_instance(instance_spec, namespace)
    except LaunchError as exc:
        raise kopf.TemporaryError(f"Recreate failed: {exc}", delay=60) from exc

    body = {
        "status": {
            "instanceId": result.instance_id,
            "offerId": result.offer_id,
            "phase": "Creating",
            "resolvedTemplate": result.template.resolved_marker,
            "resolvedOrder": result.order.resolved_marker,
            "message": f"Recreated by VastAlert from offer {result.offer_id}",
            "lastLaunchTime": dt.datetime.now(dt.UTC).isoformat(),
            "workerHealthy": None,
            "healthFailureCount": 0,
        }
    }
    _custom().patch_namespaced_custom_object_status(
        group=CONFIG.api_group,
        version=CONFIG.api_version,
        namespace=namespace,
        plural="vastinstances",
        name=instance_name,
        body=body,
    )
    logger.info("Recreated VastInstance %s as Vast instance %s", instance_name, result.instance_id)
    return result.instance_id


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(
    patch: kopf.Patch,
    **_: Any,
) -> None:
    patch.status["phase"] = "Watching"
    patch.status["notifyCount"] = 0
    patch.status["recreateCount"] = 0


@kopf.timer(
    GROUP,
    VERSION,
    PLURAL,
    interval=30.0,
    idle=10,
)
async def watch(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    instance_name = spec["instanceRef"]["name"]
    interval = float(spec.get("pollIntervalSeconds", 30))

    # Honor per-alert poll throttle (kopf timer is a 30s floor; spec only slows).
    elapsed = seconds_since(status.get("lastEventTime") or status.get("lastObservedAt"))
    if elapsed is not None and elapsed < interval - 1:
        return

    instance_obj = _read_instance(namespace, instance_name)
    instance_status = (instance_obj or {}).get("status") or {}

    enabled = set(spec.get("events") or ["InstanceTerminated", "RentalExpired"])
    event = _classify_event(enabled, status, instance_obj)

    patch.status["lastObservedAt"] = dt.datetime.now(dt.UTC).isoformat()
    patch.status["lastObservedPhase"] = instance_status.get("phase")
    patch.status["lastObservedInstanceId"] = instance_status.get("instanceId")
    patch.status["lastObservedWorkerHealthy"] = instance_status.get("workerHealthy")

    if event is None:
        patch.status["phase"] = "Watching"
        return

    cooldown = int(spec.get("cooldownSeconds", 300))
    if _within_cooldown(status, cooldown, event):
        logger.debug("Event %s suppressed by cooldown", event)
        return

    await _fire_actions(
        spec=spec,
        status=status,
        patch=patch,
        event=event,
        instance_name=instance_name,
        instance_obj=instance_obj,
        namespace=namespace,
        logger=logger,
    )


async def _fire_actions(
    *,
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    event: str,
    instance_name: str,
    instance_obj: dict[str, Any] | None,
    namespace: str,
    logger: logging.Logger,
) -> None:
    """Run the configured notify / recreate actions for a fired event."""
    actions = spec.get("actions") or {}
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    patch.status["phase"] = "Triggered"
    patch.status["lastEvent"] = event
    patch.status["lastEventTime"] = now_iso
    patch.status["message"] = f"Fired {event} for instance {instance_name}"

    if bool(actions.get("notify", True)):
        await _do_notify(
            spec=spec,
            namespace=namespace,
            event=event,
            instance_name=instance_name,
            instance_obj=instance_obj,
            logger=logger,
        )
        patch.status["lastNotifyTime"] = now_iso
        patch.status["notifyCount"] = int(status.get("notifyCount") or 0) + 1

    # Every subscribed event is recreate-eligible except the WorkerHealthy
    # recovery signal. Notably InstanceStopped: Vast.ai PAUSES outbid
    # interruptible instances instead of destroying them, so spot keepalive
    # alerts rely on recreate firing for Stopped.
    if bool(actions.get("recreate", False)) and event != "WorkerHealthy":
        new_id = await _do_recreate(
            namespace=namespace,
            instance_name=instance_name,
            instance_obj=instance_obj,
            logger=logger,
        )
        if new_id is not None:
            patch.status["lastRecreateTime"] = now_iso
            patch.status["recreateCount"] = int(status.get("recreateCount") or 0) + 1
            patch.status["lastObservedInstanceId"] = new_id
