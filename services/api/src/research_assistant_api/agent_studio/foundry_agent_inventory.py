"""Read-only inventory of agents in the configured Foundry project.

This module intentionally normalizes only display-safe remote metadata. It
does not expose credentials, create resources, or infer a Studio publication
from an agent that happens to exist in Foundry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentKind
from azure.core.credentials import TokenCredential
from research_assistant_core.azure_auth import azure_credential

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


def _agent_type(definition: Any) -> FoundryAgentType:
    raw_kind = _value(_field(definition, "kind"))
    if raw_kind is None:
        return FoundryAgentType.UNKNOWN
    exact_types = {
        AgentKind.HOSTED.value: FoundryAgentType.HOSTED,
        AgentKind.PROMPT.value: FoundryAgentType.PROMPT,
        AgentKind.WORKFLOW.value: FoundryAgentType.WORKFLOW,
        AgentKind.EXTERNAL.value: FoundryAgentType.EXTERNAL,
    }
    return exact_types.get(raw_kind, FoundryAgentType.UNKNOWN)


def _model_deployments(definition: Any, live_deployment_names: frozenset[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    direct_model = _value(_field(definition, "model"))
    if direct_model in live_deployment_names:
        candidates.append(direct_model)

    if _value(_field(definition, "kind")) == AgentKind.HOSTED.value:
        environment = _field(definition, "environment_variables")
        if isinstance(environment, Mapping):
            for raw_value in environment.values():
                value = _value(raw_value)
                if value in live_deployment_names:
                    candidates.append(value)

    return tuple(sorted(set(candidates)))


class AIProjectFoundryAgentInventory:
    """Inventories agents through the configured ``AIProjectClient`` only."""

    def __init__(self, endpoint: str, credential: TokenCredential) -> None:
        self._endpoint = endpoint
        self._credential = credential

    def list_agents(self) -> tuple[FoundryAgentInventoryItem, ...]:
        try:
            with AIProjectClient(
                endpoint=self._endpoint,
                credential=self._credential,
                allow_preview=True,
            ) as client:
                agents = list(client.agents.list())
                deployments = list(client.deployments.list())
                live_deployment_names = frozenset(
                    name
                    for deployment in deployments
                    if (name := _value(_field(deployment, "name")))
                )

                inventory: list[FoundryAgentInventoryItem] = []
                for agent in agents:
                    name = _value(_field(agent, "name"))
                    if not name:
                        continue
                    latest = _field(_field(agent, "versions"), "latest")
                    version = _value(_field(latest, "version"))
                    if version and (
                        _field(latest, "definition") is None or _field(latest, "status") is None
                    ):
                        latest = client.agents.get_version(agent_name=name, agent_version=version)
                    definition = _field(latest, "definition")
                    model_deployments = _model_deployments(definition, live_deployment_names)
                    inventory.append(
                        FoundryAgentInventoryItem(
                            name=name,
                            agent_type=_agent_type(definition),
                            description=_value(
                                _field(latest, "description") or _field(agent, "description")
                            ),
                            version=_value(_field(latest, "version")),
                            status=_value(_field(latest, "status")),
                            model_deployments=model_deployments,
                            model=model_deployments[0] if len(model_deployments) == 1 else None,
                        )
                    )
        except Exception as exc:
            raise FoundryAgentInventoryError(
                f"Listing agents for Foundry project {self._endpoint} failed."
            ) from exc
        return tuple(sorted(inventory, key=lambda item: item.name))


class UnavailableFoundryAgentInventory:
    """Explicit unavailable path when no Foundry project is configured."""

    def list_agents(self) -> tuple[FoundryAgentInventoryItem, ...]:
        raise FoundryAgentInventoryError(
            "No Foundry project endpoint is configured; agent inventory is unavailable."
        )


def build_foundry_agent_inventory(settings: Settings) -> FoundryAgentInventory:
    """Build the configured project inventory or an explicit unavailable port."""
    if not settings.foundry_project_endpoint:
        return UnavailableFoundryAgentInventory()
    return AIProjectFoundryAgentInventory(
        settings.foundry_project_endpoint,
        azure_credential(settings.managed_identity_client_id),
    )