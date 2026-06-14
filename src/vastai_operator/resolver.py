"""Cross-resource lookup helpers used by handlers.

VastInstance points at a VastTemplate (config) and a VastOrder (offer list) via
``templateRef`` / ``orderRef``. This module fetches those resources from the
API server and provides convenience accessors.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

import kopf
from kubernetes import client as k8s_client

from .config import CONFIG

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemplateSpec:
    name: str
    generation: int
    image: str | None
    disk_gb: int
    env: dict[str, str]
    onstart: str | None
    ssh_key: str | None
    runtype: str = "ssh"
    template_hash_id: str | None = None
    template_phase: str | None = None

    @property
    def resolved_marker(self) -> str:
        return f"{self.name}@{self.generation}"

    @property
    def is_ready(self) -> bool:
        return self.template_phase == "Ready" and bool(self.template_hash_id)


@dataclass(frozen=True)
class OrderPick:
    name: str
    generation: int
    offer_id: int
    price_per_hour: float
    rental_type: str = "on-demand"
    bid_price_per_hour: float | None = None

    @property
    def resolved_marker(self) -> str:
        return f"{self.name}@{self.generation}#{self.offer_id}"


def _custom() -> k8s_client.CustomObjectsApi:
    return k8s_client.CustomObjectsApi()


def fetch_template(namespace: str, name: str) -> TemplateSpec:
    try:
        obj = _custom().get_namespaced_custom_object(
            group=CONFIG.api_group,
            version=CONFIG.api_version,
            namespace=namespace,
            plural="vasttemplates",
            name=name,
        )
    except k8s_client.ApiException as exc:
        if exc.status == 404:
            raise kopf.TemporaryError(
                f"VastTemplate {namespace}/{name} not found", delay=30
            ) from exc
        raise kopf.TemporaryError(f"Error reading VastTemplate: {exc}", delay=30) from exc

    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    template_hash = status.get("vastTemplateHash")
    template_phase = status.get("phase")
    if not template_hash:
        raise kopf.TemporaryError(
            f"VastTemplate {namespace}/{name} not yet synced to Vast.ai (no hash)", delay=15
        )
    if template_phase != "Ready":
        raise kopf.TemporaryError(
            f"VastTemplate {namespace}/{name} phase={template_phase}, waiting for Ready", delay=15
        )
    return TemplateSpec(
        name=name,
        generation=int(obj.get("metadata", {}).get("generation", 0)),
        image=spec.get("image"),
        disk_gb=int(spec.get("diskGB", 32)),
        env=dict(spec.get("env") or {}),
        onstart=spec.get("onstart"),
        ssh_key=spec.get("sshKey"),
        runtype=spec.get("runtype", "ssh"),
        template_hash_id=template_hash,
        template_phase=template_phase,
    )


def fetch_order_pick(namespace: str, name: str) -> OrderPick:
    """Return the cheapest offer the referenced VastOrder currently knows about."""
    try:
        obj = _custom().get_namespaced_custom_object(
            group=CONFIG.api_group,
            version=CONFIG.api_version,
            namespace=namespace,
            plural="vastorders",
            name=name,
        )
    except k8s_client.ApiException as exc:
        if exc.status == 404:
            raise kopf.TemporaryError(
                f"VastOrder {namespace}/{name} not found", delay=30
            ) from exc
        raise kopf.TemporaryError(f"Error reading VastOrder: {exc}", delay=30) from exc

    status = obj.get("status") or {}
    cheapest_id = status.get("cheapestOfferId")
    cheapest_price = status.get("cheapestPricePerHour")
    if not cheapest_id:
        raise kopf.TemporaryError(
            f"VastOrder {namespace}/{name} has no matching offers yet", delay=60
        )

    bid = status.get("effectiveBidPerHour")
    return OrderPick(
        name=name,
        generation=int(obj.get("metadata", {}).get("generation", 0)),
        offer_id=int(cheapest_id),
        price_per_hour=float(cheapest_price or 0.0),
        rental_type=status.get("rentalTypeInUse") or "on-demand",
        bid_price_per_hour=float(bid) if bid is not None else None,
    )


def read_secret_value(namespace: str, name: str, key: str) -> str:
    """Read and base64-decode a single key from a namespaced Secret.

    Missing Secret → TemporaryError (retry); missing key → PermanentError.
    Shared by API-key resolution and VastEnvVar value sourcing.
    """
    core = k8s_client.CoreV1Api()
    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
    except k8s_client.ApiException as exc:
        raise kopf.TemporaryError(
            f"Cannot read secret {namespace}/{name}: {exc}", delay=30
        ) from exc

    raw = (secret.data or {}).get(key)
    if not raw:
        raise kopf.PermanentError(f"Secret {namespace}/{name} missing key {key}")
    return base64.b64decode(raw).decode("utf-8").strip()


def resolve_api_key(spec: dict[str, Any], namespace: str) -> str:
    ref = spec.get("apiKeySecretRef")
    if not ref:
        env_key = os.getenv(CONFIG.default_api_key_env)
        if not env_key:
            raise kopf.PermanentError(
                "No apiKeySecretRef set and VAST_API_KEY env var is empty"
            )
        return env_key

    return read_secret_value(namespace, ref["name"], ref.get("key", "VAST_API_KEY"))
