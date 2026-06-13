"""Shared pytest fixtures.

The ``vastai`` SDK is stubbed at *module import time* (not in a fixture) so the
stub is in place before ``vastai_operator.vast_client`` is first imported by a
test module's top-level ``from`` statement.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


class StubVastAI:
    """Test double for ``vastai.VastAI`` — records calls, returns canned data."""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self.search_offers_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.show_calls: list[dict[str, Any]] = []
        self.destroy_calls: list[dict[str, Any]] = []
        self.search_response: Any = []
        self.create_response: Any = {"new_contract": 42}
        self.show_response: Any = {"id": 42, "actual_status": "running"}

    def search_offers(self, **kwargs: Any) -> Any:
        self.search_offers_calls.append(kwargs)
        return self.search_response

    def create_instance(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self.create_response

    def show_instance(self, **kwargs: Any) -> Any:
        self.show_calls.append(kwargs)
        return self.show_response

    def destroy_instance(self, **kwargs: Any) -> Any:
        self.destroy_calls.append(kwargs)
        return {"success": True}

    def create_template(self, **kwargs: Any) -> Any:
        self.last_create_template_kwargs = kwargs
        return {"hash_id": "stub-hash", "id": 1}

    def update_template(self, **kwargs: Any) -> Any:
        self.last_update_template_kwargs = kwargs
        return {"hash_id": kwargs.get("hash_id", "stub-hash")}

    def delete_template(self, **kwargs: Any) -> Any:
        self.last_delete_template_kwargs = kwargs
        return {"success": True}


_stub_module = types.ModuleType("vastai")
_stub_module.VastAI = StubVastAI  # type: ignore[attr-defined]
sys.modules["vastai"] = _stub_module
sys.modules.pop("vastai_operator.vast_client", None)


@pytest.fixture
def stub_sdk() -> StubVastAI:
    """Return the StubVastAI class so tests can mutate canned responses if needed."""
    return StubVastAI()
