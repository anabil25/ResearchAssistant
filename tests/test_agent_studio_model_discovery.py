# mypy: disable-error-code=import-untyped
from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.model_discovery as model_discovery
from research_assistant_api.agent_studio.model_discovery import (
    AIProjectModelDiscovery,
    InMemoryModelDiscovery,
    ModelDiscoveryError,
    UnavailableModelDiscovery,
    build_model_discovery,
)
from research_assistant_api.agent_studio.models import ModelDeploymentRef
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


def test_in_memory_model_discovery_returns_fixed_list() -> None:
    models = (ModelDeploymentRef(deployment_name="gpt-4o", model_name="gpt-4o", model_format="oai"),)
    discovery = InMemoryModelDiscovery(models)
    assert discovery.list_deployed_models() == models


def test_in_memory_model_discovery_defaults_to_empty() -> None:
    assert InMemoryModelDiscovery().list_deployed_models() == ()


def test_unavailable_model_discovery_raises() -> None:
    with pytest.raises(ModelDiscoveryError, match="unavailable"):
        UnavailableModelDiscovery().list_deployed_models()


class FakeDeploymentsClient:
    def __init__(self, deployments: list[Any]) -> None:
        self._deployments = deployments

    def list(self) -> list[Any]:
        return self._deployments


class FakeAIProjectClient:
    def __init__(self, deployments: list[Any]) -> None:
        self.deployments = FakeDeploymentsClient(deployments)

    def __call__(self, *, endpoint: str, credential: Any, allow_preview: bool) -> FakeAIProjectClient:
        return self


def test_ai_project_model_discovery_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    deployments = [
        SimpleNamespace(name="gpt-4o-deployment", model_name="gpt-4o", model_format="oai", capacity=10),
        SimpleNamespace(name="embed-deployment", model_name=None, type="embedding", capacity="not-an-int"),
    ]

    def _factory(*, endpoint: str, credential: Any, allow_preview: bool) -> FakeAIProjectClient:
        return FakeAIProjectClient(deployments)

    monkeypatch.setattr(model_discovery, "AIProjectClient", _factory)
    discovery = AIProjectModelDiscovery(
        "https://project.example.test", credential=cast("TokenCredential", object())
    )
    results = discovery.list_deployed_models()
    assert len(results) == 2
    first = results[0]
    assert first.deployment_name == "gpt-4o-deployment"
    assert first.model_name == "gpt-4o"
    assert first.capacity == 10
    second = results[1]
    # No model_name -> falls back to deployment name; non-int capacity -> None.
    assert second.model_name == "embed-deployment"
    assert second.model_format == "embedding"
    assert second.capacity is None


def test_ai_project_model_discovery_skips_deployments_missing_name_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployments = [SimpleNamespace(name=None, model_name=None)]

    def _factory(*, endpoint: str, credential: Any, allow_preview: bool) -> FakeAIProjectClient:
        return FakeAIProjectClient(deployments)

    monkeypatch.setattr(model_discovery, "AIProjectClient", _factory)
    discovery = AIProjectModelDiscovery(
        "https://project.example.test", credential=cast("TokenCredential", object())
    )
    assert discovery.list_deployed_models() == ()


def test_ai_project_model_discovery_wraps_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingDeploymentsClient:
        def list(self) -> list[Any]:
            raise RuntimeError("boom")

    class _RaisingClient:
        def __init__(self) -> None:
            self.deployments = _RaisingDeploymentsClient()

    def _factory(*, endpoint: str, credential: Any, allow_preview: bool) -> _RaisingClient:
        return _RaisingClient()

    monkeypatch.setattr(model_discovery, "AIProjectClient", _factory)
    discovery = AIProjectModelDiscovery(
        "https://project.example.test", credential=cast("TokenCredential", object())
    )
    with pytest.raises(ModelDiscoveryError, match="failed"):
        discovery.list_deployed_models()


def test_build_model_discovery_returns_unavailable_when_not_configured() -> None:
    settings = Settings(foundry_project_endpoint=None)
    discovery = build_model_discovery(settings)
    assert isinstance(discovery, UnavailableModelDiscovery)


def test_build_model_discovery_returns_ai_project_discovery_when_configured() -> None:
    settings = Settings(foundry_project_endpoint="https://project.example.test")
    discovery = build_model_discovery(settings)
    assert isinstance(discovery, AIProjectModelDiscovery)


def test_build_model_discovery_uses_managed_identity_when_client_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[Any] = []

    def _credential(client_id: Any = None) -> str:
        requested.append(client_id)
        return f"managed:{client_id}"

    monkeypatch.setattr(model_discovery, "azure_credential", _credential)
    settings = Settings(
        foundry_project_endpoint="https://project.example.test",
        managed_identity_client_id="client-123",
    )
    discovery = build_model_discovery(settings)
    assert isinstance(discovery, AIProjectModelDiscovery)
    assert cast(Any, discovery._credential) == "managed:client-123"
    assert requested == ["client-123"]
