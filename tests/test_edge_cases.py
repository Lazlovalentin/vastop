"""Edge-case coverage: missing refs, throttling, error paths."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import kopf
import pytest


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


# ---------- Resolver ----------


def test_fetch_template_missing_raises_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    from kubernetes import client as k8s_client

    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            raise k8s_client.ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    with pytest.raises(kopf.TemporaryError):
        resolver.fetch_template("default", "missing")


def test_fetch_order_pick_missing_raises_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    from kubernetes import client as k8s_client

    from vastai_operator import resolver

    class _Custom:
        def get_namespaced_custom_object(self, **_: Any) -> dict[str, Any]:
            raise k8s_client.ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(resolver, "_custom", lambda: _Custom())
    with pytest.raises(kopf.TemporaryError):
        resolver.fetch_order_pick("default", "missing")


def test_resolve_api_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    monkeypatch.setenv("VAST_API_KEY", "env-fallback")
    assert resolver.resolve_api_key({}, "default") == "env-fallback"


def test_resolve_api_key_no_secret_and_no_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator import resolver

    monkeypatch.delenv("VAST_API_KEY", raising=False)
    with pytest.raises(kopf.PermanentError):
        resolver.resolve_api_key({}, "default")


# ---------- Launcher ----------


async def test_launch_instance_propagates_order_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator import launcher
    from vastai_operator.resolver import TemplateSpec

    monkeypatch.setattr(
        launcher,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name=name, generation=1, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
        ),
    )

    def _no_offers(ns: str, name: str) -> None:
        raise kopf.TemporaryError("no offers", delay=60)

    monkeypatch.setattr(launcher, "fetch_order_pick", _no_offers)

    with pytest.raises(kopf.TemporaryError):
        await launcher.launch_instance(
            {"templateRef": {"name": "t"}, "orderRef": {"name": "o"}},
            "default",
        )


# ---------- Order timer throttling ----------


async def test_order_timer_skips_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import order

    calls: list[str] = []

    async def _refresh(*a: Any, **k: Any) -> None:
        calls.append("called")

    monkeypatch.setattr(order, "_refresh_offers", _refresh)

    fresh_iso = dt.datetime.now(dt.UTC).isoformat()
    await order.refresh_timer(
        spec={"refreshIntervalSeconds": 600},
        status={"lastSearchTime": fresh_iso},
        patch=_Patch(),
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert calls == []  # short-circuited


async def test_order_timer_runs_when_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import order

    calls: list[str] = []

    async def _refresh(*a: Any, **k: Any) -> None:
        calls.append("called")

    monkeypatch.setattr(order, "_refresh_offers", _refresh)

    stale_iso = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    await order.refresh_timer(
        spec={"refreshIntervalSeconds": 60},
        status={"lastSearchTime": stale_iso},
        patch=_Patch(),
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert calls == ["called"]


async def test_order_timer_runs_when_no_last_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import order

    calls: list[str] = []

    async def _refresh(*a: Any, **k: Any) -> None:
        calls.append("called")

    monkeypatch.setattr(order, "_refresh_offers", _refresh)
    await order.refresh_timer(
        spec={},
        status={},
        patch=_Patch(),
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert calls == ["called"]


# ---------- Instance on_update ----------


async def test_instance_update_ignores_non_ref_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance

    async def _should_not_call(*a: Any, **k: Any) -> Any:
        raise AssertionError("_launch_and_patch must not be called")

    monkeypatch.setattr(instance, "_launch_and_patch", _should_not_call)

    diff = [kopf.DiffItem(operation="change", field=("spec", "envOverrides", "X"), old="a", new="b")]
    await instance.on_update(
        spec={"templateRef": {"name": "t"}, "orderRef": {"name": "o"}, "recreateOnRefChange": True},
        status={"instanceId": 99},
        diff=diff,
        patch=_Patch(),
        namespace="default",
        logger=logging.getLogger("test"),
    )


async def test_instance_update_respects_no_recreate_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance

    destroyed: list[int] = []

    class _FakeClient:
        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())

    async def _should_not_call(*a: Any, **k: Any) -> Any:
        raise AssertionError("_launch_and_patch must not be called when flag disabled")

    monkeypatch.setattr(instance, "_launch_and_patch", _should_not_call)

    diff = [
        kopf.DiffItem(operation="change", field=("spec", "templateRef", "name"), old="a", new="b"),
    ]
    await instance.on_update(
        spec={"templateRef": {"name": "b"}, "orderRef": {"name": "o"}, "recreateOnRefChange": False},
        status={"instanceId": 99},
        diff=diff,
        patch=_Patch(),
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert destroyed == []  # not destroyed because flag disabled


# ---------- Alert classifier edge cases ----------


def test_classify_running_no_event() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated", "InstanceFailed"},
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 7},
        current_instance={"status": {"phase": "Running", "instanceId": 7, "message": "ok"}},
    )
    assert event is None


def test_classify_stopped_only_on_transition() -> None:
    from vastai_operator.handlers.alert import _classify_event

    initial = _classify_event(
        {"InstanceStopped"},
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Stopped", "instanceId": 1, "message": ""}},
    )
    assert initial == "InstanceStopped"

    repeat = _classify_event(
        {"InstanceStopped"},
        prev={"lastObservedPhase": "Stopped", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Stopped", "instanceId": 1, "message": ""}},
    )
    assert repeat is None


def test_classify_planned_id_swap_does_not_fire_terminated() -> None:
    from vastai_operator.handlers.alert import _classify_event

    # An id change to a *new valid* id is a planned replacement (pre-expiry
    # rollover, template-drift recreate, alert recreate) — not a termination.
    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Running", "instanceId": 2, "message": "ok"}},
    )
    assert event is None


def test_classify_id_lost_fires_terminated() -> None:
    from vastai_operator.handlers.alert import _classify_event

    # Losing the id entirely (no replacement) is a real termination.
    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Running", "instanceId": None, "message": ""}},
    )
    assert event == "InstanceTerminated"


def test_cooldown_expired_allows_repeat() -> None:
    from vastai_operator.handlers.alert import _within_cooldown

    old = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=400)).isoformat()
    assert not _within_cooldown(
        {"lastEvent": "InstanceTerminated", "lastEventTime": old},
        cooldown_seconds=300,
        event="InstanceTerminated",
    )


def test_cooldown_malformed_timestamp_treated_as_no_cooldown() -> None:
    from vastai_operator.handlers.alert import _within_cooldown

    assert not _within_cooldown(
        {"lastEvent": "InstanceTerminated", "lastEventTime": "not-a-time"},
        cooldown_seconds=300,
        event="InstanceTerminated",
    )


# ---------- vast_client query building ----------


async def test_search_offers_no_max_price() -> None:
    from vastai_operator.vast_client import OfferFilters, VastClient

    client = VastClient(api_key="test")
    await client.search_offers(
        OfferFilters(min_disk_gb=8, min_download_mbps=10)
    )
    call = client._sdk.search_offers_calls[0]  # type: ignore[attr-defined]
    q = call["query"]
    assert "dph_total<=" not in q
    assert "gpu_name=" not in q


async def test_destroy_instance_swallows_404() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")

    def _raise(**_: Any) -> None:
        raise Exception("404 not found")

    client._sdk.destroy_instance = _raise  # type: ignore[attr-defined]
    # Should NOT raise
    await client.destroy_instance(123)


# ---------- Slack render ----------


def test_render_with_all_none_tokens() -> None:
    from vastai_operator import slack

    out = slack.render(
        "ev={event} inst={instance} ns={namespace} id={instanceId} ip={publicIp} ph={phase}",
        event="X",
        instance=None,
        namespace=None,
        instanceId=None,
        publicIp=None,
        phase=None,
    )
    assert out == "ev=X inst= ns= id= ip= ph="


# ---------- Template drift recreate ----------


async def test_check_template_drift_detects_generation_bump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.resolver import TemplateSpec

    monkeypatch.setattr(
        instance,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name=name, generation=5, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
        ),
    )
    drifted = instance._check_template_drift(
        spec={"templateRef": {"name": "t"}},
        status={"resolvedTemplate": "t@3"},  # old generation
        namespace="default",
    )
    assert drifted is True


async def test_check_template_drift_no_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.resolver import TemplateSpec

    monkeypatch.setattr(
        instance,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name=name, generation=3, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
        ),
    )
    drifted = instance._check_template_drift(
        spec={"templateRef": {"name": "t"}},
        status={"resolvedTemplate": "t@3"},
        namespace="default",
    )
    assert drifted is False


async def test_check_template_drift_disabled_via_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance

    def _fail(*a: Any, **k: Any) -> None:
        raise AssertionError("fetch_template should not be called when flag disabled")

    monkeypatch.setattr(instance, "fetch_template", _fail)
    drifted = instance._check_template_drift(
        spec={"templateRef": {"name": "t"}, "recreateOnTemplateUpdate": False},
        status={"resolvedTemplate": "t@1"},
        namespace="default",
    )
    assert drifted is False


async def test_check_template_drift_no_prior_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.resolver import TemplateSpec

    monkeypatch.setattr(
        instance,
        "fetch_template",
        lambda ns, name: TemplateSpec(
            name=name, generation=1, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
        ),
    )
    drifted = instance._check_template_drift(
        spec={"templateRef": {"name": "t"}},
        status={},  # never resolved
        namespace="default",
    )
    assert drifted is False


async def test_sync_status_triggers_recreate_on_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    def _drift_yes(spec: Any, status: Any, ns: str) -> bool:
        return True

    monkeypatch.setattr(instance, "_check_template_drift", _drift_yes)

    destroyed: list[int] = []

    class _FakeClient:
        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

        async def get_instance(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("get_instance must not be called on drift path")

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())

    launches: list[Any] = []

    async def _fake_launch(spec: Any, namespace: str) -> LaunchResult:
        launches.append((spec, namespace))
        return LaunchResult(
            instance_id=222,
            offer_id=33,
            template=TemplateSpec(
                name="t", generation=5, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
            ),
            order=OrderPick(name="o", generation=2, offer_id=33, price_per_hour=0.1),
        )

    monkeypatch.setattr(instance, "launch_instance", _fake_launch)

    patch_obj = _Patch()
    await instance.sync_status(
        spec={"templateRef": {"name": "t"}, "orderRef": {"name": "o"}},
        status={"instanceId": 111, "resolvedTemplate": "t@1"},
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert destroyed == [111]
    assert len(launches) == 1
    assert patch_obj.status["instanceId"] == 222
    assert patch_obj.status["phase"] == "Creating"


# ---------- Slack send ----------


async def test_slack_send_succeeds_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from vastai_operator import slack

    class _Resp:
        status_code = 200
        text = "ok"

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> "_Client":  # noqa: UP037
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, url: str, json: dict[str, Any]) -> _Resp:
            assert json == {"text": "hi"}
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await slack.send("https://x", slack.SlackMessage(text="hi"))
