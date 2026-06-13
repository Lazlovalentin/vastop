"""Tests for graceful pre-expiry rollover of VastInstance rentals."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest

from vastai_operator.handlers.instance import RolloverConfig
from vastai_operator.vast_client import InstanceState

logger = logging.getLogger("test")


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


@pytest.fixture
def patch_obj() -> _Patch:
    return _Patch()


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


def _epoch_in(seconds: float) -> float:
    return dt.datetime.now(dt.UTC).timestamp() + seconds


def _state(
    iid: int = 1,
    status: str = "running",
    end_in: float | None = None,
    ports: dict[int, int] | None = None,
    public_ip: str | None = "5.6.7.8",
) -> InstanceState:
    return InstanceState(
        id=iid, status=status, public_ip=public_ip, ssh_port=22, dph_total=0.1,
        ports=ports, end_date=_epoch_in(end_in) if end_in is not None else None,
    )


class _Client:
    """VastClient double for rollover: serves get_instance/destroy."""

    def __init__(self, instances: dict[int, InstanceState]) -> None:
        self.instances = dict(instances)
        self.destroyed: list[int] = []

    async def get_instance(self, instance_id: int) -> InstanceState | None:
        return self.instances.get(instance_id)

    async def destroy_instance(self, instance_id: int) -> None:
        self.destroyed.append(instance_id)
        self.instances.pop(instance_id, None)


# ---------- config parsing ----------


def test_rollover_config_defaults_to_10_minutes() -> None:
    cfg = RolloverConfig.from_spec({"rollover": {}})
    assert cfg is not None
    assert cfg.before_expiry_seconds == 600
    assert cfg.require_healthy is True


def test_rollover_config_absent_or_disabled() -> None:
    assert RolloverConfig.from_spec({}) is None
    assert RolloverConfig.from_spec({"rollover": {"enabled": False}}) is None


def test_instance_state_parses_end_date() -> None:
    state = InstanceState.from_api({"id": 1, "actual_status": "running", "end_date": 1750000000.0})
    assert state.end_date == 1750000000.0
    assert InstanceState.from_api({"id": 1, "actual_status": "running"}).end_date is None


# ---------- starting a rollover ----------


async def test_no_rollover_outside_window(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(spec: dict[str, Any], ns: str) -> None:
        raise AssertionError("must not launch outside the expiry window")

    monkeypatch.setattr(instance, "launch_instance", _boom)
    await instance._maybe_rollover(
        spec={"rollover": {"beforeExpirySeconds": 600}},
        status={"instanceId": 1},
        patch=patch_obj,
        state=_state(end_in=3600),  # an hour left
        client=_Client({}),
        namespace="default",
        logger=logger,
    )
    assert patch_obj.status == {}


async def test_rollover_launches_replacement_inside_window(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    launched: list[str] = []

    async def _launch(spec: dict[str, Any], ns: str) -> LaunchResult:
        launched.append(ns)
        return LaunchResult(
            instance_id=2222, offer_id=55,
            template=TemplateSpec(
                name="t", generation=1, image="x", disk_gb=10, env={},
                onstart=None, ssh_key=None,
            ),
            order=OrderPick(name="o", generation=3, offer_id=55, price_per_hour=0.1),
        )

    monkeypatch.setattr(instance, "launch_instance", _launch)
    await instance._maybe_rollover(
        spec={"rollover": {"beforeExpirySeconds": 600}},
        status={"instanceId": 1111},
        patch=patch_obj,
        state=_state(iid=1111, end_in=300),  # 5 min left, window is 10
        client=_Client({}),
        namespace="default",
        logger=logger,
    )
    assert launched == ["default"]
    assert patch_obj.status["rolloverInstanceId"] == 2222
    assert patch_obj.status["rolloverOfferId"] == 55
    assert patch_obj.status["rolloverResolvedOrder"] == "o@3#55"
    # Old rental untouched: instanceId not changed.
    assert "instanceId" not in patch_obj.status


async def test_no_rollover_for_open_ended_or_stopped(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(spec: dict[str, Any], ns: str) -> None:
        raise AssertionError("must not launch")

    monkeypatch.setattr(instance, "launch_instance", _boom)
    # No end_date -> open-ended rental, nothing to roll.
    await instance._maybe_rollover(
        spec={"rollover": {}}, status={"instanceId": 1}, patch=patch_obj,
        state=_state(end_in=None), client=_Client({}), namespace="default", logger=logger,
    )
    # Stopped instance inside window -> alert/recreate territory, not rollover.
    await instance._maybe_rollover(
        spec={"rollover": {}}, status={"instanceId": 1}, patch=patch_obj,
        state=_state(status="stopped", end_in=60), client=_Client({}),
        namespace="default", logger=logger,
    )
    assert patch_obj.status == {}


# ---------- finishing a rollover ----------


def _pending_status(old: int = 1111, new: int = 2222) -> dict[str, Any]:
    return {
        "instanceId": old,
        "rolloverInstanceId": new,
        "rolloverOfferId": 55,
        "rolloverLaunchTime": dt.datetime.now(dt.UTC).isoformat(),
        "rolloverResolvedTemplate": "t@1",
        "rolloverResolvedOrder": "o@3#55",
    }


async def test_old_kept_until_replacement_healthy(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _unhealthy(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        return False, "HTTP 503"

    monkeypatch.setattr(instance, "probe", _unhealthy)
    client = _Client({
        1111: _state(iid=1111, end_in=300),
        2222: _state(iid=2222, ports={8080: 41000}),
    })
    await instance._maybe_rollover(
        spec={"rollover": {}, "healthCheck": {"port": 8080}},
        status=_pending_status(),
        patch=patch_obj,
        state=_state(iid=1111, end_in=300),
        client=client,
        namespace="default",
        logger=logger,
    )
    assert client.destroyed == []
    assert "instanceId" not in patch_obj.status  # no promotion yet


async def test_promotes_and_destroys_old_after_new_healthy(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    probed: list[str] = []

    async def _healthy(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        probed.append(url)
        return True, "HTTP 200"

    monkeypatch.setattr(instance, "probe", _healthy)
    client = _Client({
        1111: _state(iid=1111, end_in=300),
        2222: _state(iid=2222, ports={8080: 41000}),
    })
    await instance._maybe_rollover(
        spec={"rollover": {}, "healthCheck": {"port": 8080}},
        status=_pending_status(),
        patch=patch_obj,
        state=_state(iid=1111, end_in=300),
        client=client,
        namespace="default",
        logger=logger,
    )
    # Probed the REPLACEMENT through its own port mapping.
    assert probed == ["http://5.6.7.8:41000/healthz"]
    assert client.destroyed == [1111]
    assert patch_obj.status["instanceId"] == 2222
    assert patch_obj.status["offerId"] == 55
    assert patch_obj.status["resolvedOrder"] == "o@3#55"
    assert patch_obj.status["workerHealthy"] is True
    assert patch_obj.status["healthExternalPort"] == 41000
    assert patch_obj.status["rolloverInstanceId"] is None
    assert "rolled over" in patch_obj.status["message"].lower()


async def test_promotes_unhealthy_replacement_if_old_already_expired(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _unhealthy(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        return False, "HTTP 503"

    monkeypatch.setattr(instance, "probe", _unhealthy)
    # Old rental (1111) is gone — only the booting replacement exists.
    client = _Client({2222: _state(iid=2222, ports={8080: 41000})})
    await instance._maybe_rollover(
        spec={"rollover": {}, "healthCheck": {"port": 8080}},
        status=_pending_status(),
        patch=patch_obj,
        state=_state(iid=1111, end_in=10),
        client=client,
        namespace="default",
        logger=logger,
    )
    assert client.destroyed == []  # nothing left to destroy
    assert patch_obj.status["instanceId"] == 2222
    assert patch_obj.status["workerHealthy"] is None  # not yet proven healthy


async def test_vanished_replacement_clears_state_for_retry(patch_obj: _Patch) -> None:
    from vastai_operator.handlers import instance

    client = _Client({1111: _state(iid=1111, end_in=300)})  # 2222 missing
    await instance._maybe_rollover(
        spec={"rollover": {}},
        status=_pending_status(),
        patch=patch_obj,
        state=_state(iid=1111, end_in=300),
        client=client,
        namespace="default",
        logger=logger,
    )
    assert patch_obj.status["rolloverInstanceId"] is None
    assert "instanceId" not in patch_obj.status
    assert client.destroyed == []


async def test_without_healthcheck_running_is_enough(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        raise AssertionError("no healthCheck -> no probe")

    monkeypatch.setattr(instance, "probe", _boom)
    client = _Client({
        1111: _state(iid=1111, end_in=300),
        2222: _state(iid=2222),
    })
    await instance._maybe_rollover(
        spec={"rollover": {}},  # no healthCheck
        status=_pending_status(),
        patch=patch_obj,
        state=_state(iid=1111, end_in=300),
        client=client,
        namespace="default",
        logger=logger,
    )
    assert client.destroyed == [1111]
    assert patch_obj.status["instanceId"] == 2222
    assert patch_obj.status["workerHealthy"] is None


# ---------- on_delete kills the in-flight replacement too ----------


async def test_on_delete_destroys_both_rentals(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import instance

    destroyed: list[int] = []

    class _FakeClient:
        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())
    await instance.on_delete(
        spec={},
        status={"instanceId": 1111, "rolloverInstanceId": 2222},
        namespace="default",
        logger=logger,
    )
    assert destroyed == [1111, 2222]


# ---------- alert classifier: planned id change must not fire ----------


def test_planned_id_change_does_not_fire_terminated() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedInstanceId": 1111, "lastObservedPhase": "Running"},
        current_instance={
            "status": {"phase": "Running", "instanceId": 2222, "message": "Rolled over"}
        },
    )
    assert event is None


def test_lost_id_still_fires_terminated() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedInstanceId": 1111, "lastObservedPhase": "Running"},
        current_instance={"status": {"phase": "Running", "instanceId": None, "message": ""}},
    )
    assert event == "InstanceTerminated"
