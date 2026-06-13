"""Handlers for VastOrder — search Vast.ai and publish matching offers to status."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import kopf

from ..config import CONFIG
from ..resolver import resolve_api_key
from ..timeutils import seconds_since
from ..vast_client import OfferFilters, VastAPIError, VastClient

GROUP = CONFIG.api_group
VERSION = CONFIG.api_version
PLURAL = "vastorders"


def _client_for(spec: dict[str, Any], namespace: str) -> VastClient:
    return VastClient(api_key=resolve_api_key(spec, namespace))


def _filters_from_spec(spec: dict[str, Any]) -> OfferFilters:
    """Build OfferFilters from the nested spec blocks, falling back to the
    legacy flat fields (gpuName, numGpus, diskGB, ...) where the nested form
    is absent."""
    gpu = spec.get("gpu") or {}
    machine = spec.get("machine") or {}
    location = spec.get("location") or {}
    price = spec.get("price") or {}
    rental = spec.get("rental") or {}

    names = gpu.get("names")
    if not names and spec.get("gpuName"):
        names = [spec["gpuName"]]

    return OfferFilters(
        gpu_names=tuple(names or ()),
        num_gpus=int(gpu.get("count") or spec.get("numGpus", 1)),
        min_gpu_ram_gb=_opt_float(gpu.get("minVramGB", spec.get("minGpuRamGB"))),
        min_cpu_cores=_opt_int(machine.get("minCpuCores", spec.get("minCpuCores"))),
        min_ram_gb=_opt_float(machine.get("minRamGB", spec.get("minSystemRamGB"))),
        min_disk_gb=float(machine.get("minDiskGB") or spec.get("diskGB", 32)),
        min_download_mbps=_opt_float(
            machine.get("minDownloadMbps", spec.get("minDownloadMbps", 100))
        ),
        min_upload_mbps=_opt_float(machine.get("minUploadMbps", spec.get("minUploadMbps"))),
        min_cpu_ghz=_opt_float(machine.get("minCpuGhz")),
        min_reliability=_opt_float(machine.get("minReliability")),
        countries=tuple(
            location.get("countries")
            or ([spec["geolocation"]] if spec.get("geolocation") else ())
        ),
        min_price_per_hour=_opt_float(price.get("minPerHour")),
        max_price_per_hour=_opt_float(price.get("maxPerHour", spec.get("maxPricePerHour"))),
        rental_type=rental.get("type") or spec.get("rentalType", "on-demand"),
        verified_only=bool(spec.get("verifiedOnly", True)),
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _effective_bid(spec: dict[str, Any], filters: OfferFilters) -> float | None:
    """Bid price for interruptible rentals: explicit bid wins, else max price."""
    rental = spec.get("rental") or {}
    bid = rental.get("bidPricePerHour")
    if bid is not None:
        return float(bid)
    return filters.max_price_per_hour


async def _refresh_offers(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
) -> None:
    client = _client_for(spec, namespace)
    filters = _filters_from_spec(spec)
    rental_in_use = filters.rental_type
    fell_back = False
    try:
        offers = await client.search_offers(filters)
        if (
            not offers
            and filters.rental_type == "interruptible"
            and bool((spec.get("rental") or {}).get("fallbackToOnDemand", False))
        ):
            logger.info("No interruptible offers; falling back to on-demand search")
            offers = await client.search_offers(filters.with_rental_type("on-demand"))
            if offers:
                rental_in_use = "on-demand"
                fell_back = True
    except VastAPIError as exc:
        patch.status["phase"] = "Failed"
        patch.status["message"] = f"Search failed: {exc}"
        raise kopf.TemporaryError(str(exc), delay=60) from exc

    limit = int(spec.get("maxResults", 20))
    trimmed = offers[:limit]

    patch.status["matchingOffers"] = [
        {
            "id": o.id,
            "gpuName": o.gpu_name,
            "numGpus": o.num_gpus,
            "pricePerHour": o.dph_total,
            "diskSpace": o.disk_space,
            "inetDown": o.inet_down,
            "inetUp": o.inet_up,
            "minBid": o.min_bid,
            "cpuCores": o.cpu_cores,
            "ramGB": o.cpu_ram_gb,
            "vramGB": o.gpu_ram_gb,
            "geolocation": o.geolocation,
        }
        for o in trimmed
    ]
    patch.status["matchCount"] = len(trimmed)
    patch.status["lastSearchTime"] = dt.datetime.now(dt.UTC).isoformat()
    patch.status["rentalTypeInUse"] = rental_in_use
    patch.status["fellBackToOnDemand"] = fell_back
    patch.status["effectiveBidPerHour"] = (
        _effective_bid(spec, filters) if rental_in_use == "interruptible" else None
    )

    if not trimmed:
        patch.status["phase"] = "NoMatch"
        patch.status["message"] = "No offers match the criteria"
        patch.status["cheapestOfferId"] = None
        patch.status["cheapestPricePerHour"] = None
        return

    cheapest = trimmed[0]
    patch.status["phase"] = "Ready"
    patch.status["message"] = (
        f"{len(trimmed)} offers found"
        + (" (fell back to on-demand)" if fell_back else "")
    )
    patch.status["cheapestOfferId"] = cheapest.id
    patch.status["cheapestPricePerHour"] = cheapest.dph_total
    logger.info(
        "VastOrder updated: %d offers, cheapest=%s @ $%.4f (%s)",
        len(trimmed),
        cheapest.id,
        cheapest.dph_total,
        rental_in_use,
    )


@kopf.on.create(GROUP, VERSION, PLURAL)
async def on_create(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    patch.status["phase"] = "Pending"
    await _refresh_offers(spec, patch, namespace, logger)


@kopf.on.update(GROUP, VERSION, PLURAL, field="spec")
async def on_update(
    spec: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    await _refresh_offers(spec, patch, namespace, logger)


@kopf.timer(
    GROUP,
    VERSION,
    PLURAL,
    interval=CONFIG.order_refresh_seconds,
    idle=30,
)
async def refresh_timer(
    spec: dict[str, Any],
    status: dict[str, Any],
    patch: kopf.Patch,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    refresh = float(spec.get("refreshIntervalSeconds", CONFIG.order_refresh_seconds))
    # The kopf timer fires on a global cadence (CONFIG.order_refresh_seconds);
    # the per-Order spec.refreshIntervalSeconds further throttles via this guard.
    elapsed = seconds_since(status.get("lastSearchTime"))
    if elapsed is not None and elapsed < refresh - 1:
        return
    await _refresh_offers(spec, patch, namespace, logger)
