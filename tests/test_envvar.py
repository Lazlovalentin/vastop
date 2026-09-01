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


async def test_on_create_value_template_substitutes_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)
    secrets = {("default", "redis-auth", "redis-password"): "s3cr3t"}
    monkeypatch.setattr(
        envvar, "read_secret_value", lambda ns, n, k: secrets[(ns, n, k)]
    )

    patch_obj = _Patch()
    await envvar.on_create(
        spec={
            "key": "REDIS_URL",
            "valueTemplate": {
                "template": "redis://:$(PASSWORD)@1.2.3.4:6379/0",
                "vars": [
                    {
                        "name": "PASSWORD",
                        "secretKeyRef": {
                            "name": "redis-auth",
                            "key": "redis-password",
                        },
                    }
                ],
            },
        },
        patch=patch_obj,
        namespace="default",
        name="redis-url",
        logger=logging.getLogger("test"),
    )
    assert calls["create"] == [("REDIS_URL", "redis://:s3cr3t@1.2.3.4:6379/0")]
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["valueHash"] == _hash("redis://:s3cr3t@1.2.3.4:6379/0")


def test_resolve_value_template_multiple_vars_and_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    secrets = {
        ("ns", "auth", "user"): "alice",
        ("ns", "auth", "pass"): "pw",
    }
    monkeypatch.setattr(
        envvar, "read_secret_value", lambda ns, n, k: secrets[(ns, n, k)]
    )
    value = envvar._resolve_value(
        {
            "valueTemplate": {
                "template": "amqp://$(USER):$(PASS)@host/$(UNKNOWN)",
                "vars": [
                    {"name": "USER", "secretKeyRef": {"name": "auth", "key": "user"}},
                    {"name": "PASS", "secretKeyRef": {"name": "auth", "key": "pass"}},
                ],
            }
        },
        "ns",
    )
    # Known vars substituted; unreferenced placeholders stay literal.
    assert value == "amqp://alice:pw@host/$(UNKNOWN)"


def test_resolve_value_template_without_vars_is_literal() -> None:
    from vastai_operator.handlers import envvar

    value = envvar._resolve_value(
        {"valueTemplate": {"template": "plain-text"}}, "ns"
    )
    assert value == "plain-text"


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


# ---------- API key redaction ----------


def test_redact_strips_the_api_key_from_a_url() -> None:
    from vastai_operator.vast_client import redact

    text = (
        "400 Client Error: Bad Request for url: "
        "https://console.vast.ai/api/v0/secrets/?api_key=deadbeefcafe1234"
    )
    out = redact(text)
    assert "deadbeefcafe1234" not in out
    assert "api_key=<redacted>" in out


def test_redact_keeps_following_query_parameters() -> None:
    from vastai_operator.vast_client import redact

    out = redact("https://x/y?api_key=abc123&owner=me")
    assert out == "https://x/y?api_key=<redacted>&owner=me"


def test_redact_is_a_noop_without_a_key() -> None:
    from vastai_operator.vast_client import redact

    assert redact("plain message") == "plain message"


def test_vast_api_error_redacts_its_own_message() -> None:
    from vastai_operator.vast_client import VastAPIError

    exc = VastAPIError("create_env_var failed: url ?api_key=supersecret")
    assert "supersecret" not in str(exc)
    assert "<redacted>" in str(exc)


# ---------- spec.encoding ----------


def test_resolve_value_defaults_to_no_encoding() -> None:
    from vastai_operator.handlers import envvar

    assert envvar._resolve_value({"value": "plain"}, "default") == "plain"


def test_resolve_value_base64_encodes_inline_value() -> None:
    import base64

    from vastai_operator.handlers import envvar

    out = envvar._resolve_value(
        {"value": "line1\nline2", "encoding": "base64"}, "default"
    )
    assert "\n" not in out
    assert base64.b64decode(out).decode() == "line1\nline2"


def test_resolve_value_base64_encodes_secret_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from vastai_operator.handlers import envvar

    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    monkeypatch.setattr(envvar, "read_secret_value", lambda ns, n, k: pem)
    out = envvar._resolve_value(
        {
            "valueFrom": {"secretKeyRef": {"name": "tls", "key": "tls.crt"}},
            "encoding": "base64",
        },
        "default",
    )
    assert "\n" not in out
    assert base64.b64decode(out).decode() == pem


