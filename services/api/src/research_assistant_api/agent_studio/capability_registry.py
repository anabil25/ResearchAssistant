"""Capability catalog and attach-time maturity enforcement.

The seed catalog below reflects the built-in Foundry Agent Service tool
catalog's *documented* maturity as of this writing (Microsoft Learn
"Agent tools overview for Foundry Agent Service", `tool-catalog` page):
Web Search, Code Interpreter, File Search, Azure AI Search, Azure Functions,
and Function Calling are GA; Custom Code Interpreter, Image Generation,
Browser Automation, Computer Use, Microsoft Fabric, SharePoint, Memory, and
the Toolbox/A2A connector paths are explicitly documented as "(preview)".
Custom Hosted code is modeled as a non-Foundry-native capability so it is
never eligible for Managed Foundry runtime selection.

This module intentionally hard-codes the maturity for the built-in catalog
(no network call at import time), but callers may extend/override the
registry (e.g. from a live discovery source) via ``CapabilityRegistry``.
"""

from __future__ import annotations

from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    OperationMaturity,
)


class CapabilityAttachmentError(ValueError):
    """Raised when an attempted capability attachment is not GA-eligible."""


def _ga(name: str) -> CapabilityOperation:
    return CapabilityOperation(name=name, maturity=OperationMaturity.GA)


def _preview(name: str, reason: str) -> CapabilityOperation:
    return CapabilityOperation(name=name, maturity=OperationMaturity.PREVIEW, reason=reason)


def _unavailable(name: str, reason: str) -> CapabilityOperation:
    return CapabilityOperation(name=name, maturity=OperationMaturity.UNAVAILABLE, reason=reason)


_PREVIEW_REASON = "Documented as preview in the Foundry Agent Service tool catalog."


def _seed_descriptors() -> tuple[CapabilityDescriptor, ...]:
    return (
        CapabilityDescriptor(
            id="foundry.web_search",
            provider="microsoft_foundry",
            title="Web Search",
            description="Retrieve real-time public web information with inline citations.",
            operations=(_ga("search"),),
            risk_tier="medium",
            data_boundary="public",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.code_interpreter",
            provider="microsoft_foundry",
            title="Code Interpreter",
            description="Write and run Python code in a Foundry-managed sandbox.",
            operations=(
                _ga("run"),
                _preview("custom_environment", _PREVIEW_REASON),
            ),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.file_search",
            provider="microsoft_foundry",
            title="File Search",
            description="Augment agents with knowledge from uploaded files.",
            operations=(_ga("search"),),
            auth_requirements=("workspace_connection:file_store",),
            risk_tier="low",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.azure_ai_search",
            provider="microsoft_foundry",
            title="Azure AI Search",
            description="Ground agents with data from an existing Azure AI Search index.",
            operations=(_ga("search"),),
            auth_requirements=("workspace_connection:azure_ai_search",),
            risk_tier="low",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.azure_functions",
            provider="microsoft_foundry",
            title="Azure Functions",
            description="Call Azure Functions to perform custom actions and retrieve dynamic data.",
            operations=(_ga("invoke"),),
            auth_requirements=("workspace_connection:azure_functions",),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.function_calling",
            provider="microsoft_foundry",
            title="Function Calling",
            description="Define custom functions the agent can call; the caller executes and returns results.",
            operations=(_ga("invoke"),),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.memory",
            provider="microsoft_foundry",
            title="Foundry Native Memory",
            description="Foundry-managed long-term agent memory store.",
            operations=(_preview("recall", _PREVIEW_REASON), _preview("store", _PREVIEW_REASON)),
            risk_tier="high",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.custom_code_interpreter",
            provider="microsoft_foundry",
            title="Custom Code Interpreter",
            description="Customized code interpreter resources/packages/Container Apps environment.",
            operations=(_preview("run", _PREVIEW_REASON),),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.image_generation",
            provider="microsoft_foundry",
            title="Image Generation",
            description="Generate images as part of conversations and workflows.",
            operations=(_preview("generate", _PREVIEW_REASON),),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.browser_automation",
            provider="microsoft_foundry",
            title="Browser Automation",
            description="Perform browser tasks through natural language prompts.",
            operations=(_preview("run", _PREVIEW_REASON),),
            risk_tier="high",
            data_boundary="public",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.computer_use",
            provider="microsoft_foundry",
            title="Computer Use",
            description="Interact with computer systems through their user interfaces.",
            operations=(_preview("run", _PREVIEW_REASON),),
            risk_tier="high",
            data_boundary="public",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.microsoft_fabric",
            provider="microsoft_foundry",
            title="Microsoft Fabric",
            description="Connect to a Microsoft Fabric data agent for data analysis.",
            operations=(_preview("query", _PREVIEW_REASON),),
            auth_requirements=("workspace_connection:fabric",),
            risk_tier="medium",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.sharepoint",
            provider="microsoft_foundry",
            title="SharePoint",
            description="Chat with private documents stored in SharePoint.",
            operations=(_preview("search", _PREVIEW_REASON),),
            auth_requirements=("workspace_connection:sharepoint",),
            risk_tier="medium",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.toolbox_connector",
            provider="microsoft_foundry",
            title="Toolbox Connector (Custom · Preview)",
            description="Connector-namespace managed MCP tool access (gateway_connector / catalog_MCP).",
            operations=(_preview("invoke", _PREVIEW_REASON),),
            auth_requirements=("workspace_connection:toolbox",),
            risk_tier="high",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.a2a",
            provider="microsoft_foundry",
            title="Agent2Agent (A2A)",
            description="Call another agent over the A2A protocol.",
            operations=(_preview("call", _PREVIEW_REASON),),
            risk_tier="high",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="custom.hosted_code",
            provider="custom_hosted",
            title="Custom Hosted Code",
            description="Arbitrary application code running in a Custom Hosted container; not Foundry-native.",
            operations=(_unavailable("run", "Custom code cannot run inside Managed Foundry runtime."),),
            risk_tier="high",
            data_boundary="project",
            managed_foundry_native=False,
        ),
    )


