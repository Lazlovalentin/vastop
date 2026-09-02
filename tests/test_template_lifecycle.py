"""VastTemplate handler lifecycle + drift-recreate end-to-end (mocked SDK)."""

from __future__ import annotations

import logging
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAST_API_KEY", "test-key")


class _Patch:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}


# ---------- VastClient.create_template / update_template field plumbing ----------


async def test_create_template_passes_login_ports_env() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.create_template(
        name="ubuntu-ssh",
        image="ubuntu:22.04",
        runtype="ssh",
        disk_gb=16,
        env={"FOO": "bar", "BAR": "baz"},
        ports=[8080, 8888],
        onstart="echo hi",
        description="desc",
        private=True,
        image_login="myregistry.com user pass",
    )
    kwargs = client._sdk.last_create_template_kwargs  # type: ignore[attr-defined]
    assert kwargs["image"] == "ubuntu:22.04"
    env_str = kwargs["env"]
    assert "-e FOO=bar" in env_str
    assert "-e BAR=baz" in env_str
    assert "-p 8080:8080" in env_str
    assert "-p 8888:8888" in env_str
    assert kwargs["login"] == "myregistry.com user pass"
    assert kwargs["ssh"] is True
    assert kwargs["public"] is False  # private=True → public=False


async def test_create_template_full_login_posts_credentials_directly() -> None:
    """SDK drops registry user/pass; full 3-part imageLogin must POST /template/
    itself with docker_login_user / docker_login_pass populated."""
    from vastai_operator.vast_client import VastClient

    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"success": True, "template": {"hash_id": "h1", "id": 7}}

    class _HttpClient:
        def post(self, path: str, json_data: dict[str, Any]) -> _Resp:
            captured["path"] = path
            captured["payload"] = json_data
            return _Resp()

    client = VastClient(api_key="test")
    client._sdk.client = _HttpClient()  # type: ignore[attr-defined]
    raw = await client.create_template(
        name="worker",
        image="registry.digitalocean.com/x/worker:v1",
        runtype="args",
        disk_gb=100,
        env={"FOO": "bar"},
        ports=[8000],
        onstart="run.sh",
        description="d",
        private=True,
        image_login="registry.digitalocean.com user@x.com dop_v1_token",
    )
    assert captured["path"] == "/template/"
    p = captured["payload"]
    assert p["docker_login_repo"] == "registry.digitalocean.com"
    assert p["docker_login_user"] == "user@x.com"
    assert p["docker_login_pass"] == "dop_v1_token"
    assert p["runtype"] == "args"
    assert p["use_ssh"] is False
    assert p["private"] is True
    assert p["recommended_disk_space"] == 100.0
    assert "-e FOO=bar" in p["env"] and "-p 8000:8000" in p["env"]
    assert raw["template"]["hash_id"] == "h1"
    # SDK path must not have been used.
    assert not hasattr(client._sdk, "last_create_template_kwargs")


async def test_create_template_repo_only_login_uses_sdk_path() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.create_template(
        name="w",
        image="img",
        runtype="ssh",
        disk_gb=16,
        env=None,
        ports=None,
        onstart=None,
        description=None,
        private=True,
        image_login="myregistry.com",
    )
    kwargs = client._sdk.last_create_template_kwargs  # type: ignore[attr-defined]
    assert kwargs["login"] == "myregistry.com"


async def test_update_template_emits_flat_flags() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.update_template(
        hash_id="abc123",
        name="ubuntu-ssh",
        image="ubuntu:24.04",
        runtype="ssh",
        disk_gb=20,
        env={"BAR": "qux"},
        ports=[],
        onstart=None,
        description="updated",
        private=True,
        image_login=None,
    )
    kwargs = client._sdk.last_update_template_kwargs  # type: ignore[attr-defined]
    assert kwargs["hash_id"] == "abc123"
    assert kwargs["image"] == "ubuntu:24.04"
    assert kwargs["runtype"] == "ssh"
    assert kwargs["use_ssh"] is True
    assert kwargs["use_jupyter_lab"] is False
    assert "docker_login_repo" not in kwargs  # not set


