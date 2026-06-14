"""Handlers for VastEnvVar — manage an account-level Vast.ai environment variable.

Vast.ai exposes a per-account key/value store ("environment variables" /
"secrets", REST path /secrets/) that is injected into every instance launched on
the account. Each VastEnvVar custom resource owns exactly one such entry.

Lifecycle:
  on_create  → SDK create_env_var(key, value); record key + value hash in status.
  on_update  → value change → update_env_var; key rename → delete old + create new.
  on_delete  → SDK delete_env_var(key).
  reconcile  → timer recreates the entry if it vanished from the account and
               re-pushes the value when the (possibly Secret-sourced) value drifts
               from the last-synced hash.

The Vast.ai-side key is the identity; status.vastKey tracks it so a spec rename
can delete the old entry. Values are never written to status — only a sha256
hash (status.valueHash) so drift is observable without leaking the secret.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Any

import kopf

from ..config import CONFIG
from ..resolver import read_secret_value, resolve_api_key
from ..vast_client import VastAPIError, VastClient

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vastenvvars"


def _client_for(spec: dict[str, Any], namespace: str) -> VastClient:
    return VastClient(api_key=resolve_api_key(spec, namespace))


def _env_key(spec: dict[str, Any], name: str) -> str:
    """Vast.ai-side variable name; defaults to the resource name."""
    return str(spec.get("key") or name)


def _resolve_value(spec: dict[str, Any], namespace: str) -> str:
    """Inline spec.value, else spec.valueFrom.secretKeyRef."""
    if spec.get("value") is not None:
        return str(spec["value"])
    ref = (spec.get("valueFrom") or {}).get("secretKeyRef")
    if not ref:
        raise kopf.PermanentError(
            "VastEnvVar requires spec.value or spec.valueFrom.secretKeyRef"
        )
    return read_secret_value(namespace, ref["name"], ref["key"])


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    key = _env_key(spec, name)
    value = _resolve_value(spec, namespace)
    client = _client_for(spec, namespace)
    patch.status["phase"] = "Pending"
    try:
        await client.create_env_var(key, value)
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    patch.status["phase"] = "Ready"
    patch.status["vastKey"] = key
    patch.status["valueHash"] = _value_hash(value)
    patch.status["lastSyncTime"] = _now()
    patch.status["message"] = f"Created Vast.ai env var {key}"
    logger.info("Created Vast.ai env var %s", key)


@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
async def on_update(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    new_key = _env_key(spec, name)
    old_key = status.get("vastKey") or new_key
    value = _resolve_value(spec, namespace)
    new_hash = _value_hash(value)
    client = _client_for(spec, namespace)

    try:
        if new_key != old_key:
            # Rename: the Vast.ai key is the identity, so a swap = create new + delete old.
            await client.create_env_var(new_key, value)
            await client.delete_env_var(old_key)
            msg = f"Renamed Vast.ai env var {old_key} -> {new_key}"
        elif new_hash != status.get("valueHash"):
            await client.update_env_var(new_key, value)
            msg = f"Updated Vast.ai env var {new_key}"
        else:
            msg = f"Vast.ai env var {new_key} already in sync"
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    patch.status["phase"] = "Ready"
    patch.status["vastKey"] = new_key
    patch.status["valueHash"] = new_hash
    patch.status["lastSyncTime"] = _now()
    patch.status["message"] = msg
    logger.info(msg)


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def on_delete(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    key = status.get("vastKey") or _env_key(spec, name)
    client = _client_for(spec, namespace)
    await client.delete_env_var(key)
    logger.info("Deleted Vast.ai env var %s", key)


@kopf.timer(GROUP, VERSION, PLURAL, interval=CONFIG.sync_interval_seconds, idle=30)
async def reconcile(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Re-assert desired state: recreate if the key vanished, re-push on value drift.

    The account listing masks values, so drift is detected by comparing the
    freshly-resolved value hash against status.valueHash (what we last pushed),
    not by reading the value back. This also heals a rotated source Secret —
    spec doesn't change, so on_update never fires, but the resolved hash does.
    """
    if status.get("phase") not in ("Ready", "Failed"):
        return  # let create/update settle first

    key = status.get("vastKey") or _env_key(spec, name)
    value = _resolve_value(spec, namespace)
    desired_hash = _value_hash(value)
    client = _client_for(spec, namespace)

    try:
        existing = await client.list_env_vars()
    except VastAPIError as exc:
        logger.warning("env var reconcile list failed: %s", exc)
        return

    try:
        if key not in existing:
            await client.create_env_var(key, value)
            action = f"Recreated drifted Vast.ai env var {key}"
        elif desired_hash != status.get("valueHash"):
            await client.update_env_var(key, value)
            action = f"Re-synced Vast.ai env var {key} value"
        else:
            if status.get("phase") != "Ready":
                patch.status["phase"] = "Ready"
            return
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    patch.status["phase"] = "Ready"
    patch.status["vastKey"] = key
    patch.status["valueHash"] = desired_hash
    patch.status["lastSyncTime"] = _now()
    patch.status["message"] = action
    logger.info(action)
