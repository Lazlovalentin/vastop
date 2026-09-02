"""Cross-resource lookup helpers used by handlers.

VastInstance points at a VastTemplate (config) and a VastOrder (offer list) via
``templateRef`` / ``orderRef``. This module fetches those resources from the
API server and provides convenience accessors.
"""

from __future__ import annotations

import base64
import json
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


def fetch_order_pick(
    namespace: str, name: str, offer_id: int | None = None
) -> OrderPick:
    """Return an offer the referenced VastOrder currently knows about.

    Default: the cheapest offer (``status.cheapestOfferId``). When ``offer_id``
    is given (VastInstanceGroup pins a specific offer per replica), that exact
    offer must still be present in ``status.matchingOffers`` — otherwise it was
    taken/expired and a TemporaryError lets the caller reassign.
    """
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

    if offer_id is not None:
        match = next(
            (
                o
                for o in (status.get("matchingOffers") or [])
                if o.get("id") is not None and int(o["id"]) == int(offer_id)
            ),
            None,
        )
        if match is None:
            raise kopf.TemporaryError(
                f"VastOrder {namespace}/{name} offer {offer_id} no longer available",
                delay=60,
            )
        chosen_id = int(offer_id)
        chosen_price = float(match.get("pricePerHour") or 0.0)
    else:
        cheapest_id = status.get("cheapestOfferId")
        if not cheapest_id:
            raise kopf.TemporaryError(
                f"VastOrder {namespace}/{name} has no matching offers yet", delay=60
            )
        chosen_id = int(cheapest_id)
        chosen_price = float(status.get("cheapestPricePerHour") or 0.0)

    bid = status.get("effectiveBidPerHour")
    return OrderPick(
        name=name,
        generation=int(obj.get("metadata", {}).get("generation", 0)),
        offer_id=chosen_id,
        price_per_hour=chosen_price,
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


DOCKER_CONFIG_KEY = ".dockerconfigjson"


def read_secret_data(namespace: str, name: str) -> dict[str, str]:
    """Every key of a namespaced Secret, base64-decoded."""
    core = k8s_client.CoreV1Api()
    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
    except k8s_client.ApiException as exc:
        raise kopf.TemporaryError(
            f"Cannot read secret {namespace}/{name}: {exc}", delay=30
        ) from exc
    return {
        key: base64.b64decode(value).decode("utf-8")
        for key, value in (secret.data or {}).items()
    }


def docker_login_from_config(config_json: str, registry: str | None) -> str:
    """Turn a kubernetes.io/dockerconfigjson payload into the Vast.ai login form.

    Vast.ai wants one "<registry> <username> <password>" string. A pull secret
    already holds exactly that, base64-encoded in auths[registry].auth, so a
    cluster that can pull the image already has everything needed and no second
    copy of the credential has to be created and kept in step.

    With several registries in one secret the one serving the image wins;
    otherwise a lone entry is unambiguous.
    """
    try:
        auths = (json.loads(config_json) or {}).get("auths") or {}
    except ValueError as exc:
        raise kopf.PermanentError(f"Malformed docker config: {exc}") from exc
    if not auths:
        raise kopf.PermanentError("Docker config has no auths entry")

    chosen = registry if registry in auths else None
    if chosen is None:
        if len(auths) > 1:
            raise kopf.PermanentError(
                f"Docker config has {len(auths)} registries and none matches "
                f"{registry!r}; set imageLoginSecretRef.key to a literal login"
            )
        chosen = next(iter(auths))

    entry = auths[chosen] or {}
    auth = entry.get("auth")
    if auth:
        decoded = base64.b64decode(auth).decode("utf-8")
        username, _, password = decoded.partition(":")
    else:
        username = entry.get("username", "")
        password = entry.get("password", "")
    if not username or not password:
        raise kopf.PermanentError(
            f"Docker config entry for {chosen} has no usable credentials"
        )
    return f"{chosen} {username} {password}"


def read_docker_login(
    namespace: str, name: str, registry: str | None = None
) -> str:
    data = read_secret_data(namespace, name)
    if DOCKER_CONFIG_KEY not in data:
        raise kopf.PermanentError(
            f"Secret {namespace}/{name} has no {DOCKER_CONFIG_KEY}"
        )
    return docker_login_from_config(data[DOCKER_CONFIG_KEY], registry)
