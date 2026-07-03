"""Handlers for VastInstanceGroup — a replica set of VastInstances on one Order.

The controller owns N child VastInstance objects (via ownerReferences) and
spreads them across DISTINCT offers from the referenced VastOrder's
``status.matchingOffers`` — cheapest-first, skipping offers already pinned by a
sibling (variant B). A child whose pinned offer drops out of the pool (taken by
someone else) before it launches is reassigned to the next free offer, which
also defuses the single-instance "bad offer trap".

All lifecycle (launch, health, rollover, recreate, delete) stays in the
VastInstance controller; this module only manages the set + offer assignment.
Deleting the group garbage-collects the children, whose finalizers destroy the
Vast.ai rentals.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import kopf
from kubernetes import client as k8s_client

from ..config import CONFIG

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vastinstancegroups"
CHILD_PLURAL = "vastinstances"
LABEL = "vast.ai/group"


def _custom() -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _child_name(group: str, index: int) -> str:
    return f"{group}-{index}"


def _child_body(
    group: str,
    uid: str,
    child: str,
    namespace: str,
    spec: dict[str, Any],
    order_name: str,
    offer_id: int,
) -> dict[str, Any]:
    # instanceTemplate carries arbitrary VastInstance spec fields; the refs and
    # the pinned offer are injected here and win over anything in the template.
    child_spec: dict[str, Any] = dict(spec.get("instanceTemplate") or {})
    child_spec["templateRef"] = spec["templateRef"]
    child_spec["orderRef"] = {"name": order_name, "offerId": int(offer_id)}
    if spec.get("apiKeySecretRef"):
        child_spec["apiKeySecretRef"] = spec["apiKeySecretRef"]
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "VastInstance",
        "metadata": {
            "name": child,
            "namespace": namespace,
            "labels": {LABEL: group},
            "ownerReferences": [
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": "VastInstanceGroup",
                    "name": group,
                    "uid": uid,
                    "controller": True,
                    "blockOwnerDeletion": True,
                }
            ],
        },
        "spec": child_spec,
    }


def _list_children(namespace: str, group: str) -> dict[str, dict[str, Any]]:
    resp = _custom().list_namespaced_custom_object(
        group=GROUP,
        version=VERSION,
        namespace=namespace,
        plural=CHILD_PLURAL,
        label_selector=f"{LABEL}={group}",
    )
    return {item["metadata"]["name"]: item for item in resp.get("items", [])}


def _child_offer(child: dict[str, Any]) -> int | None:
    raw = ((child.get("spec") or {}).get("orderRef") or {}).get("offerId")
    return int(raw) if raw is not None else None


def _order_pool(namespace: str, order_name: str) -> list[int] | None:
    """Offer ids the Order currently advertises (cheapest-first), or None if the
    Order can't be read yet."""
    try:
        order = _custom().get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural="vastorders",
            name=order_name,
        )
    except k8s_client.ApiException:
        return None
    offers = (order.get("status") or {}).get("matchingOffers") or []
    return [int(o["id"]) for o in offers if o.get("id") is not None]


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(patch: kopf.Patch, **_: Any) -> None:
    patch.status["phase"] = "Pending"
    patch.status["message"] = "Initializing replica set"


