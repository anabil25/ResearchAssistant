"""Read-only inventory of agents in the configured Foundry project.

This module intentionally normalizes only display-safe remote metadata. It
does not expose credentials, create resources, or infer a Studio publication
from an agent that happens to exist in Foundry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from research_assistant_api.agent_studio.models import FoundryAgentInventoryItem, FoundryAgentType
from research_assistant_api.config import Settings


class FoundryAgentInventoryError(RuntimeError):
    """Raised when the configured Foundry project cannot be inventoried."""


class FoundryAgentInventory(Protocol):
    """Lists display-safe metadata for agents in one Foundry project."""

    def list_agents(self) -> tuple[FoundryAgentInventoryItem, ...]: ...


def _value(value: Any) -> str | None:
    if value is None:
        return None
    raw_value = getattr(value, "value", value)
    return str(raw_value)


def _field(source: Any, name: str) -> Any:
    """Read a field from an SDK model that behaves as both object and mapping."""
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _agent_type(definition: Any, agent: Any) -> FoundryAgentType:
    raw_type = _value(_field(definition, "kind") or _field(agent, "kind") or _field(agent, "type"))
    if raw_type is None:
        return FoundryAgentType.UNKNOWN
    normalized = raw_type.lower().replace("_", "-")
    if "hosted" in normalized:
        return FoundryAgentType.HOSTED
    if "prompt" in normalized:
        return FoundryAgentType.PROMPT
    return FoundryAgentType.UNKNOWN


def _model(definition: Any, latest: Any, agent: Any) -> str | None:
    # A hosted agent names its deployment through the container environment;
    # only a prompt agent carries the model on the definition itself.
    environment = _field(definition, "environment_variables")
    return _value(
        _field(definition, "model")
        or _field(environment, "AZURE_AI_MODEL_DEPLOYMENT_NAME")
        or _field(latest, "model")
        or _field(agent, "model")
    )


class AIProjectFoundryAgentInventory:
    """Inventories agents through the configured ``AIProjectClient`` only."""

    def __init__(self, endpoint: str, credential: TokenCredential) -> None:
        self._endpoint = endpoint
        self._credential = credential

    def list_agents(self) -> tuple[FoundryAgentInventoryItem, ...]:
        client = AIProjectClient(endpoint=self._endpoint, credential=self._credential, allow_preview=True)
        try:
            agents = client.agents.list()
        except Exception as exc:
            raise FoundryAgentInventoryError(
                f"Listing agents for Foundry project {self._endpoint} failed."
            ) from exc

        inventory: list[FoundryAgentInventoryItem] = []
        for agent in agents:
            name = _value(_field(agent, "name"))
            if not name:
                continue
            latest = _field(_field(agent, "versions"), "latest")
            definition = _field(latest, "definition")
            inventory.append(
                FoundryAgentInventoryItem(
                    name=name,
                    agent_type=_agent_type(definition, agent),
                    description=_value(_field(latest, "description") or _field(agent, "description")),
                    version=_value(_field(latest, "version")),
                    status=_value(_field(latest, "status")),
                    model=_model(definition, latest, agent),
                )
            )
        return tuple(sorted(inventory, key=lambda item: item.name))


class UnavailableFoundryAgentInventory:
    """Explicit unavailable path when no Foundry project is configured."""

    def list_agents(self) -> tuple[FoundryAgentInventoryItem, ...]:
        raise FoundryAgentInventoryError(
            "No Foundry project endpoint is configured; agent inventory is unavailable."
        )


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_foundry_agent_inventory(settings: Settings) -> FoundryAgentInventory:
    """Build the configured project inventory or an explicit unavailable port."""
    if not settings.foundry_project_endpoint:
        return UnavailableFoundryAgentInventory()
    return AIProjectFoundryAgentInventory(
        settings.foundry_project_endpoint,
        _credential(settings.managed_identity_client_id),
    )