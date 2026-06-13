"""Shared launch logic used by both the VastInstance handler and VastAlert recreate.

Keep this module free of kopf-specific objects (Patch, Diff, etc.) so it can
be invoked from any controller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .resolver import (
    OrderPick,
    TemplateSpec,
    fetch_order_pick,
    fetch_template,
    resolve_api_key,
)
from .vast_client import VastAPIError, VastClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaunchResult:
    instance_id: int
    offer_id: int
    template: TemplateSpec
    order: OrderPick


class LaunchError(RuntimeError):
    """Raised when launching a Vast.ai instance from a VastInstance spec fails."""


def _merge_env(template: TemplateSpec, spec: dict[str, Any]) -> dict[str, str]:
    merged = dict(template.env)
    merged.update(dict(spec.get("envOverrides") or {}))
    return merged


async def launch_instance(spec: dict[str, Any], namespace: str) -> LaunchResult:
    """Resolve refs, pick cheapest offer, create Vast.ai instance.

    Caller is responsible for writing the returned IDs onto the owning
    VastInstance status (via kopf patch or CustomObjectsApi).
    """
    template_ref = spec["templateRef"]
    order_ref = spec["orderRef"]

    template = fetch_template(namespace, template_ref["name"])
    pick = fetch_order_pick(namespace, order_ref["name"])

    api_key = resolve_api_key(spec, namespace)
    client = VastClient(api_key=api_key)
    try:
        instance_id = await client.create_instance(
            offer_id=pick.offer_id,
            image=None if template.template_hash_id else template.image,
            disk_gb=template.disk_gb,
            env=_merge_env(template, spec),
            onstart=None if template.template_hash_id else template.onstart,
            ssh_key=template.ssh_key,
            runtype=template.runtype,
            template_hash_id=template.template_hash_id,
            bid_price_per_hour=(
                pick.bid_price_per_hour if pick.rental_type == "interruptible" else None
            ),
        )
    except VastAPIError as exc:
        raise LaunchError(f"Vast.ai create failed: {exc}") from exc

    logger.info(
        "Launched VastInstance ns=%s template=%s order=%s offer=%s id=%s",
        namespace,
        template.resolved_marker,
        pick.resolved_marker,
        pick.offer_id,
        instance_id,
    )
    return LaunchResult(
        instance_id=instance_id,
        offer_id=pick.offer_id,
        template=template,
        order=pick,
    )
