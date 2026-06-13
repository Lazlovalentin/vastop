"""Unit tests for VastClient wrapper."""

from __future__ import annotations

import pytest

from vastai_operator.vast_client import Offer, OfferFilters, VastAPIError, VastClient


async def test_search_offers_sorts_by_price() -> None:
    client = VastClient(api_key="test")
    client._sdk.search_response = [  # type: ignore[attr-defined]
        {"id": 1, "gpu_name": "RTX_4090", "num_gpus": 1, "dph_total": 0.50, "disk_space": 64, "inet_down": 200},
        {"id": 2, "gpu_name": "RTX_4090", "num_gpus": 1, "dph_total": 0.25, "disk_space": 64, "inet_down": 200},
    ]
    offers = await client.search_offers(
        OfferFilters(gpu_names=("RTX_4090",), min_disk_gb=64, max_price_per_hour=1.0)
    )
    assert [o.id for o in offers] == [2, 1]
    assert offers[0].dph_total == 0.25


async def test_search_offers_builds_query() -> None:
    client = VastClient(api_key="test")
    await client.search_offers(
        OfferFilters(
            gpu_names=("H100",),
            num_gpus=2,
            min_disk_gb=128,
            min_download_mbps=500,
            max_price_per_hour=3.0,
        )
    )
    call = client._sdk.search_offers_calls[0]  # type: ignore[attr-defined]
    q = call["query"]
    assert "gpu_name=H100" in q
    assert "num_gpus=2" in q
    assert "disk_space>=128" in q
    assert "inet_down>=500" in q
    assert "dph_total<=3.0" in q
    assert call["type"] == "on-demand"


async def test_create_instance_returns_id_from_new_contract() -> None:
    client = VastClient(api_key="test")
    client._sdk.create_response = {"new_contract": 99, "success": True}  # type: ignore[attr-defined]
    instance_id = await client.create_instance(
        offer_id=1,
        image="ubuntu:22.04",
        disk_gb=32,
        env={"FOO": "bar"},
        onstart="echo hi",
        ssh_key=None,
    )
    assert instance_id == 99


async def test_create_instance_raises_when_no_id() -> None:
    client = VastClient(api_key="test")
    client._sdk.create_response = {"error": "no gpu"}  # type: ignore[attr-defined]
    with pytest.raises(VastAPIError):
        await client.create_instance(
            offer_id=1,
            image="ubuntu:22.04",
            disk_gb=32,
            env=None,
            onstart=None,
            ssh_key=None,
        )


async def test_get_instance_handles_not_found() -> None:
    client = VastClient(api_key="test")

    def _raise(**_: object) -> None:
        raise Exception("404 not found")

    client._sdk.show_instance = _raise  # type: ignore[attr-defined]
    state = await client.get_instance(123)
    assert state is None


async def test_get_instance_parses_state() -> None:
    client = VastClient(api_key="test")
    client._sdk.show_response = {  # type: ignore[attr-defined]
        "id": 7,
        "actual_status": "running",
        "public_ipaddr": "1.2.3.4",
        "ssh_port": 2222,
        "dph_total": 0.42,
    }
    state = await client.get_instance(7)
    assert state is not None
    assert state.public_ip == "1.2.3.4"
    assert state.ssh_port == 2222
    assert state.dph_total == 0.42


def test_offer_from_api_coerces_types() -> None:
    o = Offer.from_api({"id": "5", "gpu_name": "RTX_4090", "num_gpus": "2", "dph_total": "0.3"})
    assert o.id == 5
    assert o.num_gpus == 2
    assert o.dph_total == 0.3


def test_empty_api_key_rejected() -> None:
    with pytest.raises(VastAPIError):
        VastClient(api_key="")
