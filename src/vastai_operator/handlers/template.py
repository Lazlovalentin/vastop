"""Handlers for VastTemplate — manage a server-side Vast.ai template.

Lifecycle:
  on_create     → SDK create_template, save hash_id + numeric id to status.
  on_update     → SDK update_template against status.vastTemplateHash.
  on_delete     → SDK delete_template using status.vastTemplateHash.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from typing import Any

import kopf

from ..config import CONFIG
from ..resolver import resolve_api_key
from ..vast_client import VastAPIError, VastClient

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vasttemplates"


def _client_for(spec: dict[str, Any], namespace: str) -> VastClient:
    return VastClient(api_key=resolve_api_key(spec, namespace))


def _extract_hash(raw: dict[str, Any]) -> tuple[str | None, int | None]:
    """Vast.ai responses sometimes nest the new template; pull hash + id robustly."""
    nodes = [raw]
    if isinstance(raw.get("template"), dict):
        nodes.append(raw["template"])
    if isinstance(raw.get("new"), dict):
        nodes.append(raw["new"])
    hash_id: str | None = None
    template_id: int | None = None
    for node in nodes:
        if hash_id is None:
            hash_id = node.get("hash_id") or node.get("template_hash") or node.get("hash")
        if template_id is None and node.get("id") is not None:
            with contextlib.suppress(TypeError, ValueError):
                template_id = int(node["id"])
    return hash_id, template_id


def _kwargs_from_spec(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "image": spec["image"],
        "runtype": spec.get("runtype", "ssh"),
        "disk_gb": int(spec.get("diskGB", 32)),
        "env": dict(spec.get("env") or {}),
        "ports": [int(p) for p in (spec.get("ports") or [])],
        "onstart": spec.get("onstart"),
        "description": spec.get("description"),
        "private": bool(spec.get("private", True)),
        "image_login": spec.get("imageLogin"),
    }


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(
    spec: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    client = _client_for(spec, namespace)
    patch.status["phase"] = "Pending"
    try:
        raw = await client.create_template(**_kwargs_from_spec(name, spec))
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    hash_id, template_id = _extract_hash(raw)
    if not hash_id:
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Vast.ai response missing hash: {raw!r}"
        raise kopf.TemporaryError("missing hash in create_template response", delay=60)

    patch.status["phase"] = "Ready"
    patch.status["vastTemplateHash"] = hash_id
    if template_id is not None:
        patch.status["vastTemplateId"] = template_id
    patch.status["syncedGeneration"] = int(meta.get("generation", 1))
    patch.status["lastSyncTime"] = dt.datetime.now(dt.UTC).isoformat()
    patch.status["message"] = f"Created Vast.ai template {hash_id}"
    logger.info("Created Vast.ai template hash=%s id=%s", hash_id, template_id)


@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
async def on_update(
    spec: dict[str, Any],
    status: dict[str, Any],
    meta: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    """Replace strategy: Vast.ai's PUT /template rejects updates from anyone but
    the original creator id, and the SDK doesn't populate that field. To keep
    behaviour predictable we *recreate* the Vast.ai template (new hash) on every
    spec change, then delete the old one. The Instance controller's drift
    detector picks up the new generation marker and recreates the rental.
    """
    old_hash = status.get("vastTemplateHash")
    old_id = status.get("vastTemplateId")
    client = _client_for(spec, namespace)

    try:
        raw = await client.create_template(**_kwargs_from_spec(name, spec))
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = str(exc)
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    new_hash, new_id = _extract_hash(raw)
    if not new_hash:
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Vast.ai response missing hash: {raw!r}"
        raise kopf.TemporaryError("missing hash in create_template response", delay=60)

    patch.status["phase"] = "Ready"
    patch.status["vastTemplateHash"] = new_hash
    if new_id is not None:
        patch.status["vastTemplateId"] = new_id
    patch.status["syncedGeneration"] = int(meta.get("generation", 0))
    patch.status["lastSyncTime"] = dt.datetime.now(dt.UTC).isoformat()
    patch.status["message"] = f"Replaced Vast.ai template → {new_hash}"
    logger.info(
        "Replaced Vast.ai template: old=%s/%s → new=%s/%s",
        old_hash, old_id, new_hash, new_id,
    )

    if old_hash or old_id:
        try:
            await client.delete_template(hash_id=old_hash, template_id=old_id)
        except VastAPIError as exc:
            # Old template lingers but new one is valid; log and continue.
            logger.warning("Could not delete old template %s/%s: %s", old_hash, old_id, exc)


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def on_delete(
    spec: dict[str, Any],
    status: dict[str, Any],
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    hash_id = status.get("vastTemplateHash")
    template_id = status.get("vastTemplateId")
    if not hash_id and not template_id:
        logger.info("No vastTemplateHash/Id in status; nothing to delete on Vast.ai")
        return
    client = _client_for(spec, namespace)
    await client.delete_template(hash_id=hash_id, template_id=template_id)
    logger.info("Deleted Vast.ai template hash=%s id=%s", hash_id, template_id)
