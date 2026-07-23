from __future__ import annotations

from typing import Any

from agent_framework_foundry_hosting import FoundryToolbox  # type: ignore[import-untyped]

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


def tools_for_profile(
    profile: AgentManifest,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
) -> Any:
    if profile.id == "coordinator":
        return []
    toolbox_endpoint = (
        str(settings.toolbox_endpoint)
        if settings is not None and settings.toolbox_endpoint is not None
        else None
    )
    requires_toolbox = any(
        binding.operation_id.startswith("foundry.toolbox.")
        for binding in profile.capability_bindings
    )
    if requires_toolbox and not toolbox_endpoint:
        raise ConfigurationError(
            "Manifest requires a configured Foundry Toolbox endpoint",
            context={"agent": profile.id},
        )
    if requires_toolbox:
        return FoundryToolbox(
            get_credential(settings.managed_identity_client_id if settings is not None else None),
            url=toolbox_endpoint,
            timeout=settings.default_timeout_seconds if settings is not None else 120,
        )
    return []
