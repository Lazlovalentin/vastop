"""Thin wrapper around the Vast.ai SDK.

The wrapper exists so that:
  * handlers can be unit-tested by injecting a fake client;
  * SDK-shape differences between `vastai` and `vastai-sdk` are isolated here;
  * blocking SDK calls run in a worker thread (kopf handlers are async).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any

try:
    from vastai import VastAI  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - fallback to deprecated package name
    from vastai_sdk import VastAI  # type: ignore[import-not-found,no-redef]

logger = logging.getLogger(__name__)


class VastAPIError(RuntimeError):
    """Raised when the Vast.ai API returns an error or no matching offer."""


def _is_not_found(exc: Exception) -> bool:
    """True when an SDK exception reports a missing resource (404)."""
    msg = str(exc).lower()
    return "not found" in msg or "404" in msg


@dataclass(frozen=True)
class OfferFilters:
    """Structured search filters translated into the Vast.ai query language.

    All ``min_*`` fields are lower bounds; ``None`` means "don't filter".
    ``rental_type`` selects the market: ``on-demand`` (ask) or ``interruptible``
    (bid). RAM sizes are GB — the SDK query parser multiplies cpu_ram/gpu_ram
    by 1000 itself, so we emit GB values verbatim.
    """

    gpu_names: tuple[str, ...] = ()
    num_gpus: int = 1
    min_gpu_ram_gb: float | None = None
    min_cpu_cores: int | None = None
    min_ram_gb: float | None = None
    min_disk_gb: float = 32
    min_download_mbps: float | None = 100
    min_upload_mbps: float | None = None
    min_cpu_ghz: float | None = None
    min_reliability: float | None = None
    countries: tuple[str, ...] = ()
    min_price_per_hour: float | None = None
    max_price_per_hour: float | None = None
    rental_type: str = "on-demand"
    verified_only: bool = True

    @property
    def sdk_offer_type(self) -> str:
        """Vast.ai calls interruptible offers 'bid'."""
        return "bid" if self.rental_type == "interruptible" else "on-demand"

    def with_rental_type(self, rental_type: str) -> OfferFilters:
        return replace(self, rental_type=rental_type)

    def to_query(self) -> str:
        parts = [
            f"num_gpus={self.num_gpus}",
            f"disk_space>={self.min_disk_gb}",
            "rentable=true",
        ]
        if len(self.gpu_names) == 1:
            parts.append(f"gpu_name={self.gpu_names[0]}")
        elif self.gpu_names:
            parts.append(f"gpu_name in [{','.join(self.gpu_names)}]")
        if self.min_gpu_ram_gb is not None:
            parts.append(f"gpu_ram>={self.min_gpu_ram_gb}")
        if self.min_cpu_cores is not None:
            parts.append(f"cpu_cores>={self.min_cpu_cores}")
        if self.min_ram_gb is not None:
            parts.append(f"cpu_ram>={self.min_ram_gb}")
        if self.min_download_mbps is not None:
            parts.append(f"inet_down>={self.min_download_mbps}")
        if self.min_upload_mbps is not None:
            parts.append(f"inet_up>={self.min_upload_mbps}")
        if self.min_cpu_ghz is not None:
            parts.append(f"cpu_ghz>={self.min_cpu_ghz}")
        if self.min_reliability is not None:
            parts.append(f"reliability>={self.min_reliability}")
        if len(self.countries) == 1:
            parts.append(f"geolocation={self.countries[0]}")
        elif self.countries:
            parts.append(f"geolocation in [{','.join(self.countries)}]")
        if self.min_price_per_hour is not None:
            parts.append(f"dph_total>={self.min_price_per_hour}")
        if self.max_price_per_hour is not None:
            parts.append(f"dph_total<={self.max_price_per_hour}")
        # `verified=any` clears the SDK's seeded verified=true default.
        parts.append("verified=true" if self.verified_only else "verified=any")
        return " ".join(parts)


@dataclass(frozen=True)
class Offer:
    id: int
    gpu_name: str
    num_gpus: int
    dph_total: float
    disk_space: float
    inet_down: float
    inet_up: float = 0.0
    min_bid: float = 0.0
    cpu_cores: int = 0
    cpu_ram_gb: float = 0.0
    gpu_ram_gb: float = 0.0
    geolocation: str = ""
    reliability: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Offer:
        return cls(
            id=int(raw["id"]),
            gpu_name=str(raw.get("gpu_name", "")),
            num_gpus=int(raw.get("num_gpus", 1)),
            dph_total=float(raw.get("dph_total", 0.0)),
            disk_space=float(raw.get("disk_space", 0.0)),
            inet_down=float(raw.get("inet_down", 0.0)),
            inet_up=float(raw.get("inet_up") or 0.0),
            min_bid=float(raw.get("min_bid") or 0.0),
            cpu_cores=int(raw.get("cpu_cores") or 0),
            # Raw API reports cpu_ram/gpu_ram in MB.
            cpu_ram_gb=round(float(raw.get("cpu_ram") or 0.0) / 1024, 1),
            gpu_ram_gb=round(float(raw.get("gpu_ram") or 0.0) / 1024, 1),
            geolocation=str(raw.get("geolocation") or ""),
            reliability=float(raw.get("reliability2") or raw.get("reliability") or 0.0),
        )


@dataclass(frozen=True)
class InstanceState:
    id: int
    status: str
    public_ip: str | None
    ssh_port: int | None
    dph_total: float
    # Container port -> externally reachable host port, e.g. {8080: 40123}.
    ports: dict[int, int] | None = None
    # Rental contract end as unix epoch; None for open-ended rentals.
    end_date: float | None = None

    def external_port(self, container_port: int) -> int | None:
        return (self.ports or {}).get(container_port)

    @staticmethod
    def _parse_ports(raw_ports: Any) -> dict[int, int] | None:
        """Vast.ai mirrors docker's port map: {'8080/tcp': [{'HostPort': '40123'}]}."""
        if not isinstance(raw_ports, dict):
            return None
        parsed: dict[int, int] = {}
        for key, bindings in raw_ports.items():
            try:
                container = int(str(key).split("/", 1)[0])
            except ValueError:
                continue
            if isinstance(bindings, list) and bindings:
                host_port = bindings[0].get("HostPort")
                if host_port:
                    try:
                        parsed[container] = int(host_port)
                    except ValueError:
                        continue
        return parsed or None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> InstanceState:
        return cls(
            id=int(raw["id"]),
            status=str(raw.get("actual_status") or raw.get("intended_status") or "unknown"),
            public_ip=raw.get("public_ipaddr") or None,
            ssh_port=int(raw["ssh_port"]) if raw.get("ssh_port") else None,
            dph_total=float(raw.get("dph_total", 0.0)),
            ports=cls._parse_ports(raw.get("ports")),
            end_date=float(raw["end_date"]) if raw.get("end_date") else None,
        )