@kopf.timer(GROUP, VERSION, PLURAL, interval=CONFIG.sync_interval_seconds, idle=5)
async def reconcile(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    name: str,
    uid: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    desired = max(0, int(spec.get("replicas", 1)))
    order_name = (spec.get("orderRef") or {}).get("name")
    if not order_name:
        patch.status["phase"] = "Failed"
        patch.status["message"] = "spec.orderRef.name is required"
        return

    pool_ids = _order_pool(namespace, order_name)
    if pool_ids is None:
        patch.status["phase"] = "Degraded"
        patch.status["message"] = f"VastOrder {order_name} not readable yet"
        return

    children = _list_children(namespace, name)
    desired_names = {_child_name(name, i) for i in range(desired)}

    # 1. Scale down: drop children beyond the desired index range.
    for cname in list(children):
        if cname not in desired_names:
            _delete_child(namespace, cname, logger)
            children.pop(cname, None)

    # 2. Free pool = advertised offers not already pinned by a surviving child.
    pinned = {o for c in children.values() if (o := _child_offer(c)) is not None}
    free = [oid for oid in pool_ids if oid not in pinned]

    # 3. Scale up (create missing) + heal (reassign not-yet-launched children
    #    whose pinned offer left the pool).
    created = 0
    for i in range(desired):
        cname = _child_name(name, i)
        child = children.get(cname)
        if child is None:
            if not free:
                continue  # not enough distinct offers -> Degraded below
            _create_child(namespace, name, uid, cname, spec, order_name, free.pop(0), logger)
            created += 1
            continue
        launched = bool((child.get("status") or {}).get("instanceId"))
        offer = _child_offer(child)
        if not launched and (offer is None or offer not in pool_ids) and free:
            _patch_child_offer(namespace, cname, free.pop(0), logger)

    _write_status(patch, desired, len(pool_ids), children, created)


def _write_status(
    patch: kopf.Patch,
    desired: int,
    pool_size: int,
    children: dict[str, dict[str, Any]],
    created: int,
) -> None:
    ready = sum(
        1
        for c in children.values()
        if (c.get("status") or {}).get("phase") == "Running"
    )
    patch.status["replicas"] = desired
    patch.status["readyReplicas"] = ready
    patch.status["instances"] = [
        {
            "name": c["metadata"]["name"],
            "offerId": _child_offer(c),
            "phase": (c.get("status") or {}).get("phase"),
            "instanceId": (c.get("status") or {}).get("instanceId"),
        }
        for c in sorted(children.values(), key=lambda c: c["metadata"]["name"])
    ]
    patch.status["lastSyncTime"] = _now()
    if ready >= desired:
        patch.status["phase"] = "Ready"
        patch.status["message"] = f"{ready}/{desired} replicas running"
    elif pool_size < desired:
        patch.status["phase"] = "Degraded"
        patch.status["message"] = (
            f"only {pool_size} distinct offers for {desired} replicas"
        )
    else:
        patch.status["phase"] = "Scaling"
        patch.status["message"] = (
            f"{ready}/{desired} running; {created} launching"
        )


def _create_child(
    namespace: str,
    group: str,
    uid: str,
    child: str,
    spec: dict[str, Any],
    order_name: str,
    offer_id: int,
    logger: logging.Logger,
) -> None:
    body = _child_body(group, uid, child, namespace, spec, order_name, offer_id)
    try:
        _custom().create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=CHILD_PLURAL,
            body=body,
        )
        logger.info("Created child %s on offer %s", child, offer_id)
    except k8s_client.ApiException as exc:
        if exc.status == 409:
            return  # already exists (race) — next tick reconciles
        raise kopf.TemporaryError(f"Create child {child} failed: {exc}", delay=30) from exc


def _patch_child_offer(
    namespace: str, child: str, offer_id: int, logger: logging.Logger
) -> None:
    try:
        _custom().patch_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=CHILD_PLURAL,
            name=child,
            body={"spec": {"orderRef": {"offerId": int(offer_id)}}},
        )
        logger.info("Reassigned child %s to offer %s", child, offer_id)
    except k8s_client.ApiException as exc:
        logger.warning("Reassign child %s failed: %s", child, exc)


def _delete_child(namespace: str, child: str, logger: logging.Logger) -> None:
    try:
        _custom().delete_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=CHILD_PLURAL,
            name=child,
        )
        logger.info("Deleted child %s (scale down)", child)
    except k8s_client.ApiException as exc:
        if exc.status != 404:
            logger.warning("Delete child %s failed: %s", child, exc)