async def test_update_template_jupyter_sets_use_jupyter_lab() -> None:
    from vastai_operator.vast_client import VastClient

    client = VastClient(api_key="test")
    await client.update_template(
        hash_id="h",
        name="t",
        image="img",
        runtype="jupyter",
        disk_gb=8,
        env=None,
        ports=None,
        onstart=None,
        description=None,
        private=True,
        image_login=None,
    )
    kwargs = client._sdk.last_update_template_kwargs  # type: ignore[attr-defined]
    assert kwargs["runtype"] == "jupyter"
    assert kwargs["use_ssh"] is True  # jupyter runtype keeps ssh on
    assert kwargs["use_jupyter_lab"] is True


# ---------- Template handler create / update / delete ----------


async def test_template_on_create_stores_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import template
    from vastai_operator.vast_client import VastClient

    class _FakeClient(VastClient):
        def __init__(self) -> None:
            pass

        async def create_template(self, **_: Any) -> dict[str, Any]:
            return {"hash_id": "abc123", "id": 4242}

    monkeypatch.setattr(template, "_client_for", lambda spec, ns: _FakeClient())

    patch_obj = _Patch()
    await template.on_create(
        spec={"image": "ubuntu:22.04", "runtype": "ssh"},
        meta={"generation": 1},
        patch=patch_obj,
        namespace="default",
        name="my-tmpl",
        logger=logging.getLogger("test"),
    )
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["vastTemplateHash"] == "abc123"
    assert patch_obj.status["vastTemplateId"] == 4242
    assert patch_obj.status["syncedGeneration"] == 1


