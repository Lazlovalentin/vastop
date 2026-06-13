"""End-to-end controller-chain tests (no cluster, no kopf process).

The real handler functions are driven against an in-memory "world":

  * ``FakeVast`` — the Vast.ai marketplace: offers per rental market and the
    set of currently provisioned instances. The test mutates it to simulate
    a host reclaiming a machine or a bid instance being outbid (paused).
  * ``FakeCustom`` — a CustomObjectsApi double backed by a dict store, shared
    by resolver (template/order reads) and the alert handler (instance reads,
    recreate status patch).

Only the transport edges are faked; everything between — query building,
offer picking, marker resolution, bid propagation, event classification,
cooldowns, recreate — is the production code path.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from vastai_operator import launcher, resolver
from vastai_operator.handlers import alert, instance, order
from vastai_operator.vast_client import InstanceState, Offer, OfferFilters

logger = logging.getLogger("e2e")

NS = "default"


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


def _apply(obj: dict[str, Any], patch: _Patch) -> None:
    """Apply a kopf status patch the way the API server would (None deletes)."""
    status = obj.setdefault("status", {})
    for key, value in patch.status.items():
        if value is None:
            status[key] = None
        else:
            status[key] = value


# ---------- fakes ----------


class FakeVast:
    """The Vast.ai side of the world."""

    def __init__(self) -> None:
        self.offers: dict[str, list[Offer]] = {"on-demand": [], "interruptible": []}
        self.instances: dict[int, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.destroyed: list[int] = []
        self._next_id = 9000

    def add_offer(self, market: str, offer_id: int, price: float, min_bid: float = 0.0) -> None:
        self.offers[market].append(
            Offer(
                id=offer_id, gpu_name="RTX_4090", num_gpus=1, dph_total=price,
                disk_space=64, inet_down=500, min_bid=min_bid,
            )
        )

    def remove_offer(self, market: str, offer_id: int) -> None:
        self.offers[market] = [o for o in self.offers[market] if o.id != offer_id]

    def reclaim(self, instance_id: int) -> None:
        """Host takes the machine back: instance disappears entirely."""
        self.instances.pop(instance_id, None)

    def outbid(self, instance_id: int) -> None:
        """Someone outbids us: Vast.ai pauses the instance (stopped)."""
        self.instances[instance_id]["status"] = "stopped"

    def set_expiry(self, instance_id: int, seconds_from_now: float) -> None:
        """Set the rental contract end relative to now."""
        import time
        self.instances[instance_id]["end_date"] = time.time() + seconds_from_now

    def set_ports(self, instance_id: int, mapping: dict[int, int]) -> None:
        self.instances[instance_id]["ports"] = mapping


class FakeVastClient:
    """Mimics the VastClient wrapper interface, bound to a FakeVast world."""

    def __init__(self, world: FakeVast) -> None:
        self.world = world

    async def search_offers(self, filters: OfferFilters) -> list[Offer]:
        found = [
            o for o in self.world.offers[filters.rental_type]
            if filters.max_price_per_hour is None or o.dph_total <= filters.max_price_per_hour
        ]
        return sorted(found, key=lambda o: o.dph_total)

    async def create_instance(self, **kwargs: Any) -> int:
        self.world.create_calls.append(kwargs)
        self.world._next_id += 1
        iid = self.world._next_id
        self.world.instances[iid] = {
            "status": "running",
            "public_ip": "5.6.7.8",
            "offer_id": kwargs["offer_id"],
        }
        return iid

    async def get_instance(self, instance_id: int) -> InstanceState | None:
        raw = self.world.instances.get(instance_id)
        if raw is None:
            return None
        return InstanceState(
            id=instance_id, status=raw["status"], public_ip=raw["public_ip"],
            ssh_port=22, dph_total=0.1, ports=raw.get("ports"),
            end_date=raw.get("end_date"),
        )

    async def destroy_instance(self, instance_id: int) -> None:
        self.world.destroyed.append(instance_id)
        self.world.instances.pop(instance_id, None)  # 404s are swallowed upstream


class _ApiException(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"fake k8s {status}")
        self.status = status


class FakeCustom:
    """CustomObjectsApi double over a {(plural, name): obj} store."""

    def __init__(self, store: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.store = store

    def get_namespaced_custom_object(
        self, *, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        obj = self.store.get((plural, name))
        if obj is None:
            raise _ApiException(404)
        return obj

    def patch_namespaced_custom_object_status(
        self, *, group: str, version: str, namespace: str, plural: str, name: str,
        body: dict[str, Any],
    ) -> None:
        self.store[(plural, name)].setdefault("status", {}).update(body["status"])


# ---------- world fixture ----------


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeVast:
    vast = FakeVast()
    store: dict[tuple[str, str], dict[str, Any]] = {}
    custom = FakeCustom(store)

    # resolver's 404 detection checks `exc.status` on ApiException; our fake
    # exception mirrors that, so swap the class it isinstance-checks against.
    monkeypatch.setattr(resolver.k8s_client, "ApiException", _ApiException)
    monkeypatch.setattr(alert.k8s_client, "ApiException", _ApiException, raising=False)

    monkeypatch.setattr(resolver, "_custom", lambda: custom)
    monkeypatch.setattr(alert, "_custom", lambda: custom)
    monkeypatch.setattr(launcher, "VastClient", lambda api_key: FakeVastClient(vast))
    monkeypatch.setattr(alert, "VastClient", lambda api_key: FakeVastClient(vast))
    monkeypatch.setattr(order, "_client_for", lambda spec, ns: FakeVastClient(vast))
    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: FakeVastClient(vast))
    monkeypatch.setattr(alert, "_read_webhook", lambda ns, ref: "https://hooks/test")

    sent: list[dict[str, Any]] = []

    async def _capture(url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
        sent.append(payload)

    monkeypatch.setattr(alert.slack, "send_payload", _capture)

    vast.k8s = store  # type: ignore[attr-defined]
    vast.slack_sent = sent  # type: ignore[attr-defined]

    # Template is pre-synced (its own controller is out of scope here).
    store[("vasttemplates", "tmpl")] = {
        "metadata": {"generation": 1},
        "spec": {"image": "worker:latest", "diskGB": 32, "runtype": "ssh"},
        "status": {"phase": "Ready", "vastTemplateHash": "hash-1", "vastTemplateId": 1},
    }
    return vast


def _seed_cr(
    world: FakeVast,
    order_spec: dict[str, Any],
    alert_events: list[str],
    instance_spec_extra: dict[str, Any] | None = None,
) -> None:
    world.k8s[("vastorders", "ord")] = {  # type: ignore[attr-defined]
        "metadata": {"generation": 1}, "spec": order_spec, "status": {},
    }
    world.k8s[("vastinstances", "inst")] = {  # type: ignore[attr-defined]
        "metadata": {"generation": 1},
        "spec": {
            "templateRef": {"name": "tmpl"},
            "orderRef": {"name": "ord"},
            **(instance_spec_extra or {}),
        },
        "status": {},
    }
    world.k8s[("vastalerts", "al")] = {  # type: ignore[attr-defined]
        "metadata": {"generation": 1},
        "spec": {
            "instanceRef": {"name": "inst"},
            "slackWebhookSecretRef": {"name": "hook"},
            "events": alert_events,
            "actions": {"notify": True, "recreate": True},
            "pollIntervalSeconds": 0,  # no throttling between test ticks
        },
        "status": {},
    }


async def _tick_order(world: FakeVast) -> None:
    obj = world.k8s[("vastorders", "ord")]  # type: ignore[attr-defined]
    patch = _Patch()
    await order.refresh_timer(
        spec={**obj["spec"], "refreshIntervalSeconds": 0},
        status=obj.get("status", {}), patch=patch, namespace=NS, logger=logger,
    )
    _apply(obj, patch)


async def _tick_instance(world: FakeVast) -> None:
    obj = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    patch = _Patch()
    await instance.sync_status(
        spec=obj["spec"], status=obj.get("status", {}), patch=patch,
        namespace=NS, logger=logger,
    )
    _apply(obj, patch)


async def _tick_alert(world: FakeVast) -> None:
    obj = world.k8s[("vastalerts", "al")]  # type: ignore[attr-defined]
    patch = _Patch()
    await alert.watch(
        spec=obj["spec"], status=obj.get("status", {}), patch=patch,
        namespace=NS, logger=logger,
    )
    _apply(obj, patch)


async def _create_instance_cr(world: FakeVast) -> None:
    obj = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    patch = _Patch()
    await instance.on_create(spec=obj["spec"], patch=patch, namespace=NS, logger=logger)
    _apply(obj, patch)


# ---------- scenario 1: on-demand instance reclaimed by the host ----------


async def test_reclaimed_instance_is_recreated_on_new_cheapest_offer(world: FakeVast) -> None:
    _seed_cr(
        world,
        order_spec={"gpu": {"names": ["RTX_4090"]}, "price": {"maxPerHour": 0.5}},
        alert_events=["InstanceTerminated"],
    )
    world.add_offer("on-demand", offer_id=101, price=0.20)
    world.add_offer("on-demand", offer_id=102, price=0.25)

    # Order finds offers; Instance launches on the cheapest (101).
    await _tick_order(world)
    inst_cr = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    await _create_instance_cr(world)
    first_id = inst_cr["status"]["instanceId"]
    assert inst_cr["status"]["offerId"] == 101
    assert world.create_calls[0]["offer_id"] == 101
    assert inst_cr["status"]["resolvedOrder"] == "ord@1#101"

    # Steady state: instance Running, alert observes it.
    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"
    await _tick_alert(world)
    alert_cr = world.k8s[("vastalerts", "al")]  # type: ignore[attr-defined]
    assert alert_cr["status"]["lastObservedInstanceId"] == first_id
    assert not world.slack_sent  # type: ignore[attr-defined]

    # Host reclaims the machine; that offer is gone from the market too.
    world.reclaim(first_id)
    world.remove_offer("on-demand", 101)

    # Instance sync notices the rental vanished.
    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Failed"
    assert "not found" in inst_cr["status"]["message"].lower()

    # Order refresh re-searches: new cheapest is 102.
    await _tick_order(world)
    ord_cr = world.k8s[("vastorders", "ord")]  # type: ignore[attr-defined]
    assert ord_cr["status"]["cheapestOfferId"] == 102

    # Alert tick: classifies InstanceTerminated, notifies, recreates on 102.
    await _tick_alert(world)
    assert alert_cr["status"]["lastEvent"] == "InstanceTerminated"
    assert alert_cr["status"]["notifyCount"] == 1
    assert alert_cr["status"]["recreateCount"] == 1

    second_id = inst_cr["status"]["instanceId"]
    assert second_id != first_id
    assert inst_cr["status"]["offerId"] == 102
    assert inst_cr["status"]["resolvedOrder"] == "ord@1#102"
    assert world.create_calls[1]["offer_id"] == 102
    assert second_id in world.instances

    # Slack got exactly one message about the termination.
    sent = world.slack_sent  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "InstanceTerminated" in str(sent[0])

    # Next instance sync converges back to Running.
    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"

    # And the next alert tick stays quiet (new id observed, no new event).
    await _tick_alert(world)
    assert alert_cr["status"]["notifyCount"] == 1
    assert alert_cr["status"]["recreateCount"] == 1


# ---------- scenario 2: interruptible instance outbid → fallback recreate ----------


async def test_outbid_interruptible_recreated_with_on_demand_fallback(world: FakeVast) -> None:
    _seed_cr(
        world,
        order_spec={
            "gpu": {"names": ["RTX_4090"]},
            "price": {"maxPerHour": 0.5},
            "rental": {
                "type": "interruptible",
                "bidPricePerHour": 0.15,
                "fallbackToOnDemand": True,
            },
        },
        alert_events=["InstanceTerminated", "InstanceStopped"],
    )
    world.add_offer("interruptible", offer_id=201, price=0.10, min_bid=0.08)
    world.add_offer("on-demand", offer_id=301, price=0.30)

    # Order resolves on the bid market; launch places our bid.
    await _tick_order(world)
    ord_cr = world.k8s[("vastorders", "ord")]  # type: ignore[attr-defined]
    assert ord_cr["status"]["rentalTypeInUse"] == "interruptible"
    assert ord_cr["status"]["effectiveBidPerHour"] == 0.15

    inst_cr = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    await _create_instance_cr(world)
    first_id = inst_cr["status"]["instanceId"]
    assert world.create_calls[0]["offer_id"] == 201
    assert world.create_calls[0]["bid_price_per_hour"] == 0.15

    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"
    await _tick_alert(world)

    # Someone outbids us: Vast pauses the instance; bid market dries up.
    world.outbid(first_id)
    world.remove_offer("interruptible", 201)

    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Stopped"

    # Order refresh finds no bid offers and falls back to on-demand.
    await _tick_order(world)
    assert ord_cr["status"]["rentalTypeInUse"] == "on-demand"
    assert ord_cr["status"]["fellBackToOnDemand"] is True
    assert ord_cr["status"]["cheapestOfferId"] == 301
    assert ord_cr["status"]["effectiveBidPerHour"] is None

    # Alert fires InstanceStopped; recreate destroys the paused rental first,
    # then relaunches from the fallback offer WITHOUT a bid.
    await _tick_alert(world)
    alert_cr = world.k8s[("vastalerts", "al")]  # type: ignore[attr-defined]
    assert alert_cr["status"]["lastEvent"] == "InstanceStopped"
    assert alert_cr["status"]["recreateCount"] == 1
    assert world.destroyed == [first_id]

    second_id = inst_cr["status"]["instanceId"]
    assert second_id != first_id
    assert inst_cr["status"]["offerId"] == 301
    assert world.create_calls[1]["offer_id"] == 301
    assert world.create_calls[1]["bid_price_per_hour"] is None

    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"


# ---------- scenario 3: reclaimed but market is empty → recreate retries ----------


async def test_reclaimed_with_empty_market_keeps_retrying(world: FakeVast) -> None:
    import kopf

    _seed_cr(
        world,
        order_spec={"gpu": {"names": ["RTX_4090"]}, "price": {"maxPerHour": 0.5}},
        alert_events=["InstanceTerminated"],
    )
    world.add_offer("on-demand", offer_id=101, price=0.20)

    await _tick_order(world)
    await _create_instance_cr(world)
    inst_cr = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    first_id = inst_cr["status"]["instanceId"]
    await _tick_instance(world)
    await _tick_alert(world)

    # Machine reclaimed AND no offers left anywhere.
    world.reclaim(first_id)
    world.remove_offer("on-demand", 101)
    await _tick_instance(world)
    await _tick_order(world)
    ord_cr = world.k8s[("vastorders", "ord")]  # type: ignore[attr-defined]
    assert ord_cr["status"]["phase"] == "NoMatch"
    assert ord_cr["status"]["cheapestOfferId"] is None

    # Recreate cannot resolve an offer -> kopf retries (TemporaryError),
    # nothing launched, old status untouched.
    with pytest.raises(kopf.TemporaryError):
        await _tick_alert(world)
    assert len(world.create_calls) == 1
    assert inst_cr["status"]["instanceId"] == first_id  # still the dead id

    # Market recovers; next alert tick succeeds end-to-end.
    world.add_offer("on-demand", offer_id=103, price=0.22)
    await _tick_order(world)
    await _tick_alert(world)
    assert inst_cr["status"]["offerId"] == 103
    assert inst_cr["status"]["instanceId"] != first_id


# ---------- scenario 4: graceful pre-expiry rollover ----------


async def test_rollover_launches_replacement_and_swaps_after_healthy(
    world: FakeVast, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Probe verdict is controllable: replacement boots unhealthy, then recovers.
    health = {"ok": False}

    async def _probe(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        return (health["ok"], "HTTP 200" if health["ok"] else "HTTP 503")

    monkeypatch.setattr(instance, "probe", _probe)

    _seed_cr(
        world,
        order_spec={"gpu": {"names": ["RTX_4090"]}, "price": {"maxPerHour": 0.5}},
        # InstanceTerminated subscribed — must NOT fire during a planned swap.
        alert_events=["InstanceTerminated"],
        instance_spec_extra={
            "healthCheck": {"port": 8080, "failureThreshold": 1},
            "rollover": {"beforeExpirySeconds": 600, "requireHealthy": True},
        },
    )
    world.add_offer("on-demand", offer_id=401, price=0.20)

    # Launch original on 401.
    await _tick_order(world)
    inst_cr = world.k8s[("vastinstances", "inst")]  # type: ignore[attr-defined]
    await _create_instance_cr(world)
    first_id = inst_cr["status"]["instanceId"]
    world.set_ports(first_id, {8080: 50001})

    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"
    await _tick_alert(world)
    alert_cr = world.k8s[("vastalerts", "al")]  # type: ignore[attr-defined]
    assert alert_cr["status"]["lastObservedInstanceId"] == first_id

    # Rental now 5 min from expiry (window is 10 min). A cheaper fresh offer
    # appears; re-search so the replacement lands on the new cheapest.
    world.set_expiry(first_id, 300)
    world.remove_offer("on-demand", 401)
    world.add_offer("on-demand", offer_id=402, price=0.18)
    await _tick_order(world)
    assert world.k8s[("vastorders", "ord")]["status"]["cheapestOfferId"] == 402  # type: ignore[attr-defined]

    # Instance tick: inside the window -> launch the replacement alongside.
    await _tick_instance(world)
    replacement_id = inst_cr["status"]["rolloverInstanceId"]
    assert replacement_id is not None
    assert replacement_id != first_id
    assert inst_cr["status"]["rolloverOfferId"] == 402
    assert inst_cr["status"]["instanceId"] == first_id  # NOT promoted yet
    assert world.destroyed == []  # old rental still alive
    world.set_ports(replacement_id, {8080: 50002})

    # Replacement still booting (probe 503): old kept, no promotion.
    await _tick_instance(world)
    assert inst_cr["status"]["instanceId"] == first_id
    assert world.destroyed == []

    # Alert tick meanwhile: old id still observed, nothing fires.
    await _tick_alert(world)
    assert alert_cr["status"].get("lastEvent") is None
    assert not world.slack_sent  # type: ignore[attr-defined]

    # Replacement passes health -> promote, destroy old.
    health["ok"] = True
    await _tick_instance(world)
    assert world.destroyed == [first_id]
    assert inst_cr["status"]["instanceId"] == replacement_id
    assert inst_cr["status"]["offerId"] == 402
    assert inst_cr["status"]["workerHealthy"] is True
    assert inst_cr["status"]["healthExternalPort"] == 50002
    assert inst_cr["status"]["rolloverInstanceId"] is None

    # Alert observes the planned id swap (first_id -> replacement_id):
    # InstanceTerminated must NOT fire, no Slack.
    await _tick_alert(world)
    assert alert_cr["status"].get("lastEvent") is None
    assert alert_cr["status"]["lastObservedInstanceId"] == replacement_id
    assert not world.slack_sent  # type: ignore[attr-defined]

    # Steady state holds.
    await _tick_instance(world)
    assert inst_cr["status"]["phase"] == "Running"
    assert inst_cr["status"]["instanceId"] == replacement_id