class CapabilityRegistry:
    """In-memory capability catalog with GA-only attach enforcement."""

    def __init__(self, descriptors: tuple[CapabilityDescriptor, ...] | None = None) -> None:
        seed = descriptors if descriptors is not None else _seed_descriptors()
        self._descriptors: dict[str, CapabilityDescriptor] = {descriptor.id: descriptor for descriptor in seed}

    def catalog(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptors.values())

    def get(self, descriptor_id: str) -> CapabilityDescriptor | None:
        return self._descriptors.get(descriptor_id)

    def as_mapping(self) -> dict[str, CapabilityDescriptor]:
        return dict(self._descriptors)

    def validate_attachment(
        self,
        *,
        descriptor_id: str,
        operation: str,
    ) -> CapabilityOperation:
        """Validate that ``operation`` on ``descriptor_id`` is GA-attachable.

        Returns the resolved ``CapabilityOperation`` on success; raises
        ``CapabilityAttachmentError`` with an honest reason otherwise.
        """
        descriptor = self._descriptors.get(descriptor_id)
        if descriptor is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' is not in the catalog.")
        resolved = descriptor.operation(operation)
        if resolved is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' has no operation '{operation}'.")
        if resolved.maturity != OperationMaturity.GA:
            reason = resolved.reason or f"Operation '{operation}' is {resolved.maturity.value}."
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': {reason}"
            )
        return resolved

    def attach(
        self,
        *,
        descriptor_id: str,
        operation: str,
        attached_by: str,
        workspace_connection_id: str | None = None,
        config: dict[str, object] | None = None,
    ) -> CapabilityInstance:
        """Validate and construct a ``CapabilityInstance`` for a GA operation."""
        self.validate_attachment(descriptor_id=descriptor_id, operation=operation)
        return CapabilityInstance(
            descriptor_id=descriptor_id,
            operation=operation,
            workspace_connection_id=workspace_connection_id,
            config=dict(config or {}),
            attached_by=attached_by,
        )


def default_registry() -> CapabilityRegistry:
    return CapabilityRegistry()
