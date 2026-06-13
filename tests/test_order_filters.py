"""Tests for the structured VastOrder filters, interruptible search and
the on-demand fallback path."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from vastai_operator.vast_client import Offer, OfferFilters


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


@pytest.fixture
def patch_obj() -> _Patch:
    return _Patch()


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


def _offer(offer_id: int, price: float = 0.2, min_bid: float = 0.0) -> Offer:
    return Offer(
        id=offer_id,
        gpu_name="RTX_4090",
        num_gpus=1,
        dph_total=price,
        disk_space=64,
        inet_down=500,
        min_bid=min_bid,
    )


# ---------- OfferFilters.to_query ----------


def test_to_query_single_gpu_and_bounds() -> None:
    q = OfferFilters(
        gpu_names=("RTX_4090",),
        num_gpus=2,
        min_gpu_ram_gb=24,
        min_cpu_cores=8,
        min_ram_gb=32,
        min_disk_gb=64,
        min_download_mbps=500,
        min_upload_mbps=100,
        min_reliability=0.98,
        min_price_per_hour=0.05,
        max_price_per_hour=0.60,
    ).to_query()
    assert "gpu_name=RTX_4090" in q
    assert "num_gpus=2" in q
    assert "gpu_ram>=24" in q
    assert "cpu_cores>=8" in q
    assert "cpu_ram>=32" in q
    assert "disk_space>=64" in q
    assert "inet_down>=500" in q
    assert "inet_up>=100" in q
    assert "reliability>=0.98" in q
    assert "dph_total>=0.05" in q
    assert "dph_total<=0.6" in q
    assert "verified=true" in q


def test_to_query_multi_gpu_uses_in_operator() -> None:
    q = OfferFilters(gpu_names=("RTX_4090", "RTX_5090")).to_query()
    assert "gpu_name in [RTX_4090,RTX_5090]" in q


def test_to_query_countries() -> None:
    one = OfferFilters(countries=("US",)).to_query()
    many = OfferFilters(countries=("US", "DE")).to_query()
    assert "geolocation=US" in one
    assert "geolocation in [US,DE]" in many


def test_to_query_unverified_clears_default() -> None:
    q = OfferFilters(verified_only=False).to_query()
    assert "verified=any" in q
    assert "verified=true" not in q


def test_sdk_offer_type_maps_interruptible_to_bid() -> None:
    assert OfferFilters(rental_type="interruptible").sdk_offer_type == "bid"
    assert OfferFilters().sdk_offer_type == "on-demand"


# ---------- spec -> filters mapping ----------


def test_filters_from_nested_spec() -> None:
    from vastai_operator.handlers.order import _filters_from_spec

    f = _filters_from_spec(
        {
            "gpu": {"names": ["RTX_4090", "RTX_5090"], "count": 2, "minVramGB": 24},
            "machine": {
                "minCpuCores": 16,
                "minRamGB": 64,
                "minDiskGB": 100,
                "minDownloadMbps": 750,
                "minUploadMbps": 200,
                "minReliability": 0.95,
            },
            "location": {"countries": ["US", "CA"]},
            "price": {"minPerHour": 0.1, "maxPerHour": 0.9},
            "rental": {"type": "interruptible"},
            "verifiedOnly": False,
        }
    )
    assert f.gpu_names == ("RTX_4090", "RTX_5090")
    assert f.num_gpus == 2
    assert f.min_gpu_ram_gb == 24
    assert f.min_cpu_cores == 16
    assert f.min_ram_gb == 64
    assert f.min_disk_gb == 100
    assert f.min_download_mbps == 750
    assert f.min_upload_mbps == 200
    assert f.min_reliability == 0.95
    assert f.countries == ("US", "CA")
    assert f.min_price_per_hour == 0.1
    assert f.max_price_per_hour == 0.9
    assert f.rental_type == "interruptible"
    assert f.verified_only is False


def test_filters_from_legacy_flat_spec() -> None:
    from vastai_operator.handlers.order import _filters_from_spec

    f = _filters_from_spec(
        {
            "gpuName": "RTX_3090",
            "numGpus": 1,
            "diskGB": 48,
            "minDownloadMbps": 100,
            "maxPricePerHour": 0.20,
            "rentalType": "interruptible",
            "geolocation": "US",
            "minSystemRamGB": 16,
            "minGpuRamGB": 12,
        }
    )
    assert f.gpu_names == ("RTX_3090",)
    assert f.min_disk_gb == 48
    assert f.max_price_per_hour == 0.20
    assert f.rental_type == "interruptible"
    assert f.countries == ("US",)
    assert f.min_ram_gb == 16
    assert f.min_gpu_ram_gb == 12


def test_nested_spec_wins_over_legacy() -> None:
    from vastai_operator.handlers.order import _filters_from_spec

    f = _filters_from_spec(
        {
            "gpuName": "OLD",
            "gpu": {"names": ["NEW"]},
            "maxPricePerHour": 1.0,
            "price": {"maxPerHour": 0.5},
        }
    )
    assert f.gpu_names == ("NEW",)
    assert f.max_price_per_hour == 0.5


# ---------- interruptible search + fallback ----------


class _MarketClient:
    """Fake VastClient returning different offers per rental market."""

    def __init__(self, by_type: dict[str, list[Offer]]) -> None:
        self.by_type = by_type
        self.calls: list[OfferFilters] = []

    async def search_offers(self, filters: OfferFilters) -> list[Offer]:
        self.calls.append(filters)
        return self.by_type.get(filters.rental_type, [])


async def test_interruptible_offers_found_no_fallback(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import order

    client = _MarketClient({"interruptible": [_offer(7, 0.11, min_bid=0.09)]})
    monkeypatch.setattr(order, "_client_for", lambda spec, ns: client)
    await order.on_create(
        spec={
            "gpu": {"names": ["RTX_4090"]},
            "price": {"maxPerHour": 0.5},
            "rental": {"type": "interruptible", "bidPricePerHour": 0.15, "fallbackToOnDemand": True},
        },
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["rentalTypeInUse"] == "interruptible"
    assert patch_obj.status["fellBackToOnDemand"] is False
    assert patch_obj.status["effectiveBidPerHour"] == 0.15
    assert patch_obj.status["matchingOffers"][0]["minBid"] == 0.09
    # Only the bid market was searched.
    assert [f.rental_type for f in client.calls] == ["interruptible"]


async def test_interruptible_falls_back_to_on_demand(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import order

    client = _MarketClient({"interruptible": [], "on-demand": [_offer(9, 0.3)]})
    monkeypatch.setattr(order, "_client_for", lambda spec, ns: client)
    await order.on_create(
        spec={
            "gpu": {"names": ["RTX_4090"]},
            "rental": {"type": "interruptible", "fallbackToOnDemand": True},
        },
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["rentalTypeInUse"] == "on-demand"
    assert patch_obj.status["fellBackToOnDemand"] is True
    assert patch_obj.status["effectiveBidPerHour"] is None
    assert patch_obj.status["cheapestOfferId"] == 9
    assert [f.rental_type for f in client.calls] == ["interruptible", "on-demand"]
    assert "fell back" in patch_obj.status["message"]


async def test_interruptible_no_fallback_stays_nomatch(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import order

    client = _MarketClient({"interruptible": [], "on-demand": [_offer(9, 0.3)]})
    monkeypatch.setattr(order, "_client_for", lambda spec, ns: client)
    await order.on_create(
        spec={"rental": {"type": "interruptible"}},  # fallback defaults to false
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "NoMatch"
    assert [f.rental_type for f in client.calls] == ["interruptible"]


async def test_bid_defaults_to_max_price(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import order

    client = _MarketClient({"interruptible": [_offer(1)]})
    monkeypatch.setattr(order, "_client_for", lambda spec, ns: client)
    await order.on_create(
        spec={"price": {"maxPerHour": 0.4}, "rental": {"type": "interruptible"}},
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["effectiveBidPerHour"] == 0.4


# ---------- bid propagation: Order status -> OrderPick -> create_instance ----------


def test_order_pick_carries_rental_type_and_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            return {
                "metadata": {"generation": 4},
                "status": {
                    "cheapestOfferId": 77,
                    "cheapestPricePerHour": 0.12,
                    "rentalTypeInUse": "interruptible",
                    "effectiveBidPerHour": 0.2,
                },
            }

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    pick = resolver.fetch_order_pick("default", "spot")
    assert pick.rental_type == "interruptible"
    assert pick.bid_price_per_hour == 0.2


async def test_launcher_forwards_bid_price(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import launcher
    from vastai_operator.resolver import OrderPick, TemplateSpec

    created: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, api_key: str) -> None: ...

        async def create_instance(self, **kwargs: Any) -> int:
            created.append(kwargs)
            return 555

    monkeypatch.setattr(launcher, "VastClient", _FakeClient)
    monkeypatch.setattr(
        launcher,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name="t", generation=1, image="img", disk_gb=10, env={}, onstart=None,
            ssh_key=None, template_hash_id="hash", template_phase="Ready",
        ),
    )
    monkeypatch.setattr(
        launcher,
        "fetch_order_pick",
        lambda ns, name: OrderPick(
            name="o", generation=1, offer_id=77, price_per_hour=0.12,
            rental_type="interruptible", bid_price_per_hour=0.2,
        ),
    )
    monkeypatch.setattr(launcher, "resolve_api_key", lambda spec, ns: "k")

    result = await launcher.launch_instance(
        {"templateRef": {"name": "t"}, "orderRef": {"name": "o"}}, "default"
    )
    assert result.instance_id == 555
    assert created[0]["bid_price_per_hour"] == 0.2


async def test_launcher_no_bid_for_on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import launcher
    from vastai_operator.resolver import OrderPick, TemplateSpec

    created: list[dict[str, Any]] = []

    class _FakeClient:
        def __init__(self, api_key: str) -> None: ...

        async def create_instance(self, **kwargs: Any) -> int:
            created.append(kwargs)
            return 556

    monkeypatch.setattr(launcher, "VastClient", _FakeClient)
    monkeypatch.setattr(
        launcher,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name="t", generation=1, image="img", disk_gb=10, env={}, onstart=None,
            ssh_key=None, template_hash_id="hash", template_phase="Ready",
        ),
    )
    monkeypatch.setattr(
        launcher,
        "fetch_order_pick",
        lambda ns, name: OrderPick(name="o", generation=1, offer_id=77, price_per_hour=0.12),
    )
    monkeypatch.setattr(launcher, "resolve_api_key", lambda spec, ns: "k")

    await launcher.launch_instance(
        {"templateRef": {"name": "t"}, "orderRef": {"name": "o"}}, "default"
    )
    assert created[0]["bid_price_per_hour"] is None
