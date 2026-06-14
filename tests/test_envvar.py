"""VastEnvVar handler + VastClient env-var wrapper tests (mocked SDK)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------- VastClient env-var wrappers ----------


async def test_create_env_var_passes_name_value() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.create_env_var("HF_TOKEN", "secret-value")
    kwargs = client._sdk.last_create_env_var_kwargs  # type: ignore[attr-defined]
    assert kwargs == {"name": "HF_TOKEN", "value": "secret-value"}


async def test_update_env_var_passes_name_value() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.update_env_var("HF_TOKEN", "rotated")
    kwargs = client._sdk.last_update_env_var_kwargs  # type: ignore[attr-defined]
    assert kwargs == {"name": "HF_TOKEN", "value": "rotated"}


async def test_delete_env_var_swallows_not_found() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")

    def _boom(**_: Any) -> Any:
        raise RuntimeError("404 not found")

    client._sdk.delete_env_var = _boom  # type: ignore[attr-defined]
    # Should NOT raise — idempotent delete.
    await client.delete_env_var("GONE")


async def test_list_env_vars_returns_keys() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    client._sdk.env_vars_response = {"A": "*****", "B": "*****"}  # type: ignore[attr-defined]
    result = await client.list_env_vars()
    assert set(result) == {"A", "B"}
    assert client._sdk.last_show_env_vars_kwargs == {"show_values": False}  # type: ignore[attr-defined]


# ---------- Handler create / update / delete ----------


def _fake_client(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]):
    from vastai_operator.handlers import envvar
    from vastai_operator.vast_client import VastClient

    class _FakeClient(VastClient):
        def __init__(self) -> None:
            pass

        async def create_env_var(self, name: str, value: str) -> dict[str, Any]:
            calls["create"].append((name, value))
            return {}

        async def update_env_var(self, name: str, value: str) -> dict[str, Any]:
            calls["update"].append((name, value))
            return {}

        async def delete_env_var(self, name: str) -> None:
            calls["delete"].append(name)

        async def list_env_vars(self) -> dict[str, Any]:
            return calls.get("existing_response", [{}])[0]

    monkeypatch.setattr(envvar, "_client_for", lambda spec, ns: _FakeClient())
    return _FakeClient


async def test_on_create_inline_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.on_create(
        spec={"value": "abc123"},
        patch=patch_obj,
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [("hf-token", "abc123")]  # key defaults to name
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["vastKey"] == "hf-token"
    assert patch_obj.status["valueHash"] == _hash("abc123")


async def test_on_create_explicit_key_and_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)
    monkeypatch.setattr(
        envvar, "read_secret_value", lambda ns, n, k: "from-secret"
    )

    patch_obj = _Patch()
    await envvar.on_create(
        spec={
            "key": "HF_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "hf", "key": "token"}},
        },
        patch=patch_obj,
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [("HF_TOKEN", "from-secret")]
    assert patch_obj.status["vastKey"] == "HF_TOKEN"


async def test_on_update_value_change_calls_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.on_update(
        spec={"key": "HF_TOKEN", "value": "new"},
        status={"vastKey": "HF_TOKEN", "valueHash": _hash("old")},
        patch=patch_obj,
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["update"] == [("HF_TOKEN", "new")]
    assert calls["create"] == [] and calls["delete"] == []
    assert patch_obj.status["valueHash"] == _hash("new")


async def test_on_update_key_rename_deletes_old_creates_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.on_update(
        spec={"key": "NEW_KEY", "value": "v"},
        status={"vastKey": "OLD_KEY", "valueHash": _hash("v")},
        patch=patch_obj,
        namespace="default",
        name="ev",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [("NEW_KEY", "v")]
    assert calls["delete"] == ["OLD_KEY"]
    assert calls["update"] == []
    assert patch_obj.status["vastKey"] == "NEW_KEY"


async def test_on_update_no_change_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.on_update(
        spec={"key": "K", "value": "same"},
        status={"vastKey": "K", "valueHash": _hash("same")},
        patch=patch_obj,
        namespace="default",
        name="ev",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [] and calls["update"] == [] and calls["delete"] == []
    assert patch_obj.status["phase"] == "Ready"


async def test_on_delete_uses_vast_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    await envvar.on_delete(
        spec={"value": "x"},
        status={"vastKey": "HF_TOKEN"},
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["delete"] == ["HF_TOKEN"]


async def test_reconcile_recreates_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {
        "create": [],
        "update": [],
        "delete": [],
        "existing_response": [{}],  # key NOT present on the account
    }
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.reconcile(
        spec={"key": "HF_TOKEN", "value": "v"},
        status={"phase": "Ready", "vastKey": "HF_TOKEN", "valueHash": _hash("v")},
        patch=patch_obj,
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [("HF_TOKEN", "v")]
    assert patch_obj.status["message"].startswith("Recreated drifted")


async def test_reconcile_repushes_on_value_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {
        "create": [],
        "update": [],
        "delete": [],
        "existing_response": [{"HF_TOKEN": "*****"}],  # present, but value rotated
    }
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.reconcile(
        spec={"key": "HF_TOKEN", "value": "rotated"},
        status={"phase": "Ready", "vastKey": "HF_TOKEN", "valueHash": _hash("old")},
        patch=patch_obj,
        namespace="default",
        name="hf-token",
        logger=logging.getLogger("test"),
    )
    assert calls["update"] == [("HF_TOKEN", "rotated")]
    assert patch_obj.status["valueHash"] == _hash("rotated")


async def test_reconcile_noop_when_in_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {
        "create": [],
        "update": [],
        "delete": [],
        "existing_response": [{"K": "*****"}],
    }
    _fake_client(monkeypatch, calls)

    patch_obj = _Patch()
    await envvar.reconcile(
        spec={"key": "K", "value": "v"},
        status={"phase": "Ready", "vastKey": "K", "valueHash": _hash("v")},
        patch=patch_obj,
        namespace="default",
        name="ev",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [] and calls["update"] == []
    assert patch_obj.status == {}  # nothing patched on a clean reconcile


async def test_resolve_value_requires_source() -> None:
    import kopf

    from vastai_operator.handlers import envvar

    with pytest.raises(kopf.PermanentError):
        envvar._resolve_value({}, "default")
