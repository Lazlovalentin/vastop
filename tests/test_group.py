"""VastInstanceGroup reconcile + resolver offerId-pin tests (dict-backed k8s API)."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from kubernetes.client.rest import ApiException


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


def _merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class FakeCustom:
    """Dict-backed CustomObjectsApi covering get/list/create/patch/delete."""

    def __init__(self) -> None:
        self.objs: dict[tuple[str, str], dict[str, Any]] = {}
        self.deleted: list[str] = []

    def add(self, plural: str, obj: dict[str, Any]) -> None:
        self.objs[(plural, obj["metadata"]["name"])] = obj

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        try:
            return self.objs[(plural, name)]
        except KeyError:
            raise ApiException(status=404) from None

    def list_namespaced_custom_object(
        self, group, version, namespace, plural, label_selector=None
    ):
        items = []
        for (p, _n), o in self.objs.items():
            if p != plural:
                continue
            if label_selector:
                k, v = label_selector.split("=", 1)
                if ((o.get("metadata") or {}).get("labels") or {}).get(k) != v:
                    continue
            items.append(o)
        return {"items": items}

    def create_namespaced_custom_object(self, group, version, namespace, plural, body):
        key = (plural, body["metadata"]["name"])
        if key in self.objs:
            raise ApiException(status=409)
        self.objs[key] = body
        return body

    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        _merge(self.objs[(plural, name)], body)
        return self.objs[(plural, name)]

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.objs.pop((plural, name), None)
        self.deleted.append(name)


def _order(name: str, offer_ids: list[int]) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "generation": 1},
        "status": {
            "matchingOffers": [
                {"id": oid, "pricePerHour": 0.1 * (i + 1)}
                for i, oid in enumerate(offer_ids)
            ],
            "cheapestOfferId": offer_ids[0] if offer_ids else None,
            "cheapestPricePerHour": 0.1 if offer_ids else None,
            "rentalTypeInUse": "on-demand",
        },
    }


def _group_cr(name: str, replicas: int) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": "default", "uid": "uid-123"},
        "spec": {
            "replicas": replicas,
            "templateRef": {"name": "tmpl"},
            "orderRef": {"name": "ord"},
            "apiKeySecretRef": {"name": "vastai-credentials"},
        },
    }


def _child(name: str, group: str, offer_id: int, *, instance_id=None, phase=None):
    obj: dict[str, Any] = {
        "metadata": {"name": name, "namespace": "default", "labels": {"vast.ai/group": group}},
        "spec": {"templateRef": {"name": "tmpl"}, "orderRef": {"name": "ord", "offerId": offer_id}},
        "status": {},
    }
    if instance_id is not None:
        obj["status"]["instanceId"] = instance_id
    if phase is not None:
        obj["status"]["phase"] = phase
    return obj


def _children(fake: FakeCustom) -> dict[int, dict[str, Any]]:
    """child name -> object, only the vastinstances."""
    return {
        n: o for (p, n), o in fake.objs.items() if p == "vastinstances"
    }


async def _run(monkeypatch, fake: FakeCustom, group_name: str = "grp", replicas: int = 2):
    from vastai_operator.handlers import group

    monkeypatch.setattr(group, "_custom", lambda: fake)
    patch = _Patch()
    await group.reconcile(
        spec=_group_cr(group_name, replicas)["spec"],
        patch=patch,
        namespace="default",
        name=group_name,
        uid="uid-123",
        logger=logging.getLogger("test"),
    )
    return patch


# ---------- resolver offerId pin ----------


def test_fetch_order_pick_cheapest_and_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    fake = FakeCustom()
    fake.add("vastorders", _order("ord", [10, 11, 12]))
    monkeypatch.setattr(resolver, "_custom", lambda: fake)

    cheap = resolver.fetch_order_pick("default", "ord")
    assert cheap.offer_id == 10

    pinned = resolver.fetch_order_pick("default", "ord", offer_id=12)
    assert pinned.offer_id == 12
    assert pinned.price_per_hour == pytest.approx(0.3)


def test_fetch_order_pick_pinned_gone_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import kopf

    from vastai_operator import resolver

    fake = FakeCustom()
    fake.add("vastorders", _order("ord", [10, 11]))
    monkeypatch.setattr(resolver, "_custom", lambda: fake)

    with pytest.raises(kopf.TemporaryError):
        resolver.fetch_order_pick("default", "ord", offer_id=99)


# ---------- group reconcile ----------


async def test_reconcile_creates_replicas_on_distinct_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCustom()
    fake.add("vastorders", _order("ord", [10, 11, 12]))

    patch = await _run(monkeypatch, fake, "grp", replicas=2)

    kids = _children(fake)
    assert set(kids) == {"grp-0", "grp-1"}
    offers = sorted(k["spec"]["orderRef"]["offerId"] for k in kids.values())
    assert offers == [10, 11]              # distinct, cheapest-first
    assert patch.status["replicas"] == 2
    assert patch.status["phase"] == "Scaling"


async def test_reconcile_degraded_when_too_few_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCustom()
    fake.add("vastorders", _order("ord", [10]))   # 1 offer, want 2

    patch = await _run(monkeypatch, fake, "grp", replicas=2)

    kids = _children(fake)
    assert set(kids) == {"grp-0"}                  # only one could be placed
    assert kids["grp-0"]["spec"]["orderRef"]["offerId"] == 10
    assert patch.status["phase"] == "Degraded"


async def test_reconcile_reassigns_taken_offer_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCustom()
    # grp-0 pinned to 10, but 10 left the pool (taken); pool now [11,12].
    fake.add("vastorders", _order("ord", [11, 12]))
    fake.add("vastinstances", _child("grp-0", "grp", 10))  # not launched

    await _run(monkeypatch, fake, "grp", replicas=1)

    assert fake.objs[("vastinstances", "grp-0")]["spec"]["orderRef"]["offerId"] == 11


async def test_reconcile_launched_child_keeps_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCustom()
    # grp-0 already Running on offer 10; 10 is no longer rentable (it's rented).
    fake.add("vastorders", _order("ord", [11, 12]))
    fake.add("vastinstances", _child("grp-0", "grp", 10, instance_id=999, phase="Running"))

    patch = await _run(monkeypatch, fake, "grp", replicas=1)

    # offer unchanged — a launched child is never reassigned.
    assert fake.objs[("vastinstances", "grp-0")]["spec"]["orderRef"]["offerId"] == 10
    assert patch.status["phase"] == "Ready"
    assert patch.status["readyReplicas"] == 1


async def test_reconcile_scales_down(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCustom()
    fake.add("vastorders", _order("ord", [10, 11, 12]))
    fake.add("vastinstances", _child("grp-0", "grp", 10, instance_id=1, phase="Running"))
    fake.add("vastinstances", _child("grp-1", "grp", 11, instance_id=2, phase="Running"))

    await _run(monkeypatch, fake, "grp", replicas=1)

    assert "grp-1" in fake.deleted
    assert set(_children(fake)) == {"grp-0"}
