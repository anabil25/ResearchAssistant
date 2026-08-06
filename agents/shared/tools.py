from __future__ import annotations

import json
import os
from typing import Annotated, Any

from agent_framework import tool
from agent_framework_foundry_hosting import FoundryToolbox  # type: ignore[import-untyped]
from azure.ai.projects import AIProjectClient
from pydantic import Field

from .connector_catalog import connector_definition
from .contracts import AgentManifest, Sensitivity, SpecialistCapability
from .credentials import get_credential
from .errors import ConfigurationError
from .invocation import RetryingResponsesInvoker
from .settings import HarnessSettings
from .workflows import CoordinatorRouter


def delegated_agent_name(capability: str, sensitivity: str) -> str | None:
    try:
        typed_capability = SpecialistCapability(capability)
        typed_sensitivity = Sensitivity(sensitivity)
    except ValueError:
        return None
    return CoordinatorRouter().target(typed_capability, typed_sensitivity)


def _invoke_specialist(client: Any, request: str, agent_name: str) -> str:
    return RetryingResponsesInvoker().invoke(client, request, agent_name).content


def _bound_tool_names(profile: AgentManifest) -> frozenset[str]:
    prefix = "foundry.toolbox."
    return frozenset(
        binding.operation_ref.id.removeprefix(prefix)
        for binding in profile.capability_bindings
        if binding.operation_ref.id.startswith(prefix)
    )


def tools_for_profile(
    profile: AgentManifest,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
) -> Any:
    if profile.id == "coordinator":
        return []
    toolbox_endpoint = (
        str(settings.toolbox_endpoint) if settings is not None and settings.toolbox_endpoint is not None else None
    )
    requires_toolbox = any(
        binding.operation_ref.id.startswith("foundry.toolbox.") for binding in profile.capability_bindings
    )
    if requires_toolbox and not toolbox_endpoint:
        raise ConfigurationError(
            "Manifest requires a configured Foundry Toolbox endpoint",
            context={"agent": profile.id},
        )
    if requires_toolbox:
        toolbox = FoundryToolbox(
            get_credential(settings.managed_identity_client_id if settings is not None else None),
            url=toolbox_endpoint,
            timeout=settings.default_timeout_seconds if settings is not None else 120,
        )
        toolbox.allowed_tools = _bound_tool_names(profile)
        return toolbox
    return []


def request_tool_names_for_profile(
    profile: AgentManifest,
    connector_ids: tuple[str, ...],
) -> frozenset[str]:
    if not profile.online:
        raise ConfigurationError(
            "Request-scoped Toolbox tools are only supported for online profiles",
            context={"agent": profile.id},
        )
    configured_sources = set(profile.connector_sources)
    unauthorized = set(connector_ids) - configured_sources
    if unauthorized:
        error_context: dict[str, Any] = {
            "agent": profile.id,
            "connectors": sorted(unauthorized),
        }
        raise ConfigurationError(
            "Request names connectors outside the profile Toolbox surface",
            context=error_context,
        )
    allowed_names = frozenset({
        "web_search",
        *{
            f"{connector_id}___{operation.mcp_tool_name}"
            for connector_id in connector_ids
            for operation in connector_definition(connector_id).operations
            if operation.operation_class != "delete"
        },
    })
    missing_bindings = allowed_names - _bound_tool_names(profile)
    if missing_bindings:
        missing_context: dict[str, Any] = {
            "agent": profile.id,
            "tools": sorted(missing_bindings),
        }
        raise ConfigurationError(
            "Profile Toolbox bindings are missing authorized tools",
            context=missing_context,
        )
    return allowed_names


def _agent_names() -> dict[str, str]:
    return {
        "literature": os.getenv("RESEARCH_LITERATURE_AGENT_NAME", "literature-agent"),
        "grant": os.getenv("RESEARCH_GRANT_AGENT_NAME", "grant-agent"),
        "matching": os.getenv("RESEARCH_MATCHING_AGENT_NAME", "matching-agent"),
        "dataset": os.getenv("RESEARCH_DATASET_AGENT_NAME", "dataset-agent"),
        "institutional_qa": os.getenv("RESEARCH_INSTITUTION_AGENT_NAME", "institution-agent"),
    }


def build_delegate_tool() -> Any:
    @tool(approval_mode="never_require")
    def delegate_to_specialist(
        capability: Annotated[
            str,
            Field(description=("One of literature, grant, matching, dataset, institutional_qa")),
        ],
        request: Annotated[
            str,
            Field(description="Complete user request and any verified context"),
        ],
        sensitivity: Annotated[
            str,
            Field(description="One of public, internal, confidential, restricted"),
        ] = "internal",
    ) -> str:
        """Delegate a bounded read-only request to the matching Hosted Agent."""
        if capability not in _agent_names():
            return json.dumps(
                {
                    "error": "unsupported_capability",
                    "allowed": sorted(_agent_names()),
                }
            )
        if sensitivity not in {"public", "internal", "confidential", "restricted"}:
            return json.dumps(
                {
                    "error": "invalid_sensitivity",
                    "allowed": ["public", "internal", "confidential", "restricted"],
                }
            )
        agent_name = delegated_agent_name(capability, sensitivity)
        if agent_name is None:
            return json.dumps(
                {
                    "error": "unsupported_route",
                    "capability": capability,
                    "sensitivity": sensitivity,
                }
            )
        endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        project = AIProjectClient(
            endpoint=endpoint,
            credential=get_credential(),
            allow_preview=True,
        )
        client = project.get_openai_client(agent_name=agent_name)
        return _invoke_specialist(client, request, agent_name)

    return delegate_to_specialist