def test_resolve_value_base64_encodes_template_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from vastai_operator.handlers import envvar

    monkeypatch.setattr(envvar, "read_secret_value", lambda ns, n, k: "s3cret")
    out = envvar._resolve_value(
        {
            "valueTemplate": {
                "template": "a=$(P)\nb=2",
                "vars": [
                    {"name": "P", "secretKeyRef": {"name": "x", "key": "y"}}
                ],
            },
            "encoding": "base64",
        },
        "default",
    )
    assert base64.b64decode(out).decode() == "a=s3cret\nb=2"


def test_unsupported_encoding_is_permanent() -> None:
    import kopf

    from vastai_operator.handlers import envvar

    with pytest.raises(kopf.PermanentError):
        envvar._resolve_value({"value": "x", "encoding": "rot13"}, "default")


async def test_on_create_pushes_the_encoded_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)
    patch = _Patch()

    await envvar.on_create(
        spec={"key": "PEM", "value": "a\nb", "encoding": "base64"},
        patch=patch,
        namespace="default",
        name="pem",
        logger=logging.getLogger("test"),
    )
    (name, value), = calls["create"]
    assert name == "PEM"
    assert "\n" not in value
    assert base64.b64decode(value).decode() == "a\nb"
    assert patch.status["valueHash"] == _hash(value)


# ---------- deletion never wedges the resource ----------


async def test_on_delete_without_vast_key_survives_an_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar
    from vastai_operator.vast_client import VastAPIError, VastClient

    class _Failing(VastClient):
        def __init__(self) -> None:
            pass

        async def delete_env_var(self, name: str) -> None:
            raise VastAPIError("delete_env_var failed: 400 Client Error")

    monkeypatch.setattr(envvar, "_client_for", lambda spec, ns: _Failing())

    await envvar.on_delete(
        spec={"key": "NEVER_CREATED", "value": "x"},
        status={"phase": "Failed"},
        namespace="default",
        name="never-created",
        logger=logging.getLogger("test"),
    )


async def test_on_delete_with_vast_key_still_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar
    from vastai_operator.vast_client import VastAPIError, VastClient

    class _Failing(VastClient):
        def __init__(self) -> None:
            pass

        async def delete_env_var(self, name: str) -> None:
            raise VastAPIError("delete_env_var failed: 500 Server Error")

    monkeypatch.setattr(envvar, "_client_for", lambda spec, ns: _Failing())

    with pytest.raises(VastAPIError):
        await envvar.on_delete(
            spec={"key": "REAL", "value": "x"},
            status={"vastKey": "REAL", "phase": "Ready"},
            namespace="default",
            name="real",
            logger=logging.getLogger("test"),
        )


async def test_on_delete_without_vast_key_still_attempts_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    await envvar.on_delete(
        spec={"key": "ORPHAN", "value": "x"},
        status={},
        namespace="default",
        name="orphan",
        logger=logging.getLogger("test"),
    )
    assert calls["delete"] == ["ORPHAN"]


# ---------- chunking around the 256-character value cap ----------


def _joined(entries: dict[str, str], key: str) -> str:
    count = int(entries[f"{key}_CHUNKS"])
    return "".join(entries[f"{key}_{i}"] for i in range(count))


def test_unchunked_value_is_a_single_entry() -> None:
    from vastai_operator.handlers import envvar

    key, entries, count = envvar._desired_entries(
        {"key": "PLAIN", "value": "short"}, "plain", "default"
    )
    assert key == "PLAIN"
    assert entries == {"PLAIN": "short"}
    assert count == 0


def test_chunked_short_value_still_uses_the_chunked_layout() -> None:
    from vastai_operator.handlers import envvar

    key, entries, count = envvar._desired_entries(
        {"key": "C", "value": "tiny", "chunked": True}, "c", "default"
    )
    assert count == 1
    assert entries == {"C_CHUNKS": "1", "C_0": "tiny"}
    assert _joined(entries, key) == "tiny"


def test_chunked_value_splits_at_the_limit_and_round_trips() -> None:
    from vastai_operator.handlers import envvar

    value = "x" * 700
    key, entries, count = envvar._desired_entries(
        {"key": "C", "value": value, "chunked": True}, "c", "default"
    )
    assert count == 3
    assert entries["C_CHUNKS"] == "3"
    assert all(
        len(v) <= envvar.CHUNK_LIMIT
        for k, v in entries.items()
        if k != "C_CHUNKS"
    )
    assert _joined(entries, key) == value


