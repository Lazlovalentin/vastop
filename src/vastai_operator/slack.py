"""Slack incoming-webhook client with rich Block Kit formatting.

Incoming webhooks are bound to a fixed channel and the workspace ignores
`channel` / `username` overrides, so we don't try to set them.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SlackError(RuntimeError):
    """Raised when the Slack webhook returns a non-2xx response."""


# Official Vast.ai logo, used as the message "avatar". Incoming webhooks
# ignore icon_url/username overrides, so the logo is embedded as a Block Kit
# image element in the title row instead.
VAST_LOGO_URL = "https://vast.ai/apple-touch-icon.png"

_EVENT_STYLE: dict[str, tuple[str, str]] = {
    "InstanceTerminated": ("#e01e5a", ":red_circle:"),
    "InstanceFailed":     ("#e01e5a", ":red_circle:"),
    "RentalExpired":      ("#eca538", ":hourglass_flowing_sand:"),
    "InstanceStopped":    ("#eca538", ":pause_button:"),
    "WorkerUnhealthy":    ("#e01e5a", ":face_with_thermometer:"),
    "WorkerHealthy":      ("#36a64f", ":white_check_mark:"),
}
_DEFAULT_STYLE: tuple[str, str] = ("#36a64f", ":information_source:")


def _style_for(event: str) -> tuple[str, str]:
    return _EVENT_STYLE.get(event, _DEFAULT_STYLE)


@dataclass(frozen=True)
class AlertContext:
    event: str
    instance: str
    namespace: str
    instance_id: int | None
    public_ip: str | None
    phase: str | None
    cluster: str | None = None
    custom_title: str | None = None
    custom_summary_template: str | None = None
    extra_buttons: list[dict[str, str]] = field(default_factory=list)


def render_payload(ctx: AlertContext) -> dict[str, Any]:
    color, icon = _style_for(ctx.event)
    title = ctx.custom_title or "*VastAI Operator*"

    if ctx.custom_summary_template:
        summary = _safe_format(
            ctx.custom_summary_template,
            event=ctx.event,
            instance=ctx.instance,
            namespace=ctx.namespace,
            instanceId=ctx.instance_id,
            publicIp=ctx.public_ip,
            phase=ctx.phase,
        )
    else:
        summary = f"{icon} *{ctx.event}* on `{ctx.instance}` (ns `{ctx.namespace}`)"

    detail_lines = []
    if ctx.instance_id is not None:
        detail_lines.append(f"• *Vast.ai instance ID:* `{ctx.instance_id}`")
    if ctx.public_ip:
        detail_lines.append(f"• *Public IP:* `{ctx.public_ip}`")
    if ctx.phase:
        detail_lines.append(f"• *Last known phase:* `{ctx.phase}`")
    detail_block: dict[str, Any] | None = (
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(detail_lines)}}
        if detail_lines
        else None
    )

    when = dt.datetime.now(dt.UTC).strftime("%d %b %Y, %H:%M UTC")
    context_text = f"Reported at: {when}"
    if ctx.cluster:
        context_text += f" · Cluster: {ctx.cluster}"

    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {"type": "image", "image_url": VAST_LOGO_URL, "alt_text": "Vast.ai"},
                {"type": "mrkdwn", "text": title},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]
    if detail_block:
        blocks.append(detail_block)
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context_text}]})

    action_elements: list[dict[str, Any]] = []
    if ctx.instance_id is not None:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open in Vast.ai"},
                "url": f"https://cloud.vast.ai/instances/?selected={ctx.instance_id}",
                "action_id": "vast_open",
            }
        )
    for btn in ctx.extra_buttons:
        action_elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": btn["text"]},
                "url": btn["url"],
                "action_id": btn.get("action_id", f"extra_{len(action_elements)}"),
            }
        )
    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements})

    return {
        "attachments": [{"color": color, "blocks": blocks}],
    }


def _safe_format(template: str, **tokens: Any) -> str:
    safe = {k: ("" if v is None else v) for k, v in tokens.items()}
    try:
        return template.format(**safe)
    except (KeyError, IndexError) as exc:
        logger.warning("Slack template render failed (%s); using raw template", exc)
        return template


async def send_payload(webhook_url: str, payload: dict[str, Any], *, timeout: float = 5.0) -> None:
    if not webhook_url:
        raise SlackError("Slack webhook URL is empty")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(webhook_url, json=payload)
    if resp.status_code >= 300:
        raise SlackError(f"Slack webhook returned {resp.status_code}: {resp.text[:200]}")
    logger.debug("Slack notify ok")


# Backwards-compatible thin wrappers (kept so existing alert handler tests keep working).


@dataclass(frozen=True)
class SlackMessage:
    """Legacy plain-text message wrapper. Prefer ``AlertContext`` + ``render_payload``."""

    text: str
    channel: str | None = None
    username: str | None = None
    icon_emoji: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": self.text}
        if self.channel:
            payload["channel"] = self.channel
        if self.username:
            payload["username"] = self.username
        if self.icon_emoji:
            payload["icon_emoji"] = self.icon_emoji
        return payload


async def send(webhook_url: str, message: SlackMessage, *, timeout: float = 5.0) -> None:
    await send_payload(webhook_url, message.as_payload(), timeout=timeout)


def render(template: str | None, **tokens: Any) -> str:
    if not template:
        template = (
            ":rotating_light: VastInstance *{instance}* (ns *{namespace}*) → "
            "event *{event}*. Last known phase: {phase}. Instance ID: {instanceId}."
        )
    return _safe_format(template, **tokens)
