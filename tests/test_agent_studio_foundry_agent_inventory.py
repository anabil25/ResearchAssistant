# mypy: disable-error-code=import-untyped
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import research_assistant_api.agent_studio.foundry_agent_inventory as inventory_module
from research_assistant_api.agent_studio.foundry_agent_inventory import (
    AIProjectFoundryAgentInventory,
    FoundryAgentInventoryError,
    UnavailableFoundryAgentInventory,
    build_foundry_agent_inventory,
)
from research_assistant_api.config import Settings

if False:  # pragma: no cover
    from azure.core.credentials import TokenCredential


class FakeAgentsClient:
    def __init__(self, agents: list[Any]) -> None:
        self._agents = agents

    def list(self) -> list[Any]:
        return self._agents


class FakeProjectClient:
    def __init__(self, agents: list[Any]) -> None:
        self.agents = FakeAgentsClient(agents)


def test_inventory_maps_display_safe_agent_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = [
        SimpleNamespace(
            name="research-coordinator",
            kind="hosted",
            description="Routes research work.",
            model="gpt-5.4-mini",
            versions=SimpleNamespace(latest=SimpleNamespace(version="2", status="active")),
        ),
        SimpleNamespace(
            name="literature-helper",
            type="prompt",
            description=None,
            versions=SimpleNamespace(latest=SimpleNamespace(version="1", status="ready", model="gpt-5.6-sol")),
        ),
    ]

    def factory(*, endpoint: str, credential: Any, allow_preview: bool) -> FakeProjectClient:
        assert endpoint == "https://project.example.test"
        assert allow_preview is True
        return FakeProjectClient(agents)

    monkeypatch.setattr(inventory_module, "AIProjectClient", factory)
    inventory = AIProjectFoundryAgentInventory(
        "https://project.example.test", credential=cast("TokenCredential", object())
    )

    assert [item.name for item in inventory.list_agents()] == ["literature-helper", "research-coordinator"]
    prompt, hosted = inventory.list_agents()
    assert (prompt.agent_type, prompt.version, prompt.status, prompt.model) == ("prompt", "1", "ready", "gpt-5.6-sol")
    assert (hosted.agent_type, hosted.description, hosted.version, hosted.status) == (
        "hosted",
        "Routes research work.",
        "2",
        "active",
    )


def test_inventory_maps_the_real_foundry_agent_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: this payload is captured verbatim from ``agents.list()``
    against a live Foundry project.

    Every display field lives on the *version*, not the agent: ``kind`` is
    under ``versions.latest.definition`` and a hosted agent names its model
    only through the container's environment. Hand-written fixtures that hung
    those fields off the agent let the mapper report every real agent as
    "unknown" with no model while still passing.
    """
    agents = [
        {
            "object": "agent",
            "id": "grant-agent",
            "name": "grant-agent",
            "state": "enabled",
            "versions": {
                "latest": {
                    "object": "agent.version",
                    "id": "grant-agent:2",
                    "name": "grant-agent",
                    "version": "2",
                    "description": "Maps funding requirements and drafts grant sections.",
                    "status": "active",
                    "draft": False,
                    "definition": {
                        "kind": "hosted",
                        "cpu": "0.5",
                        "memory": "1Gi",
                        "environment_variables": {
                            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.6-sol",
                            "RESEARCH_WORKSPACE_PROJECT_ID": "researchassistant-nc-y7m4",
                        },
                    },
                }
            },
        }
    ]
    monkeypatch.setattr(inventory_module, "AIProjectClient", lambda **_: FakeProjectClient(agents))
    inventory = AIProjectFoundryAgentInventory(
        "https://project.example.test", credential=cast("TokenCredential", object())
    )

    (agent,) = inventory.list_agents()
    assert (agent.name, agent.agent_type, agent.version, agent.status, agent.model) == (
        "grant-agent",
        "hosted",
        "2",
        "active",
        "gpt-5.6-sol",
    )
    assert agent.description == "Maps funding requirements and drafts grant sections."


def test_inventory_skips_agents_without_names_and_labels_unknown_type(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = [
        SimpleNamespace(name=None, kind="hosted", versions=SimpleNamespace(latest=None)),
        SimpleNamespace(name="unknown-agent", versions=SimpleNamespace(latest=None)),
    ]
    monkeypatch.setattr(
        inventory_module,
        "AIProjectClient",
        lambda **_: FakeProjectClient(agents),
    )
    inventory = AIProjectFoundryAgentInventory("https://project.example.test", cast("TokenCredential", object()))

    assert [(item.name, item.agent_type, item.version, item.status) for item in inventory.list_agents()] == [
        ("unknown-agent", "unknown", None, None)
    ]


def test_inventory_wraps_foundry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class RaisingAgentsClient:
        def list(self) -> list[Any]:
            raise RuntimeError("forbidden")

    monkeypatch.setattr(
        inventory_module,
        "AIProjectClient",
        lambda **_: SimpleNamespace(agents=RaisingAgentsClient()),
    )
    inventory = AIProjectFoundryAgentInventory("https://project.example.test", cast("TokenCredential", object()))

    with pytest.raises(FoundryAgentInventoryError, match="failed"):
        inventory.list_agents()


def test_unavailable_inventory_raises() -> None:
    with pytest.raises(FoundryAgentInventoryError, match="unavailable"):
        UnavailableFoundryAgentInventory().list_agents()


def test_build_inventory_returns_unavailable_when_endpoint_is_not_configured() -> None:
    assert isinstance(build_foundry_agent_inventory(Settings()), UnavailableFoundryAgentInventory)


def test_build_inventory_returns_project_inventory_when_endpoint_is_configured() -> None:
    settings = Settings(foundry_project_endpoint="https://project.example.test")
    assert isinstance(build_foundry_agent_inventory(settings), AIProjectFoundryAgentInventory)


def test_build_inventory_uses_managed_identity_when_client_id_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        inventory_module,
        "ManagedIdentityCredential",
        lambda client_id: f"managed:{client_id}",
    )
    inventory = build_foundry_agent_inventory(
        Settings(
            foundry_project_endpoint="https://project.example.test",
            managed_identity_client_id="client-123",
        )
    )

    assert isinstance(inventory, AIProjectFoundryAgentInventory)
    assert cast(Any, inventory._credential) == "managed:client-123"