async def test_template_on_update_replaces_via_create_then_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_update strategy: create new template (new hash), then delete the old one.

    Reason: Vast.ai's PUT /template rejects updates from non-creator user ids and
    the SDK doesn't populate creator_id. Create+delete avoids the broken path.
    """
    from vastai_operator.handlers import template
    from vastai_operator.vast_client import VastClient

    creates: list[dict[str, Any]] = []
    deletes: list[dict[str, Any]] = []

    class _FakeClient(VastClient):
        def __init__(self) -> None:
            pass

        async def create_template(self, **kwargs: Any) -> dict[str, Any]:
            creates.append(kwargs)
            return {"hash_id": "new-hash", "id": 9999}

        async def delete_template(self, **kwargs: Any) -> None:
            deletes.append(kwargs)

    monkeypatch.setattr(template, "_client_for", lambda spec, ns: _FakeClient())

    patch_obj = _Patch()
    await template.on_update(
        spec={"image": "ubuntu:24.04", "runtype": "ssh"},
        status={"vastTemplateHash": "old-hash", "vastTemplateId": 4242, "phase": "Ready"},
        meta={"generation": 2},
        patch=patch_obj,
        namespace="default",
        name="my-tmpl",
        logger=logging.getLogger("test"),
    )
    assert len(creates) == 1
    assert creates[0]["image"] == "ubuntu:24.04"
    assert len(deletes) == 1
    assert deletes[0]["hash_id"] == "old-hash"
    assert deletes[0]["template_id"] == 4242
    assert patch_obj.status["phase"] == "Ready"
    assert patch_obj.status["vastTemplateHash"] == "new-hash"
    assert patch_obj.status["vastTemplateId"] == 9999
    assert patch_obj.status["syncedGeneration"] == 2


async def test_template_on_delete_uses_template_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from vastai_operator.handlers import template
    from vastai_operator.vast_client import VastClient

    deleted: list[dict[str, Any]] = []

    class _FakeClient(VastClient):
        def __init__(self) -> None:
            pass

        async def delete_template(self, **kwargs: Any) -> None:
            deleted.append(kwargs)

    monkeypatch.setattr(template, "_client_for", lambda spec, ns: _FakeClient())

    await template.on_delete(
        spec={"image": "x"},
        status={"vastTemplateHash": "abc123", "vastTemplateId": 4242},
        namespace="default",
        logger=logging.getLogger("test"),
    )
    assert deleted[0]["hash_id"] == "abc123"
    assert deleted[0]["template_id"] == 4242


# ---------- Ubuntu image-tag drift recreates Instance ----------


async def test_image_tag_change_drives_instance_recreate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full simulated flow:
       1) Template registered with ubuntu:22.04 → hash=h1, generation=1
       2) Instance launched, resolvedTemplate=t@1, instance_id=I1
       3) Template patched to ubuntu:24.04 → hash unchanged but generation=2
       4) Next Instance sync_status tick: drift detected → destroy + relaunch
    """
    from vastai_operator.handlers import instance
    from vastai_operator.launcher import LaunchResult
    from vastai_operator.resolver import OrderPick, TemplateSpec

    # State as it evolves through the test
    template_state = {
        "generation": 2,  # Template already updated by the time sync_status runs
        "image": "ubuntu:24.04",
        "hash": "h1",
    }

    def _fetch_template(ns: str, name: str) -> TemplateSpec:
        return TemplateSpec(
            name=name,
            generation=template_state["generation"],
            image=template_state["image"],
            disk_gb=16,
            env={},
            onstart=None,
            ssh_key=None,
            runtype="ssh",
            template_hash_id=template_state["hash"],
            template_phase="Ready",
        )

    monkeypatch.setattr(instance, "fetch_template", _fetch_template)

    destroyed: list[int] = []
    new_launches: list[dict[str, Any]] = []

    class _FakeClient:
        async def destroy_instance(self, instance_id: int) -> None:
            destroyed.append(instance_id)

        async def get_instance(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("must not poll Vast.ai when drift triggers")

    monkeypatch.setattr(instance, "_client_for", lambda spec, ns: _FakeClient())

    async def _fake_launch(spec: dict[str, Any], namespace: str) -> LaunchResult:
        new_launches.append({"spec": spec, "ns": namespace})
        return LaunchResult(
            instance_id=2222,
            offer_id=99,
            template=TemplateSpec(
                name="ubuntu-ssh",
                generation=template_state["generation"],
                image=template_state["image"],
                disk_gb=16,
                env={},
                onstart=None,
                ssh_key=None,
                runtype="ssh",
                template_hash_id=template_state["hash"],
                template_phase="Ready",
            ),
            order=OrderPick(name="o", generation=1, offer_id=99, price_per_hour=0.05),
        )

    monkeypatch.setattr(instance, "launch_instance", _fake_launch)

    patch_obj = _Patch()
    await instance.sync_status(
        spec={
            "templateRef": {"name": "ubuntu-ssh"},
            "orderRef": {"name": "o"},
            "recreateOnTemplateUpdate": True,
        },
        status={"instanceId": 1111, "resolvedTemplate": "ubuntu-ssh@1"},  # was 22.04
        patch=patch_obj,
        namespace="default",
        logger=logging.getLogger("test"),
    )

    # The old Vast.ai instance was destroyed and a new one launched.
    assert destroyed == [1111]
    assert len(new_launches) == 1
    assert patch_obj.status["instanceId"] == 2222
    assert patch_obj.status["phase"] == "Creating"
    # New marker tracks the updated Template generation.
    assert patch_obj.status["resolvedTemplate"].endswith("@2")


async def test_image_tag_change_respects_recreate_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When recreateOnTemplateUpdate=false, drift is NOT detected."""
    from vastai_operator.handlers import instance

    def _fail(*a: Any, **k: Any) -> Any:
        raise AssertionError("fetch_template must not be called when flag is off")

    monkeypatch.setattr(instance, "fetch_template", _fail)

    drifted = instance._check_template_drift(
        spec={"templateRef": {"name": "t"}, "recreateOnTemplateUpdate": False},
        status={"resolvedTemplate": "t@1"},
        namespace="default",
    )
    assert drifted is False


# ---------- imageLoginSecretRef ----------


def test_image_login_inline_is_used_as_is() -> None:
    from vastai_operator.handlers import template

    spec = {"image": "x", "imageLogin": "reg user pass"}
    assert template._resolve_image_login(spec, "default") == "reg user pass"


def test_image_login_absent_is_none() -> None:
    from vastai_operator.handlers import template

    assert template._resolve_image_login({"image": "x"}, "default") is None


def test_image_login_defaults_to_the_pull_secret(monkeypatch) -> None:
    from vastai_operator.handlers import template

    seen: dict[str, str | None] = {}

    def _read(namespace: str, name: str, registry: str | None = None) -> str:
        seen.update(namespace=namespace, name=name, registry=registry)
        return "reg user from-secret"

    monkeypatch.setattr(template, "read_docker_login", _read)
    spec = {
        "image": "registry.digitalocean.com/adva-soft-us/qwen-worker:v1",
        "imageLoginSecretRef": {"name": "adva-soft-us"},
    }
    assert template._resolve_image_login(spec, "production") == "reg user from-secret"
    assert seen == {
        "namespace": "production",
        "name": "adva-soft-us",
        "registry": "registry.digitalocean.com",
    }


def test_image_login_secret_ref_honours_an_explicit_key(monkeypatch) -> None:
    from vastai_operator.handlers import template

    monkeypatch.setattr(
        template, "read_secret_value", lambda ns, n, k: f"{n}/{k}"
    )
    spec = {
        "image": "x",
        "imageLoginSecretRef": {"name": "creds", "key": "docker"},
    }
    assert template._resolve_image_login(spec, "default") == "creds/docker"


def test_image_login_secret_ref_wins_over_inline(monkeypatch) -> None:
    from vastai_operator.handlers import template

    monkeypatch.setattr(
        template, "read_docker_login", lambda ns, n, r=None: "from-secret"
    )
    spec = {
        "image": "x",
        "imageLogin": "inline value",
        "imageLoginSecretRef": {"name": "creds"},
    }
    assert template._resolve_image_login(spec, "default") == "from-secret"


def test_kwargs_from_spec_carries_the_resolved_login(monkeypatch) -> None:
    from vastai_operator.handlers import template

    monkeypatch.setattr(
        template, "read_docker_login", lambda ns, n, r=None: "resolved"
    )
    kwargs = template._kwargs_from_spec(
        "t2e-worker",
        {"image": "img", "imageLoginSecretRef": {"name": "creds"}},
        "production",
    )
    assert kwargs["image_login"] == "resolved"
    assert kwargs["name"] == "t2e-worker"


def test_registry_of_reads_the_host_from_an_image() -> None:
    from vastai_operator.handlers.template import _registry_of

    assert _registry_of("registry.digitalocean.com/x/y:1") == "registry.digitalocean.com"
    assert _registry_of("localhost:5000/x") == "localhost:5000"
    assert _registry_of("ubuntu:24.04") is None
    assert _registry_of("library/ubuntu") is None
    assert _registry_of(None) is None


def test_docker_login_from_a_single_registry_entry() -> None:
    import base64
    import json

    from vastai_operator.resolver import docker_login_from_config

    cfg = json.dumps(
        {
            "auths": {
                "registry.example.com": {
                    "auth": base64.b64encode(b"bob:hunter2").decode()
                }
            }
        }
    )
    assert (
        docker_login_from_config(cfg, None)
        == "registry.example.com bob hunter2"
    )


def test_docker_login_picks_the_registry_serving_the_image() -> None:
    import base64
    import json

    from vastai_operator.resolver import docker_login_from_config

    cfg = json.dumps(
        {
            "auths": {
                "a.example.com": {"auth": base64.b64encode(b"a:1").decode()},
                "b.example.com": {"auth": base64.b64encode(b"b:2").decode()},
            }
        }
    )
    assert docker_login_from_config(cfg, "b.example.com") == "b.example.com b 2"


def test_docker_login_is_ambiguous_without_a_matching_registry() -> None:
    import base64
    import json

    import kopf
    import pytest as _pytest

    from vastai_operator.resolver import docker_login_from_config

    cfg = json.dumps(
        {
            "auths": {
                "a.example.com": {"auth": base64.b64encode(b"a:1").decode()},
                "b.example.com": {"auth": base64.b64encode(b"b:2").decode()},
            }
        }
    )
    with _pytest.raises(kopf.PermanentError):
        docker_login_from_config(cfg, "c.example.com")


def test_docker_login_accepts_explicit_username_and_password() -> None:
    import json

    from vastai_operator.resolver import docker_login_from_config

    cfg = json.dumps(
        {"auths": {"r.example.com": {"username": "u", "password": "p"}}}
    )
    assert docker_login_from_config(cfg, None) == "r.example.com u p"


def test_docker_login_rejects_an_empty_config() -> None:
    import kopf
    import pytest as _pytest

    from vastai_operator.resolver import docker_login_from_config

    with _pytest.raises(kopf.PermanentError):
        docker_login_from_config('{"auths": {}}', None)


def test_docker_login_rejects_malformed_json() -> None:
    import kopf
    import pytest as _pytest

    from vastai_operator.resolver import docker_login_from_config

    with _pytest.raises(kopf.PermanentError):
        docker_login_from_config("not json", None)
