"""Tests for worker health probing and its VastAlert integration."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
import pytest

from vastai_operator.health import HealthCheckConfig, probe
from vastai_operator.vast_client import InstanceState


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


@pytest.fixture
def patch_obj() -> _Patch:
    return _Patch()


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


# ---------- HealthCheckConfig ----------


def test_config_absent_when_no_healthcheck() -> None:
    assert HealthCheckConfig.from_spec({}) is None
    assert HealthCheckConfig.from_spec({"healthCheck": {}}) is None


def test_config_parses_spec_and_normalizes_path() -> None:
    hc = HealthCheckConfig.from_spec(
        {
            "healthCheck": {
                "port": 8080,
                "path": "health",  # missing leading slash
                "timeoutSeconds": 3,
                "initialDelaySeconds": 120,
                "failureThreshold": 2,
            }
        }
    )
    assert hc is not None
    assert hc.port == 8080
    assert hc.path == "/health"
    assert hc.timeout_seconds == 3
    assert hc.initial_delay_seconds == 120
    assert hc.failure_threshold == 2
    assert hc.url("1.2.3.4", 40123) == "http://1.2.3.4:40123/health"


# ---------- probe ----------


class _FakeAsyncClient:
    """httpx.AsyncClient double; behavior driven by class attributes."""

    status_code = 200
    raise_error: Exception | None = None

    def __init__(self, *a: Any, **k: Any) -> None: ...

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *a: Any) -> None: ...

    async def get(self, url: str) -> Any:
        if self.raise_error is not None:
            raise self.raise_error

        class _Resp:
            status_code = _FakeAsyncClient.status_code

        return _Resp()


async def test_probe_2xx_is_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.status_code = 200
    _FakeAsyncClient.raise_error = None
    ok, detail = await probe("http://1.2.3.4:8080/healthz")
    assert ok is True
    assert "200" in detail


async def test_probe_5xx_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.status_code = 503
    _FakeAsyncClient.raise_error = None
    ok, detail = await probe("http://1.2.3.4:8080/healthz")
    assert ok is False
    assert "503" in detail


async def test_probe_connect_error_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.raise_error = httpx.ConnectError("refused")
    ok, detail = await probe("http://1.2.3.4:8080/healthz")
    _FakeAsyncClient.raise_error = None
    assert ok is False
    assert "ConnectError" in detail


# ---------- port mapping ----------


def test_instance_state_parses_docker_port_map() -> None:
    state = InstanceState.from_api(
        {
            "id": 1,
            "actual_status": "running",
            "public_ipaddr": "5.6.7.8",
            "ports": {
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40123"}],
                "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2222"}],
            },
        }
    )
    assert state.external_port(8080) == 40123
    assert state.external_port(22) == 2222
    assert state.external_port(9999) is None


def test_instance_state_tolerates_missing_ports() -> None:
    state = InstanceState.from_api({"id": 1, "actual_status": "running"})
    assert state.external_port(8080) is None


# ---------- probe_health timer (dedicated 24s cadence) ----------


def _running_status(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instanceId": 1,
        "phase": "Running",
        "publicIp": "5.6.7.8",
        "healthExternalPort": 40123,
    }
    base.update(extra)
    return base


def test_default_cadence_is_5_probes_per_2_minutes() -> None:
    from vastai_operator.config import OperatorConfig

    assert 120 / OperatorConfig().health_probe_seconds == 5
    hc = HealthCheckConfig.from_spec({"healthCheck": {"port": 8080}})
    assert hc is not None
    assert 120 / hc.interval_seconds == 5


async def test_probe_health_marks_healthy(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    probed: list[str] = []

    async def _ok(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        probed.append(url)
        return True, "HTTP 200"

    monkeypatch.setattr(instance, "probe", _ok)
    await instance.probe_health(
        spec={"healthCheck": {"port": 8080, "path": "/healthz"}},
        status=_running_status(),
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )
    # External port persisted by sync_status, not the container port.
    assert probed == ["http://5.6.7.8:40123/healthz"]
    assert patch_obj.status["workerHealthy"] is True
    assert patch_obj.status["healthFailureCount"] == 0


async def test_probe_health_needs_threshold_failures_to_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance

    async def _fail(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        return False, "HTTP 503"

    monkeypatch.setattr(instance, "probe", _fail)
    spec = {"healthCheck": {"port": 8080, "failureThreshold": 3, "intervalSeconds": 0}}

    # Failure 1 and 2: counter grows, workerHealthy untouched.
    p1 = _Patch()
    await instance.probe_health(
        spec=spec, status=_running_status(), patch=p1,
        namespace="default", logger=logging.getLogger("t"),
    )
    assert p1.status["healthFailureCount"] == 1
    assert "workerHealthy" not in p1.status

    p2 = _Patch()
    await instance.probe_health(
        spec=spec,
        status=_running_status(healthFailureCount=1, workerHealthy=True),
        patch=p2,
        namespace="default",
        logger=logging.getLogger("t"),
    )
    assert p2.status["healthFailureCount"] == 2
    assert "workerHealthy" not in p2.status

    # Failure 3: flips to unhealthy.
    p3 = _Patch()
    await instance.probe_health(
        spec=spec,
        status=_running_status(healthFailureCount=2, workerHealthy=True),
        patch=p3,
        namespace="default",
        logger=logging.getLogger("t"),
    )
    assert p3.status["healthFailureCount"] == 3
    assert p3.status["workerHealthy"] is False


async def test_probe_health_respects_initial_delay(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        raise AssertionError("must not probe during initial delay")

    monkeypatch.setattr(instance, "probe", _boom)
    just_launched = dt.datetime.now(dt.UTC).isoformat()
    await instance.probe_health(
        spec={"healthCheck": {"port": 8080, "initialDelaySeconds": 120}},
        status=_running_status(lastLaunchTime=just_launched),
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("t"),
    )
    assert patch_obj.status == {}


async def test_probe_health_skips_when_not_running(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        raise AssertionError("must not probe a stopped instance")

    monkeypatch.setattr(instance, "probe", _boom)
    await instance.probe_health(
        spec={"healthCheck": {"port": 8080}},
        status=_running_status(phase="Stopped"),
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("t"),
    )
    assert patch_obj.status == {}


async def test_probe_health_throttles_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import instance

    calls: list[str] = []

    async def _ok(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        calls.append(url)
        return True, "HTTP 200"

    monkeypatch.setattr(instance, "probe", _ok)
    spec = {"healthCheck": {"port": 8080, "intervalSeconds": 24}}

    # Last probe 5s ago — inside the 24s window: skip.
    recent = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)).isoformat()
    p1 = _Patch()
    await instance.probe_health(
        spec=spec, status=_running_status(lastHealthProbeTime=recent), patch=p1,
        namespace="default", logger=logging.getLogger("t"),
    )
    assert calls == []
    assert p1.status == {}

    # Last probe 25s ago — window elapsed: probe again.
    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=25)).isoformat()
    p2 = _Patch()
    await instance.probe_health(
        spec=spec, status=_running_status(lastHealthProbeTime=stale), patch=p2,
        namespace="default", logger=logging.getLogger("t"),
    )
    assert len(calls) == 1
    assert p2.status["workerHealthy"] is True


async def test_probe_health_noop_without_healthcheck_or_instance(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    from vastai_operator.handlers import instance

    async def _boom(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
        raise AssertionError("must not probe")

    monkeypatch.setattr(instance, "probe", _boom)
    # No healthCheck in spec.
    await instance.probe_health(
        spec={}, status=_running_status(), patch=patch_obj,
        namespace="default", logger=logging.getLogger("t"),
    )
    # healthCheck set but instance not launched yet.
    await instance.probe_health(
        spec={"healthCheck": {"port": 8080}}, status={}, patch=patch_obj,
        namespace="default", logger=logging.getLogger("t"),
    )
    assert patch_obj.status == {}


async def test_sync_status_persists_external_port(
    monkeypatch: pytest.MonkeyPatch, patch_obj: _Patch
) -> None:
    import kopf

    from vastai_operator.handlers import instance

    class _FakeClient:
        async def get_instance(self, instance_id: int) -> InstanceState:
            return InstanceState(
                id=instance_id, status="running", public_ip="5.6.7.8",
                ssh_port=22, dph_total=0.1, ports={8080: 40123},
            )

    def _no_template(ns: str, name: str) -> None:
        raise kopf.TemporaryError("no template in this test")

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())
    monkeypatch.setattr(instance, "fetch_template", _no_template)
    await instance.sync_status(
        spec={
            "templateRef": {"name": "t"},
            "orderRef": {"name": "o"},
            "healthCheck": {"port": 8080},
        },
        status={"instanceId": 7},
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("t"),
    )
    assert patch_obj.status["healthExternalPort"] == 40123
    assert patch_obj.status["phase"] == "Running"


# ---------- VastAlert classification ----------


def test_classify_worker_unhealthy_on_transition() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"WorkerUnhealthy"},
        prev={"lastObservedInstanceId": 1, "lastObservedWorkerHealthy": True},
        current_instance={
            "status": {"phase": "Running", "instanceId": 1, "workerHealthy": False}
        },
    )
    assert event == "WorkerUnhealthy"


def test_classify_worker_unhealthy_not_repeated() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"WorkerUnhealthy"},
        prev={"lastObservedInstanceId": 1, "lastObservedWorkerHealthy": False},
        current_instance={
            "status": {"phase": "Running", "instanceId": 1, "workerHealthy": False}
        },
    )
    assert event is None


def test_classify_worker_recovery() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"WorkerHealthy"},
        prev={"lastObservedInstanceId": 1, "lastObservedWorkerHealthy": False},
        current_instance={
            "status": {"phase": "Running", "instanceId": 1, "workerHealthy": True}
        },
    )
    assert event == "WorkerHealthy"


def test_classify_no_event_when_never_probed() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"WorkerUnhealthy", "WorkerHealthy"},
        prev={"lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Running", "instanceId": 1}},
    )
    assert event is None


def test_classify_worker_event_disabled() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedInstanceId": 1, "lastObservedWorkerHealthy": True},
        current_instance={
            "status": {"phase": "Running", "instanceId": 1, "workerHealthy": False}
        },
    )
    assert event is None


# ---------- recreate destroys still-running rental first ----------


async def test_recreate_destroys_old_instance_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import alert
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    destroyed: list[int] = []

    class _FakeClient:
        def __init__(self, api_key: str) -> None: ...

        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

    async def _fake_launch(spec: dict[str, Any], ns: str) -> LaunchResult:
        return LaunchResult(
            instance_id=999,
            offer_id=11,
            template=TemplateSpec(
                name="t", generation=1, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
            ),
            order=OrderPick(name="o", generation=2, offer_id=11, price_per_hour=0.1),
        )

    monkeypatch.setattr(alert, "VastClient", _FakeClient)
    monkeypatch.setattr(alert, "resolve_api_key", lambda spec, ns: "k")
    monkeypatch.setattr(alert, "launch_instance", _fake_launch)

    patched: list[dict[str, Any]] = []

    class _Custom:
        def patch_namespaced_custom_object_status(self, **kwargs: Any) -> None:
            patched.append(kwargs)

    monkeypatch.setattr(alert, "_custom", lambda: _Custom())

    new_id = await alert._do_recreate(
        namespace="default",
        instance_name="vinst-1",
        instance_obj={
            "spec": {"templateRef": {"name": "t"}, "orderRef": {"name": "o"}},
            "status": {"instanceId": 123, "workerHealthy": False},
        },
        logger=logging.getLogger("test"),
    )
    assert destroyed == [123]
    assert new_id == 999
    body = patched[0]["body"]["status"]
    assert body["workerHealthy"] is None
    assert body["healthFailureCount"] == 0


# ---------- Slack payload: logo + worker styles ----------


def test_slack_payload_uses_vast_logo() -> None:
    from vastai_operator import slack

    payload = slack.render_payload(
        slack.AlertContext(
            event="WorkerUnhealthy",
            instance="i",
            namespace="ns",
            instance_id=1,
            public_ip=None,
            phase="Running",
        )
    )
    attachment = payload["attachments"][0]
    assert attachment["color"] == "#e01e5a"
    header = attachment["blocks"][0]
    assert header["type"] == "context"
    image = header["elements"][0]
    assert image["type"] == "image"
    assert image["image_url"] == slack.VAST_LOGO_URL
    assert image["alt_text"] == "Vast.ai"
    assert ":robot_face:" not in str(payload)


def test_slack_worker_event_styles() -> None:
    from vastai_operator import slack

    unhealthy = slack.render_payload(
        slack.AlertContext(
            event="WorkerUnhealthy", instance="i", namespace="ns",
            instance_id=None, public_ip=None, phase=None,
        )
    )
    healthy = slack.render_payload(
        slack.AlertContext(
            event="WorkerHealthy", instance="i", namespace="ns",
            instance_id=None, public_ip=None, phase=None,
        )
    )
    assert unhealthy["attachments"][0]["color"] == "#e01e5a"
    assert healthy["attachments"][0]["color"] == "#36a64f"
    assert ":face_with_thermometer:" in str(unhealthy)
