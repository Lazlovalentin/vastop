"""Worker health probing for VastInstance.

A "running" Vast.ai instance only means the container was scheduled — the
workload inside may still be dead. When ``spec.healthCheck`` is set, the
instance sync timer probes the worker's HTTP health endpoint through the
instance's public IP and the external port Vast.ai mapped for the configured
container port.

Kopf-free so it can be unit-tested and reused from any controller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthCheckConfig:
    port: int
    path: str = "/healthz"
    scheme: str = "http"
    timeout_seconds: float = 5.0
    initial_delay_seconds: int = 0
    failure_threshold: int = 3
    # 24s = 5 probes per 2 minutes. The dedicated probe timer runs at the
    # operator-wide CONFIG.health_probe_seconds floor; this per-instance value
    # can only throttle further (same pattern as Order refresh).
    interval_seconds: float = 24.0

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> HealthCheckConfig | None:
        raw = spec.get("healthCheck")
        if not raw or not raw.get("port"):
            return None
        path = str(raw.get("path", "/healthz"))
        if not path.startswith("/"):
            path = "/" + path
        return cls(
            port=int(raw["port"]),
            path=path,
            scheme=str(raw.get("scheme", "http")),
            timeout_seconds=float(raw.get("timeoutSeconds", 5)),
            initial_delay_seconds=int(raw.get("initialDelaySeconds", 0)),
            failure_threshold=max(1, int(raw.get("failureThreshold", 3))),
            interval_seconds=float(raw.get("intervalSeconds", 24)),
        )

    def url(self, public_ip: str, external_port: int) -> str:
        return f"{self.scheme}://{public_ip}:{external_port}{self.path}"


async def probe(url: str, *, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    """GET the health endpoint; any 2xx counts as healthy."""
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"probe failed: {type(exc).__name__}: {exc}"
    if 200 <= resp.status_code < 300:
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}"