def test_chunked_base64_pem_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from vastai_operator.handlers import envvar

    pem = (
        "-----BEGIN CERTIFICATE-----\n"
        + "\n".join("MIIB" + "a" * 60 for _ in range(9))
        + "\n-----END CERTIFICATE-----\n"
    )
    monkeypatch.setattr(envvar, "read_secret_value", lambda ns, n, k: pem)
    key, entries, count = envvar._desired_entries(
        {
            "key": "REDIS_CLIENT_CERT",
            "valueFrom": {"secretKeyRef": {"name": "tls", "key": "tls.crt"}},
            "encoding": "base64",
            "chunked": True,
        },
        "cert",
        "default",
    )
    assert count > 1
    assert base64.b64decode(_joined(entries, key)).decode() == pem


async def test_on_create_registers_every_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)
    patch = _Patch()

    await envvar.on_create(
        spec={"key": "C", "value": "y" * 600, "chunked": True},
        patch=patch,
        namespace="default",
        name="c",
        logger=logging.getLogger("test"),
    )
    created = dict(calls["create"])
    assert created["C_CHUNKS"] == "3"
    assert set(created) == {"C_CHUNKS", "C_0", "C_1", "C_2"}
    assert patch.status["chunkCount"] == 3
    assert patch.status["vastKey"] == "C"


async def test_on_update_removes_chunks_that_are_no_longer_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)
    patch = _Patch()

    await envvar.on_update(
        spec={"key": "C", "value": "z" * 300, "chunked": True},
        status={"vastKey": "C", "chunkCount": 4, "valueHash": "stale"},
        patch=patch,
        namespace="default",
        name="c",
        logger=logging.getLogger("test"),
    )
    assert patch.status["chunkCount"] == 2
    assert sorted(calls["delete"]) == ["C_2", "C_3"]


async def test_on_delete_removes_every_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    _fake_client(monkeypatch, calls)

    await envvar.on_delete(
        spec={"key": "C", "value": "x", "chunked": True},
        status={"vastKey": "C", "chunkCount": 3},
        namespace="default",
        name="c",
        logger=logging.getLogger("test"),
    )
    assert sorted(calls["delete"]) == ["C_0", "C_1", "C_2", "C_CHUNKS"]


async def test_reconcile_recreates_only_the_missing_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vastai_operator.handlers import envvar

    calls: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
    calls["existing_response"] = [{"C_CHUNKS": "", "C_0": "", "C_2": ""}]
    _fake_client(monkeypatch, calls)
    patch = _Patch()

    value = "q" * 600
    await envvar.reconcile(
        spec={"key": "C", "value": value, "chunked": True},
        status={
            "phase": "Ready",
            "vastKey": "C",
            "chunkCount": 3,
            "valueHash": _hash(value),
        },
        patch=patch,
        namespace="default",
        name="c",
        logger=logging.getLogger("test"),
    )
    assert [k for k, _ in calls["create"]] == ["C_1"]


# ---------- API error bodies ----------


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def test_api_detail_appends_the_response_body() -> None:
    from vastai_operator.vast_client import api_detail

    exc = Exception("400 Client Error: Bad Request for url: https://x/y")
    exc.response = _Resp('{"error": "too many secrets"}')  # type: ignore[attr-defined]
    out = api_detail(exc)
    assert "400 Client Error" in out
    assert "too many secrets" in out


def test_api_detail_without_a_response_is_unchanged() -> None:
    from vastai_operator.vast_client import api_detail

    assert api_detail(Exception("boom")) == "boom"


def test_api_detail_ignores_an_empty_body() -> None:
    from vastai_operator.vast_client import api_detail

    exc = Exception("boom")
    exc.response = _Resp("   ")  # type: ignore[attr-defined]
    assert api_detail(exc) == "boom"


def test_api_detail_truncates_a_huge_body() -> None:
    from vastai_operator.vast_client import api_detail

    exc = Exception("boom")
    exc.response = _Resp("x" * 5000)  # type: ignore[attr-defined]
    assert len(api_detail(exc)) < 600


def test_api_error_body_is_still_redacted() -> None:
    from vastai_operator.vast_client import VastAPIError, api_detail

    exc = Exception("400 for url: https://x/y?api_key=secret123")
    exc.response = _Resp("denied for api_key=secret123")  # type: ignore[attr-defined]
    err = VastAPIError(f"create_env_var failed: {api_detail(exc)}")
    assert "secret123" not in str(err)
    assert str(err).count("<redacted>") == 2
