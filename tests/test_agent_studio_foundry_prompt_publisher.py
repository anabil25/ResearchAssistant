# mypy: disable-error-code=import-untyped
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import research_assistant_api.agent_studio.foundry_prompt_publisher as publisher_module
from research_assistant_api.agent_studio.foundry_prompt_publisher import (
    AIProjectPromptAgentPublisher,
    PromptAgentPublicationError,
    UnavailablePromptAgentPublisher,
    build_prompt_agent_publisher,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentVersion,
    ModelDeploymentRef,
    RuntimeTarget,
)
from research_assistant_api.config import Settings

if False:  # pragma: no cover
    from azure.core.credentials import TokenCredential


def _version(*, runtime_target: RuntimeTarget = RuntimeTarget.MANAGED_FOUNDRY) -> AgentVersion:
    manifest = AgentManifest(
        logical_agent_id="agent-prompt-publisher",
        tenant_id="demo",
        project_id="default",
        display_name="Prompt Publisher",
        description="Summarizes governed research.",
        instructions="Use approved sources only.",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        model_deployment=ModelDeploymentRef(
            deployment_name="gpt-5.4-mini",
            model_name="gpt-5.4-mini",
            model_format="OpenAI",
        ),
    )
    return AgentVersion(
        id="version-123",
        logical_agent_id=manifest.logical_agent_id,
        tenant_id=manifest.tenant_id,
        project_id=manifest.project_id,
        sequence=1,
        manifest=manifest,
        manifest_hash="sha256:manifest",
        created_by="user-1",
        runtime_target=runtime_target,
    )


class FakeAgentsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_version(self, agent_name: str, **kwargs: Any) -> Any:
        self.calls.append({"agent_name": agent_name, **kwargs})
        return SimpleNamespace(name=agent_name, version="5", status="active")


class FakeProjectClient:
    def __init__(self) -> None:
        self.agents = FakeAgentsClient()


def test_publisher_creates_foundry_prompt_agent_version(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeProjectClient()

    def factory(*, endpoint: str, credential: Any, allow_preview: bool) -> FakeProjectClient:
        assert endpoint == "https://project.example.test"
        assert allow_preview is True
        return client

    monkeypatch.setattr(publisher_module, "AIProjectClient", factory)
    publication = AIProjectPromptAgentPublisher(
        "https://project.example.test", cast("TokenCredential", object())
    ).publish(_version())

    assert publication.remote_agent_name == "agent-prompt-publisher"
    assert publication.remote_version == "5"
    assert publication.remote_status == "active"
    assert publication.studio_version_id == "version-123"
    assert client.agents.calls[0]["metadata"] == {
        "agent_studio_version_id": "version-123",
        "agent_studio_manifest_hash": "sha256:manifest",
    }
    definition = client.agents.calls[0]["definition"]
    assert definition.model == "gpt-5.4-mini"
    assert definition.instructions == "Use approved sources only."


def test_publisher_rejects_non_managed_foundry_version() -> None:
    publisher = AIProjectPromptAgentPublisher(
        "https://project.example.test", cast("TokenCredential", object())
    )
    with pytest.raises(PromptAgentPublicationError, match="custom_hosted"):
        publisher.publish(_version(runtime_target=RuntimeTarget.CUSTOM_HOSTED))


def test_publisher_wraps_foundry_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class RaisingAgentsClient:
        def create_version(self, agent_name: str, **kwargs: Any) -> Any:
            del agent_name, kwargs
            raise RuntimeError("forbidden")

    monkeypatch.setattr(
        publisher_module,
        "AIProjectClient",
        lambda **_: SimpleNamespace(agents=RaisingAgentsClient()),
    )
    publisher = AIProjectPromptAgentPublisher(
        "https://project.example.test", cast("TokenCredential", object())
    )
    with pytest.raises(PromptAgentPublicationError, match="Publishing prompt agent"):
        publisher.publish(_version())


def test_publisher_rejects_missing_remote_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        publisher_module,
        "AIProjectClient",
        lambda **_: SimpleNamespace(
            agents=SimpleNamespace(create_version=lambda *args, **kwargs: SimpleNamespace(name="agent"))
        ),
    )
    publisher = AIProjectPromptAgentPublisher(
        "https://project.example.test", cast("TokenCredential", object())
    )
    with pytest.raises(PromptAgentPublicationError, match="did not return a version"):
        publisher.publish(_version())


def test_build_publisher_returns_unavailable_without_foundry_endpoint() -> None:
    assert isinstance(build_prompt_agent_publisher(Settings()), UnavailablePromptAgentPublisher)


def test_build_publisher_uses_managed_identity_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        publisher_module,
        "ManagedIdentityCredential",
        lambda client_id: f"managed:{client_id}",
    )
    publisher = build_prompt_agent_publisher(
        Settings(
            foundry_project_endpoint="https://project.example.test",
            managed_identity_client_id="client-123",
        )
    )

    assert isinstance(publisher, AIProjectPromptAgentPublisher)
    assert cast(Any, publisher._credential) == "managed:client-123"