class VastClient:
    """Async-friendly facade over the synchronous Vast.ai SDK."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise VastAPIError("Vast.ai API key is empty")
        self._sdk = VastAI(api_key=api_key)

    async def _run(self, fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def search_offers(self, filters: OfferFilters) -> list[Offer]:
        try:
            raw = await self._run(
                self._sdk.search_offers,
                query=filters.to_query(),
                type=filters.sdk_offer_type,
                order="dph_total",
            )
        except Exception as exc:
            raise VastAPIError(f"search_offers failed: {exc}") from exc
        offers = [Offer.from_api(o) for o in self._as_list(raw)]
        offers.sort(key=lambda o: o.dph_total)
        return offers

    async def create_instance(
        self,
        *,
        offer_id: int,
        image: str | None,
        disk_gb: int,
        env: dict[str, str] | None,
        onstart: str | None,
        ssh_key: str | None,
        runtype: str = "ssh",
        template_hash_id: str | None = None,
        bid_price_per_hour: float | None = None,
    ) -> int:
        kwargs: dict[str, Any] = {
            "id": offer_id,
            "disk": disk_gb,
            "runtype": runtype,
        }
        if bid_price_per_hour is not None:
            # Interruptible (bid) rental: the offer only runs while our bid
            # stays above the host's min_bid.
            kwargs["price"] = bid_price_per_hour
        if template_hash_id:
            kwargs["template_hash"] = template_hash_id
        if image:
            kwargs["image"] = image
        if env:
            kwargs["env"] = dict(env)
        if onstart:
            kwargs["onstart_cmd"] = onstart
        if ssh_key:
            kwargs["extra"] = {"ssh_key": ssh_key}

        try:
            raw = await self._run(self._sdk.create_instance, **kwargs)
        except Exception as exc:
            raise VastAPIError(f"create_instance failed: {exc}") from exc
        instance_id = self._extract_instance_id(raw)
        if instance_id is None:
            raise VastAPIError(f"create_instance returned no id: {raw!r}")
        logger.info("Created Vast.ai instance %s from offer %s", instance_id, offer_id)
        return instance_id

    async def get_instance(self, instance_id: int) -> InstanceState | None:
        try:
            raw = await self._run(self._sdk.show_instance, id=instance_id)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise VastAPIError(f"show_instance failed: {exc}") from exc
        if not raw:
            return None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not raw:
            return None
        return InstanceState.from_api(raw)

    @staticmethod
    def _build_env_string(env: dict[str, str] | None, ports: list[int] | None) -> str | None:
        """Vast.ai templates take a single docker-options string for env+ports."""
        parts: list[str] = []
        if env:
            parts.extend(f"-e {k}={v}" for k, v in env.items())
        if ports:
            parts.extend(f"-p {int(p)}:{int(p)}" for p in ports)
        return " ".join(parts) if parts else None

    async def create_template(
        self,
        *,
        name: str,
        image: str,
        runtype: str,
        disk_gb: int,
        env: dict[str, str] | None,
        ports: list[int] | None,
        onstart: str | None,
        description: str | None,
        private: bool,
        image_login: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "name": name,
            "image": image,
            "disk_space": float(disk_gb),
            "public": not private,
        }
        if runtype == "ssh":
            kwargs["ssh"] = True
        elif runtype == "jupyter":
            kwargs["jupyter"] = True
        env_str = self._build_env_string(env, ports)
        if env_str:
            kwargs["env"] = env_str
        if onstart:
            kwargs["onstart_cmd"] = onstart
        if description:
            kwargs["desc"] = description
        if image_login:
            kwargs["login"] = image_login
        try:
            raw = await self._run(self._sdk.create_template, **kwargs)
        except Exception as exc:
            raise VastAPIError(f"create_template failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise VastAPIError(f"create_template returned unexpected shape: {raw!r}")
        return raw

    async def update_template(
        self,
        *,
        hash_id: str,
        name: str,
        image: str,
        runtype: str,
        disk_gb: int,
        env: dict[str, str] | None,
        ports: list[int] | None,
        onstart: str | None,
        description: str | None,
        private: bool,
        image_login: str | None,
    ) -> dict[str, Any]:
        # SDK's update_template is a thin wrapper that does NOT translate
        # high-level flags (ssh=True/jupyter=True) the way create_template does.
        # It forwards directly to ``offers.update_template``, which expects the
        # flat fields: runtype, use_ssh, use_jupyter_lab, etc.
        use_ssh = runtype in ("ssh", "jupyter")
        use_jupyter_lab = runtype == "jupyter"
        kwargs: dict[str, Any] = {
            "name": name,
            "image": image,
            "disk_space": float(disk_gb),
            "private": private,
            "runtype": runtype,
            "use_ssh": use_ssh,
            "use_jupyter_lab": use_jupyter_lab,
        }
        env_str = self._build_env_string(env, ports)
        if env_str:
            kwargs["env"] = env_str
        if onstart:
            kwargs["onstart_cmd"] = onstart
        if description:
            kwargs["desc"] = description
        if image_login:
            # offers.update_template wants docker_login_repo (just the registry token).
            kwargs["docker_login_repo"] = image_login.split(" ", 1)[0]
        try:
            raw = await self._run(self._sdk.update_template, hash_id=hash_id, **kwargs)
        except Exception as exc:
            raise VastAPIError(f"update_template failed: {exc}") from exc
        return raw if isinstance(raw, dict) else {}

    async def delete_template(
        self,
        *,
        hash_id: str | None = None,
        template_id: int | None = None,
    ) -> None:
        if not hash_id and not template_id:
            raise VastAPIError("delete_template requires hash_id or template_id")
        kwargs: dict[str, Any] = {}
        if template_id is not None:
            kwargs["template_id"] = template_id
        else:
            kwargs["hash_id"] = hash_id
        try:
            await self._run(self._sdk.delete_template, **kwargs)
        except Exception as exc:
            if _is_not_found(exc):
                logger.info("Vast.ai template %s already gone", hash_id or template_id)
                return
            raise VastAPIError(f"delete_template failed: {exc}") from exc

    # ---- Account-level environment variables (Vast.ai "secrets", /secrets/) ----

    async def create_env_var(self, name: str, value: str) -> dict[str, Any]:
        """Create an account-level Vast.ai env var (POST /secrets/)."""
        try:
            raw = await self._run(self._sdk.create_env_var, name=name, value=value)
        except Exception as exc:
            raise VastAPIError(f"create_env_var failed: {exc}") from exc
        return raw if isinstance(raw, dict) else {}

    async def update_env_var(self, name: str, value: str) -> dict[str, Any]:
        """Update an existing account-level env var (PUT /secrets/)."""
        try:
            raw = await self._run(self._sdk.update_env_var, name=name, value=value)
        except Exception as exc:
            raise VastAPIError(f"update_env_var failed: {exc}") from exc
        return raw if isinstance(raw, dict) else {}

    async def delete_env_var(self, name: str) -> None:
        """Delete an account-level env var (DELETE /secrets/). Idempotent."""
        try:
            await self._run(self._sdk.delete_env_var, name=name)
        except Exception as exc:
            if _is_not_found(exc):
                logger.info("Vast.ai env var %s already gone", name)
                return
            raise VastAPIError(f"delete_env_var failed: {exc}") from exc

    async def list_env_vars(self) -> dict[str, Any]:
        """Return account env vars keyed by name (values masked — existence check)."""
        try:
            raw = await self._run(self._sdk.show_env_vars, show_values=False)
        except Exception as exc:
            raise VastAPIError(f"show_env_vars failed: {exc}") from exc
        return raw if isinstance(raw, dict) else {}

    async def destroy_instance(self, instance_id: int) -> None:
        try:
            await self._run(self._sdk.destroy_instance, id=instance_id)
        except Exception as exc:
            if _is_not_found(exc):
                logger.info("Instance %s already gone", instance_id)
                return
            raise VastAPIError(f"destroy_instance failed: {exc}") from exc

    @staticmethod
    def _as_list(raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and "offers" in raw:
            return list(raw["offers"])
        return [raw]

    @staticmethod
    def _extract_instance_id(raw: Any) -> int | None:
        if isinstance(raw, dict):
            for key in ("new_contract", "id", "instance_id"):
                if key in raw and raw[key] is not None:
                    return int(raw[key])
        if isinstance(raw, int):
            return raw
        return None
