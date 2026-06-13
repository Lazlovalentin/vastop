"""Runtime configuration for the operator."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorConfig:
    api_group: str = "vast.ai"
    api_version: str = "v1alpha1"
    kind_plural: str = "vastinstances"
    finalizer: str = "vast.ai/instance-finalizer"
    sync_interval_seconds: float = 60.0
    search_timeout_seconds: float = 30.0
    order_refresh_seconds: float = 300.0
    # Worker health probe cadence: 24s = 5 probes per 2 minutes. This is the
    # global floor; spec.healthCheck.intervalSeconds can only slow it down.
    health_probe_seconds: float = 24.0
    default_api_key_env: str = "VAST_API_KEY"

    @classmethod
    def from_env(cls) -> OperatorConfig:
        return cls(
            sync_interval_seconds=float(os.getenv("VAST_SYNC_INTERVAL", "60")),
            search_timeout_seconds=float(os.getenv("VAST_SEARCH_TIMEOUT", "30")),
            order_refresh_seconds=float(os.getenv("VAST_ORDER_REFRESH", "300")),
            health_probe_seconds=float(os.getenv("VAST_HEALTH_PROBE_INTERVAL", "24")),
        )


CONFIG = OperatorConfig.from_env()
