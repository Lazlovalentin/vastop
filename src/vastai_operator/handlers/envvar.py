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

Value sources (exactly one): spec.value (inline), spec.valueFrom.secretKeyRef
(whole value from a Secret), or spec.valueTemplate — a template string with
$(VAR) placeholders filled from Secrets, for values that mix public and secret
parts (e.g. a connection URL with an embedded password).

The Vast.ai-side key is the identity; status.vastKey tracks it so a spec rename
can delete the old entry. Values are never written to status — only a sha256
hash (status.valueHash) so drift is observable without leaking the secret.
"""

from __future__ import annotations

import base64
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


def _apply_encoding(spec: dict[str, Any], value: str) -> str:
    """Encode the resolved value for transport to Vast.ai.

    The /secrets/ API rejects any value containing a newline: a two-line value
    returns 400 Bad Request while the same value on one line is accepted. That
    makes PEM material - certificates and private keys - impossible to deliver
    verbatim, so base64 is offered as an explicit opt-in and whatever consumes
    the variable on the instance decodes it again.
    """
    encoding = str(spec.get("encoding") or "none")
    if encoding == "none":
        return value
    if encoding == "base64":
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    raise kopf.PermanentError(f"unsupported spec.encoding {encoding!r}")


def _resolve_value(spec: dict[str, Any], namespace: str) -> str:
    """Inline spec.value, else spec.valueTemplate, else spec.valueFrom.secretKeyRef.

    The result is what gets pushed to Vast.ai, encoding included, so the value
    hash recorded in status describes the transported form and drift detection
    stays consistent across create, update and reconcile.
    """
    if spec.get("value") is not None:
        return _apply_encoding(spec, str(spec["value"]))
    template = spec.get("valueTemplate")
    if template:
        value = str(template["template"])
        for var in template.get("vars") or []:
            ref = var["secretKeyRef"]
            secret = read_secret_value(namespace, ref["name"], ref["key"])
            value = value.replace(f"$({var['name']})", secret)
        return _apply_encoding(spec, value)
    ref = (spec.get("valueFrom") or {}).get("secretKeyRef")
    if not ref:
        raise kopf.PermanentError(
            "VastEnvVar requires spec.value, spec.valueFrom.secretKeyRef"
            " or spec.valueTemplate"
        )
    return _apply_encoding(
        spec, read_secret_value(namespace, ref["name"], ref["key"])
    )


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


@kopf.on.delete(GROUP, VERSION, PLURAL, retries=3)
async def on_delete(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Remove the account-level entry, without ever wedging the resource.

    status.vastKey is written only after a create succeeded, so its absence
    means there is almost certainly nothing on the account to remove. The delete
    is still attempted, in case the status patch was lost after a successful
    create, but its failure is not allowed to block deletion: /secrets/ answers
    400 - not 404 - for a key that does not exist, so _is_not_found cannot
    swallow it and the finalizer would hold the resource in Terminating
    forever. A resource that did reach Ready retries a few times first, so a
    transient API failure does not orphan the entry.
    """
    client = _client_for(spec, namespace)
    recorded = status.get("vastKey")
    key = recorded or _env_key(spec, name)

    if recorded:
        await client.delete_env_var(key)
        logger.info("Deleted Vast.ai env var %s", key)
        return

    try:
        await client.delete_env_var(key)
    except VastAPIError as exc:
        logger.warning(
            "Vast.ai env var %s was never registered, nothing to delete: %s",
            key,
            exc,
        )
    else:
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
