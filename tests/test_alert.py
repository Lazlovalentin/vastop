"""Tests for VastAlert event classification, cooldown, Slack notify, recreate."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


# ---------- Event classification ----------


def test_classify_terminated_when_instance_disappears() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated"},
        prev={"lastObservedInstanceId": 100, "lastObservedPhase": "Running"},
        current_instance=None,
    )
    assert event == "InstanceTerminated"


def test_classify_no_event_for_first_observation() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"InstanceTerminated", "InstanceFailed"},
        prev={},
        current_instance=None,
    )
    assert event is None


def test_classify_rental_expired_via_status_message() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"RentalExpired", "InstanceTerminated"},
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 5},
        current_instance={
            "status": {
                "phase": "Failed",
                "instanceId": 5,
                "message": "Vast status: rental period ended",
            }
        },
    )
    assert event == "RentalExpired"


def test_classify_instance_failed_only_on_transition() -> None:
    from vastai_operator.handlers.alert import _classify_event

    enabled = {"InstanceFailed"}
    same = _classify_event(
        enabled,
        prev={"lastObservedPhase": "Failed", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Failed", "instanceId": 1, "message": "x"}},
    )
    assert same is None

    new = _classify_event(
        enabled,
        prev={"lastObservedPhase": "Running", "lastObservedInstanceId": 1},
        current_instance={"status": {"phase": "Failed", "instanceId": 1, "message": "x"}},
    )
    assert new == "InstanceFailed"


def test_classify_disabled_event_not_returned() -> None:
    from vastai_operator.handlers.alert import _classify_event

    event = _classify_event(
        {"RentalExpired"},  # InstanceTerminated NOT enabled
        prev={"lastObservedInstanceId": 100, "lastObservedPhase": "Running"},
        current_instance=None,
    )
    assert event is None


# ---------- Cooldown ----------


def test_cooldown_blocks_repeat_event_within_window() -> None:
    from vastai_operator.handlers.alert import _within_cooldown

    now = dt.datetime.now(dt.UTC).isoformat()
    assert _within_cooldown(
        {"lastEvent": "InstanceTerminated", "lastEventTime": now},
        cooldown_seconds=300,
        event="InstanceTerminated",
    )


def test_cooldown_allows_different_event() -> None:
    from vastai_operator.handlers.alert import _within_cooldown

    now = dt.datetime.now(dt.UTC).isoformat()
    assert not _within_cooldown(
        {"lastEvent": "InstanceTerminated", "lastEventTime": now},
        cooldown_seconds=300,
        event="RentalExpired",
    )


def test_cooldown_zero_disables() -> None:
    from vastai_operator.handlers.alert import _within_cooldown

    now = dt.datetime.now(dt.UTC).isoformat()
    assert not _within_cooldown(
        {"lastEvent": "InstanceTerminated", "lastEventTime": now},
        cooldown_seconds=0,
        event="InstanceTerminated",
    )


# ---------- Notify path ----------


async def test_notify_sends_rich_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import alert

    sent: list[tuple[str, dict[str, Any]]] = []

    async def _fake_send_payload(webhook_url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
        sent.append((webhook_url, payload))

    monkeypatch.setattr(alert.slack, "send_payload", _fake_send_payload)
    monkeypatch.setattr(alert, "_read_webhook", lambda ns, ref: "https://hooks/test")

    await alert._do_notify(
        spec={"slackWebhookSecretRef": {"name": "x"}},
        namespace="default",
        event="InstanceTerminated",
        instance_name="vinst-1",
        instance_obj={"status": {"publicIp": "1.2.3.4", "instanceId": 99, "phase": "Failed"}},
        logger=logging.getLogger("test"),
    )
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "https://hooks/test"
    assert payload["attachments"][0]["color"] == "#e01e5a"  # red for terminated
    blocks = payload["attachments"][0]["blocks"]
    types = [b["type"] for b in blocks]
    assert "section" in types and "context" in types and "actions" in types
    # Action button targets the Vast.ai cloud UI
    actions = next(b for b in blocks if b["type"] == "actions")
    assert actions["elements"][0]["url"].endswith("selected=99")


async def test_notify_uses_custom_title_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import alert

    captured: dict[str, Any] = {}

    async def _fake_send_payload(webhook_url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
        captured.update(payload)

    monkeypatch.setattr(alert.slack, "send_payload", _fake_send_payload)
    monkeypatch.setattr(alert, "_read_webhook", lambda ns, ref: "https://hooks/test")

    await alert._do_notify(
        spec={
            "slackWebhookSecretRef": {"name": "x"},
            "customTitle": ":fire: *Custom Title*",
            "customSummaryTemplate": "ev={event} inst={instance}",
        },
        namespace="default",
        event="RentalExpired",
        instance_name="vinst-1",
        instance_obj={"status": {"phase": "Failed", "instanceId": 7}},
        logger=logging.getLogger("test"),
    )
    blocks = captured["attachments"][0]["blocks"]
    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    # Custom title lives in the logo context block (first block).
    title_texts = [
        el["text"]
        for b in blocks
        if b["type"] == "context"
        for el in b["elements"]
        if el["type"] == "mrkdwn"
    ]
    assert ":fire: *Custom Title*" in title_texts
    assert "ev=RentalExpired inst=vinst-1" in section_texts


# ---------- Recreate path ----------


async def test_recreate_calls_launcher_and_patches_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import alert
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    async def _fake_launch(spec: dict[str, Any], ns: str) -> LaunchResult:
        return LaunchResult(
            instance_id=4242,
            offer_id=11,
            template=TemplateSpec(
                name="t", generation=1, image="x", disk_gb=10, env={}, onstart=None, ssh_key=None
            ),
            order=OrderPick(name="o", generation=2, offer_id=11, price_per_hour=0.1),
        )

    monkeypatch.setattr(alert, "launch_instance", _fake_launch)

    patched: list[dict[str, Any]] = []

    class _Custom:
        def patch_namespaced_custom_object_status(self, **kwargs: Any) -> None:
            patched.append(kwargs)

    monkeypatch.setattr(alert, "_custom", lambda: _Custom())

    new_id = await alert._do_recreate(
        namespace="default",
        instance_name="vinst-1",
        instance_obj={"spec": {"templateRef": {"name": "t"}, "orderRef": {"name": "o"}}},
        logger=logging.getLogger("test"),
    )
    assert new_id == 4242
    assert len(patched) == 1
    body = patched[0]["body"]
    assert body["status"]["instanceId"] == 4242
    assert body["status"]["phase"] == "Creating"


async def test_recreate_skips_when_instance_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import alert

    result = await alert._do_recreate(
        namespace="default",
        instance_name="gone",
        instance_obj=None,
        logger=logging.getLogger("test"),
    )
    assert result is None


# ---------- Slack module ----------


def test_render_uses_default_when_template_empty() -> None:
    from vastai_operator import slack

    out = slack.render(None, event="X", instance="i", namespace="n", instanceId=1, publicIp=None, phase="P")
    assert "VastInstance" in out
    assert "*X*" in out


def test_render_handles_missing_token_gracefully() -> None:
    from vastai_operator import slack

    out = slack.render("hello {missing}", event="X")
    # Missing keys cause fallback to raw template
    assert out == "hello {missing}"


async def test_slack_send_raises_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from vastai_operator import slack

    class _Resp:
        status_code = 500
        text = "boom"

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        async def __aenter__(self) -> _Client:
            return self
        async def __aexit__(self, *a: Any) -> None: ...
        async def post(self, url: str, json: dict[str, Any]) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(slack.SlackError):
        await slack.send("https://x", slack.SlackMessage(text="hi"))
