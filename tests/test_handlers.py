"""Tests for VastInstance and VastOrder handlers."""

from __future__ import annotations

import logging
from typing import Any

import kopf
import pytest


class _Patch:
    """Minimal stand-in for kopf.Patch supporting dict-like status access."""

    def __init__(self) -> None:
        self.status: dict[str, Any] = {}
        self.spec: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}


@pytest.fixture
def patch_obj() -> _Patch:
    return _Patch()


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


# ---------- VastInstance ----------


async def test_instance_on_create_resolves_refs_and_launches(
    monkeypatch: pytest.MonkeyPatch,
    patch_obj: _Patch,
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    captured_specs: list[dict[str, Any]] = []

    async def _fake_launch(spec: dict[str, Any], namespace: str) -> LaunchResult:
        captured_specs.append(spec)
        return LaunchResult(
            instance_id=1234,
            offer_id=999,
            template=TemplateSpec(
                name="tmpl-a",
                generation=3,
                image="ubuntu:22.04",
                disk_gb=64,
                env={"FOO": "bar"},
                onstart="echo hi",
                ssh_key=None,
            ),
            order=OrderPick(name="order-a", generation=5, offer_id=999, price_per_hour=0.30),
        )

    monkeypatch.setattr(instance, "launch_instance", _fake_launch)

    spec = {
        "templateRef": {"name": "tmpl-a"},
        "orderRef": {"name": "order-a"},
        "envOverrides": {"EXTRA": "1"},
    }
    result = await instance.on_create(
        spec=spec,
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert result == {"offerId": 999, "instanceId": 1234}
    assert patch_obj.status["instanceId"] == 1234
    assert patch_obj.status["offerId"] == 999
    assert patch_obj.status["phase"] == "Creating"
    assert patch_obj.status["resolvedTemplate"] == "tmpl-a@3"
    assert patch_obj.status["resolvedOrder"] == "order-a@5#999"
    assert captured_specs[0]["envOverrides"] == {"EXTRA": "1"}


async def test_instance_on_delete_destroys(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import instance

    destroyed: list[int] = []

    class _FakeClient:
        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())
    await instance.on_delete(
        spec={},
        status={"instanceId": 7},
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert destroyed == [7]


def test_phase_from_status_mapping() -> None:
    from vastai_operator.handlers.instance import _phase_from_status

    assert _phase_from_status("running") == "Running"
    assert _phase_from_status("loading") == "Creating"
    assert _phase_from_status("stopped") == "Stopped"
    assert _phase_from_status("failed") == "Failed"
    assert _phase_from_status("anything-else") == "Creating"


# ---------- VastOrder ----------


async def test_order_on_create_populates_status(
    monkeypatch: pytest.MonkeyPatch,
    patch_obj: _Patch,
) -> None:
    from vastai_operator.handlers import order
    from vastai_operator.vast_client import Offer

    class _FakeClient:
        async def search_offers(self, *_: Any, **__: Any) -> list[Offer]:
            return [
                Offer(id=2, gpu_name="RTX_4090", num_gpus=1, dph_total=0.25, disk_space=64, inet_down=200),
                Offer(id=3, gpu_name="RTX_4090", num_gpus=1, dph_total=0.30, disk_space=64, inet_down=200),
            ]

    monkeypatch.setattr(order, "_client_for", lambda spec, ns: _FakeClient())

    spec = {"gpuName": "RTX_4090", "numGpus": 1, "diskGB": 64, "maxResults": 5}
    await order.on_create(
        spec=spec,
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["matchCount"] == 2
    assert patch_obj.status["cheapestOfferId"] == 2
    assert patch_obj.status["cheapestPricePerHour"] == 0.25
    assert len(patch_obj.status["matchingOffers"]) == 2
    assert patch_obj.status["matchingOffers"][0]["id"] == 2


async def test_order_no_matches_sets_nomatch(
    monkeypatch: pytest.MonkeyPatch,
    patch_obj: _Patch,
) -> None:
    from vastai_operator.handlers import order

    class _Empty:
        async def search_offers(self, *_: Any, **__: Any) -> list[Any]:
            return []

    monkeypatch.setattr(order, "_client_for", lambda spec, ns: _Empty())
    await order.on_create(
        spec={"gpuName": "H200"},
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "NoMatch"
    assert patch_obj.status["matchCount"] == 0
    assert patch_obj.status["cheapestOfferId"] is None


async def test_order_truncates_to_max_results(
    monkeypatch: pytest.MonkeyPatch,
    patch_obj: _Patch,
) -> None:
    from vastai_operator.handlers import order
    from vastai_operator.vast_client import Offer

    class _Many:
        async def search_offers(self, *_: Any, **__: Any) -> list[Offer]:
            return [
                Offer(id=i, gpu_name="X", num_gpus=1, dph_total=float(i), disk_space=10, inet_down=10)
                for i in range(1, 11)
            ]

    monkeypatch.setattr(order, "_client_for", lambda spec, ns: _Many())
    await order.on_create(
        spec={"maxResults": 3},
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["matchCount"] == 3
    assert [o["id"] for o in patch_obj.status["matchingOffers"]] == [1, 2, 3]


# ---------- Resolver ----------


def test_fetch_order_pick_raises_when_no_cheapest(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            return {"metadata": {"generation": 1}, "status": {}}

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    with pytest.raises(kopf.TemporaryError):
        resolver.fetch_order_pick("default", "no-matches")


def test_fetch_template_returns_typed_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            return {
                "metadata": {"generation": 2},
                "spec": {"image": "alpine", "diskGB": 16, "env": {"A": "B"}, "onstart": "echo"},
                "status": {"phase": "Ready", "vastTemplateHash": "abc123"},
            }

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    t = resolver.fetch_template("default", "tmpl-x")
    assert t.image == "alpine"
    assert t.disk_gb == 16
    assert t.env == {"A": "B"}
    assert t.resolved_marker == "tmpl-x@2"
    assert t.template_hash_id == "abc123"
    assert t.is_ready


def test_fetch_template_pending_raises_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    import kopf

    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            return {
                "metadata": {"generation": 1},
                "spec": {"image": "x"},
                "status": {"phase": "Pending"},
            }

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    with pytest.raises(kopf.TemporaryError):
        resolver.fetch_template("default", "tmpl-x")
