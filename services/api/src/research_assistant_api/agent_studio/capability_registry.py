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
(no network call at import time). It is a **transitional, local-only
fallback**: the platform correction requires Agent Studio to *consume*
provider discovery through an interface owned by the integration/harness
session rather than duplicate Foundry/tool discovery here. See
``capability_discovery.CapabilityDiscoverySource`` for that seam —
``CapabilityRegistry.from_source`` builds a registry entirely from an
injected source's output (no seed mixed in), and ``default_registry`` only
falls back to this hard-coded seed when no source is supplied, e.g. while
the real provider adapter has not yet been wired at this call site.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoverySource,
)
from research_assistant_api.agent_studio.models import (
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    InstanceReadiness,
    OperationClass,
    OperationLifecycle,
    OperationMaturity,
    utc_now,
)

_LEARN_TOOL_CATALOG_URL = (
    "https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/tool-catalog"
)


class CapabilityAttachmentError(ValueError):
    """Raised when an attempted capability attachment is not GA-eligible."""


def _canonical_digest(payload: Any) -> str:
    """Canonical ``sha256:``-prefixed digest of a JSON-serializable payload.

    Same convention used by ``schema_ref_resolver.compute_schema_digest``
    (sorted-key, compact-separator canonical JSON) so every content digest
    in this package is computed identically.
    """

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_descriptor_digest(descriptor: CapabilityDescriptor) -> str:
    """Content digest of a ``CapabilityDescriptor``, pinned by attaching bindings.

    Pins the descriptor's *content*, not just its ``version`` string, so a
    catalog edit that changes semantics without bumping the version cannot
    silently change an already-attached binding's behavior.
    """

    return _canonical_digest(descriptor.model_dump(mode="json"))


def compute_config_hash(config: dict[str, Any]) -> str:
    """Canonical digest of a binding's non-secret ``config`` dict."""

    return _canonical_digest(config)


def compute_instance_fingerprint(
    descriptor: CapabilityDescriptor, instance: CapabilityInstance
) -> str:
    """Canonical digest pinning a discovered ``CapabilityInstance``.

    Pins provider/descriptor/operation identity, operation definitions and
    versions, side-effect destinations, tenant/data boundaries, and
    non-secret discovered configuration. Deliberately excludes
    health/timestamps/secrets/credential material so a readiness flap alone
    never invalidates a pinned binding — only a genuine reconfiguration
    (different descriptor content, different operations, different
    instance/tenant/project/connection identity, or different discovered
    config) changes this value.
    """

    payload = {
        "provider": descriptor.provider,
        "descriptor_id": descriptor.id,
        "descriptor_version": descriptor.version,
        "descriptor_digest": compute_descriptor_digest(descriptor),
        "operations": sorted(
            (
                {
                    "name": op.name,
                    "version": op.version,
                    "maturity": op.maturity.value,
                    "lifecycle": op.lifecycle.value,
                    "operation_class": op.operation_class.value,
                    "input_schema_digest": op.input_schema_digest,
                    "output_schema_digest": op.output_schema_digest,
                    "side_effect_destinations": sorted(op.side_effect_destinations),
                }
                for op in descriptor.operations
            ),
            key=lambda entry: str(entry["name"]),
        ),
        "instance_id": instance.id,
        "tenant_id": instance.tenant_id,
        "project_id": instance.project_id,
        "discovered_provider_version": instance.discovered_provider_version,
        "config_fingerprint": instance.config_fingerprint,
    }
    return _canonical_digest(payload)


def _ga(
    name: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.READ,
    side_effect_destinations: tuple[str, ...] = (),
    requires_approval: bool = False,
    approval_policy_ref: str | None = None,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.GA,
        operation_class=operation_class,
        side_effect_destinations=side_effect_destinations,
        requires_approval=requires_approval,
        approval_policy_ref=approval_policy_ref,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _preview(
    name: str,
    reason: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.READ,
    side_effect_destinations: tuple[str, ...] = (),
    requires_approval: bool = False,
    approval_policy_ref: str | None = None,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.PREVIEW,
        operation_class=operation_class,
        side_effect_destinations=side_effect_destinations,
        requires_approval=requires_approval,
        approval_policy_ref=approval_policy_ref,
        reason=reason,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _retired(
    name: str,
    reason: str,
    *,
    version: str = "1",
    maturity: OperationMaturity = OperationMaturity.GA,
    operation_class: OperationClass = OperationClass.PRIVILEGED,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    """An operation the provider has documented as retired/removed.

    ``maturity`` defaults to ``GA`` — a retired operation typically *was* GA
    before withdrawal, and its maturity claim does not change on retirement.
    ``lifecycle=RETIRED`` is what actually fails it closed:
    ``CapabilityOperation.is_bindable`` requires both ``GA`` maturity **and**
    ``ACTIVE`` lifecycle, so a retired operation is never attachable
    regardless of its (possibly still-``GA``) maturity value.
    """
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=maturity,
        lifecycle=OperationLifecycle.RETIRED,
        operation_class=operation_class,
        reason=reason,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _unknown(
    name: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.PRIVILEGED,
    reason: str = "Maturity has not yet been verified against official provenance.",
) -> CapabilityOperation:
    """An operation whose maturity has not been verified (or does not apply).

    ``unknown`` is deliberately fail-closed and non-attachable until
    provenance (``source_url``/``source_version``/``last_verified_at``) is
    recorded and the maturity is re-classified as ``ga``/``preview``. Also
    used for operations that are structurally inapplicable in a given
    runtime (e.g. custom hosted code under Managed Foundry) via the
    ``reason`` override, since ``OperationMaturity`` has no separate
    "unavailable" tier — an inapplicable operation is, honestly, one whose
    GA-maturity has not (and will never be) confirmed here.
    """
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.UNKNOWN,
        operation_class=operation_class,
        reason=reason,
    )


_PREVIEW_REASON = "Documented as preview in the Foundry Agent Service tool catalog."


def _seed_descriptors() -> tuple[CapabilityDescriptor, ...]:
    return (
        CapabilityDescriptor(
            id="foundry.web_search",
            provider="microsoft_foundry",
            title="Web Search",
            description="Retrieve real-time public web information with inline citations.",
            operations=(_ga("search", side_effect_destinations=("public_web",)),),
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
                _ga(
                    "run",
                    operation_class=OperationClass.WRITE_REVERSIBLE,
                    side_effect_destinations=("foundry_sandbox",),
                ),
                _preview(
                    "custom_environment",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_REVERSIBLE,
                    side_effect_destinations=("foundry_sandbox",),
                ),
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
            operations=(_ga("search", side_effect_destinations=("file_store",)),),
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
            operations=(_ga("search", side_effect_destinations=("azure_ai_search_index",)),),
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
            operations=(
                _ga(
                    "invoke",
                    operation_class=OperationClass.WRITE_IRREVERSIBLE,
                    side_effect_destinations=("azure_functions",),
                    requires_approval=True,
                    approval_policy_ref="policy.capability-approval.write-irreversible.v1",
                ),
            ),
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
            operations=(_ga("invoke", operation_class=OperationClass.PURE),),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.memory",
            provider="microsoft_foundry",
            title="Foundry Native Memory",
            description="Foundry-managed long-term agent memory store.",
            operations=(
                _preview("recall", _PREVIEW_REASON, side_effect_destinations=("foundry_memory_store",)),
                _preview(
                    "store",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_REVERSIBLE,
                    side_effect_destinations=("foundry_memory_store",),
                ),
            ),
            risk_tier="high",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.custom_code_interpreter",
            provider="microsoft_foundry",
            title="Custom Code Interpreter",
            description="Customized code interpreter resources/packages/Container Apps environment.",
            operations=(
                _preview(
                    "run",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_REVERSIBLE,
                    side_effect_destinations=("container_apps_environment",),
                ),
            ),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.image_generation",
            provider="microsoft_foundry",
            title="Image Generation",
            description="Generate images as part of conversations and workflows.",
            operations=(
                _preview(
                    "generate",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_REVERSIBLE,
                    side_effect_destinations=("generated_media_store",),
                ),
            ),
            risk_tier="medium",
            data_boundary="project",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.browser_automation",
            provider="microsoft_foundry",
            title="Browser Automation",
            description="Perform browser tasks through natural language prompts.",
            operations=(
                _preview(
                    "run",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_IRREVERSIBLE,
                    side_effect_destinations=("public_web",),
                    requires_approval=True,
                    approval_policy_ref="policy.capability-approval.write-irreversible.v1",
                ),
            ),
            risk_tier="high",
            data_boundary="public",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.computer_use",
            provider="microsoft_foundry",
            title="Computer Use",
            description="Interact with computer systems through their user interfaces.",
            operations=(
                _preview(
                    "run",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.PRIVILEGED,
                    side_effect_destinations=("host_computer",),
                    requires_approval=True,
                    approval_policy_ref="policy.capability-approval.privileged.v1",
                ),
            ),
            risk_tier="high",
            data_boundary="public",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="foundry.microsoft_fabric",
            provider="microsoft_foundry",
            title="Microsoft Fabric",
            description="Connect to a Microsoft Fabric data agent for data analysis.",
            operations=(_preview("query", _PREVIEW_REASON, side_effect_destinations=("microsoft_fabric",)),),
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
            operations=(_preview("search", _PREVIEW_REASON, side_effect_destinations=("sharepoint",)),),
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
            operations=(
                _preview(
                    "invoke",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_IRREVERSIBLE,
                    side_effect_destinations=("toolbox_connector",),
                    requires_approval=True,
                    approval_policy_ref="policy.capability-approval.write-irreversible.v1",
                ),
            ),
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
            operations=(
                _preview(
                    "call",
                    _PREVIEW_REASON,
                    operation_class=OperationClass.WRITE_IRREVERSIBLE,
                    side_effect_destinations=("a2a_peer_agent",),
                    requires_approval=True,
                    approval_policy_ref="policy.capability-approval.write-irreversible.v1",
                ),
            ),
            risk_tier="high",
            data_boundary="tenant",
            managed_foundry_native=True,
        ),
        CapabilityDescriptor(
            id="custom.hosted_code",
            provider="custom_hosted",
            title="Custom Hosted Code",
            description="Arbitrary application code running in a Custom Hosted container; not Foundry-native.",
            operations=(_unknown("run", reason="Custom code cannot run inside Managed Foundry runtime."),),
            risk_tier="high",
            data_boundary="project",
            managed_foundry_native=False,
        ),
    )


class CapabilityRegistry:
    """In-memory capability catalog with GA-only attach enforcement.

    Also holds the *discovered* ``CapabilityInstance`` set (tenant/project
    resources such as a specific Azure AI Search index connection). Instances
    remain governance-adjacent catalog data — they are provider-discovered
    facts about what a tenant/project actually has available, not part of any
    agent's manifest — so they live alongside the descriptor catalog rather
    than in a separate store.
    """

    def __init__(self, descriptors: tuple[CapabilityDescriptor, ...] | None = None) -> None:
        seed = descriptors if descriptors is not None else _seed_descriptors()
        self._descriptors: dict[str, CapabilityDescriptor] = {descriptor.id: descriptor for descriptor in seed}
        self._instances: dict[str, CapabilityInstance] = {}
        self._warnings: tuple[str, ...] = ()
        self._refreshed_at: datetime = utc_now()

    @classmethod
    def from_source(cls, source: CapabilityDiscoverySource) -> CapabilityRegistry:
        """Build a registry entirely from a ``CapabilityDiscoverySource``.

        No local seed catalog is mixed in: the injected source is treated
        as the authoritative, real discovery output (or an honestly empty
        one), never merged with or silently overridden by hard-coded data.
        Discovered instances are registered immediately so they resolve via
        ``get_instance``/``instances_for`` without a separate wiring step.
        Discovery ``warnings`` are preserved (surfaced via ``/capabilities/
        discovery`` for admins/operators) and ``refreshed_at`` records when
        this discovery pass ran.
        """
        result = source.discover()
        registry = cls(descriptors=result.descriptors)
        for instance in result.instances:
            registry.register_instance(instance)
        registry._warnings = result.warnings
        registry._refreshed_at = utc_now()
        return registry

    @property
    def warnings(self) -> tuple[str, ...]:
        """Honest, non-fatal discovery caveats from the last discovery pass."""
        return self._warnings

    @property
    def refreshed_at(self) -> datetime:
        """When this registry's catalog/instances were last (re)discovered."""
        return self._refreshed_at

    def catalog(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptors.values())

    def get(self, descriptor_id: str) -> CapabilityDescriptor | None:
        return self._descriptors.get(descriptor_id)

    def as_mapping(self) -> dict[str, CapabilityDescriptor]:
        return dict(self._descriptors)

    def register_instance(self, instance: CapabilityInstance) -> CapabilityInstance:
        """Register (or replace) a discovered capability instance.

        Registration is dynamic-discovery-shaped (callers supply the
        discovered facts; nothing here fabricates readiness/health), but
        held in-memory for now — consistent with the rest of the registry,
        which is itself an in-memory catalog seeded at process start.

        The instance must reference a known ``descriptor_id`` — a
        fingerprint cannot honestly pin a descriptor this registry does not
        have, so an unknown reference fails closed rather than registering
        an un-pinnable instance. ``instance_fingerprint`` is always
        (re)computed here from the current descriptor + instance facts,
        never trusted verbatim from the caller, so registration cannot be
        used to smuggle in a stale or fabricated pin.
        """
        descriptor = self._descriptors.get(instance.descriptor_id)
        if descriptor is None:
            raise CapabilityAttachmentError(
                f"Capability instance references unknown descriptor '{instance.descriptor_id}'."
            )
        stamped = instance.model_copy(
            update={
                "instance_fingerprint": compute_instance_fingerprint(descriptor, instance),
                "descriptor_digest": compute_descriptor_digest(descriptor),
            }
        )
        self._instances[stamped.id] = stamped
        return stamped

    def get_instance(self, instance_id: str) -> CapabilityInstance | None:
        return self._instances.get(instance_id)

    def instances_for(self, *, tenant_id: str, project_id: str | None = None) -> tuple[CapabilityInstance, ...]:
        return tuple(
            instance
            for instance in self._instances.values()
            if instance.tenant_id == tenant_id and (project_id is None or instance.project_id == project_id)
        )

    def validate_attachment(
        self,
        *,
        descriptor_id: str,
        operation: str,
    ) -> CapabilityOperation:
        """Validate that ``operation`` on ``descriptor_id`` is GA+ACTIVE-attachable.

        Returns the resolved ``CapabilityOperation`` on success; raises
        ``CapabilityAttachmentError`` with an honest reason otherwise.
        Bindability requires ``CapabilityOperation.is_bindable`` (both
        ``OperationMaturity.GA`` and ``OperationLifecycle.ACTIVE`` — a GA
        operation that has been deprecated/retired is rejected the same as
        a non-GA one). When the operation ``requires_approval``, its
        declared ``approval_policy_ref`` must be present — an operation
        flagged as approval-gated with no governing policy reference is an
        unsatisfiable, catalog-authoring inconsistency and must never be
        silently treated as attachable.
        """
        descriptor = self._descriptors.get(descriptor_id)
        if descriptor is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' is not in the catalog.")
        resolved = descriptor.operation(operation)
        if resolved is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' has no operation '{operation}'.")
        if not resolved.is_bindable:
            if resolved.maturity != OperationMaturity.GA:
                default_reason = f"Operation '{operation}' is {resolved.maturity.value}."
            else:
                default_reason = (
                    f"Operation '{operation}' is GA maturity but {resolved.lifecycle.value} lifecycle "
                    "(not active)."
                )
            reason = resolved.reason or default_reason
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': {reason}"
            )
        if resolved.requires_approval and resolved.approval_policy_ref is None:
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': it requires approval but declares no "
                "approval_policy_ref, so the approval requirement is unsatisfiable."
            )
        return resolved

    def attach(
        self,
        *,
        descriptor_id: str,
        operation: str,
        attached_by: str,
        instance_id: str | None = None,
        connection_ref: str | None = None,
        policy_ref: str | None = None,
        config: dict[str, object] | None = None,
    ) -> CapabilityBinding:
        """Validate and construct a ``CapabilityBinding`` for a GA operation.

        When ``instance_id`` is supplied it must resolve to a registered
        ``CapabilityInstance`` for the same ``descriptor_id``; the binding
        pins the instance's ``discovered_provider_version`` so a later
        instance re-discovery never silently changes an already-attached
        binding's behavior. When the resolved operation ``requires_approval``,
        a ``policy_ref`` must be supplied — attach-time satisfiability of an
        approval-gated operation means the caller has identified *how*
        approval will be sought, not that it has already been granted (that
        is enforced later, at the APPROVAL release gate and again at
        deploy time).
        """
        resolved = self.validate_attachment(descriptor_id=descriptor_id, operation=operation)
        if resolved.requires_approval and policy_ref is None:
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': it requires approval, so a policy_ref "
                "identifying the governing approval policy must be supplied."
            )
        descriptor = self._descriptors[descriptor_id]
        pinned_provider_version: str | None = None
        instance_fingerprint: str | None = None
        if instance_id is not None:
            instance = self._instances.get(instance_id)
            if instance is None:
                raise CapabilityAttachmentError(f"Capability instance '{instance_id}' is not registered.")
            if instance.descriptor_id != descriptor_id:
                raise CapabilityAttachmentError(
                    f"Capability instance '{instance_id}' belongs to descriptor "
                    f"'{instance.descriptor_id}', not '{descriptor_id}'."
                )
            if instance.readiness == InstanceReadiness.UNAVAILABLE:
                raise CapabilityAttachmentError(
                    f"Capability instance '{instance_id}' is unavailable: "
                    f"{instance.unavailable_reason or 'no reason supplied'}."
                )
            pinned_provider_version = instance.discovered_provider_version
            instance_fingerprint = instance.instance_fingerprint or compute_instance_fingerprint(
                descriptor, instance
            )
        resolved_config = dict(config or {})
        return CapabilityBinding(
            descriptor_id=descriptor_id,
            descriptor_version=descriptor.version,
            descriptor_digest=compute_descriptor_digest(descriptor),
            operation=operation,
            operation_version=resolved.version,
            instance_id=instance_id,
            pinned_provider_version=pinned_provider_version,
            instance_fingerprint=instance_fingerprint,
            input_schema_digest=resolved.input_schema_digest,
            output_schema_digest=resolved.output_schema_digest,
            config=resolved_config,
            config_hash=compute_config_hash(resolved_config),
            connection_ref=connection_ref,
            policy_ref=policy_ref,
            destination_constraints=resolved.side_effect_destinations,
            attached_by=attached_by,
        )

    def check_binding_freshness(self, binding: CapabilityBinding) -> str | None:
        """Return a stale-binding reason, or ``None`` if the binding is fresh.

        Re-resolves the binding's ``descriptor_id``/``instance_id`` against
        the *current* registry state and compares digests/fingerprints. A
        release/invoke path must call this and hard-fail on a non-``None``
        result — a binding whose pinned descriptor/instance no longer
        matches the live catalog must never be silently honored.
        """
        descriptor = self._descriptors.get(binding.descriptor_id)
        if descriptor is None:
            return f"Descriptor '{binding.descriptor_id}' is no longer in the catalog."
        current_descriptor_digest = compute_descriptor_digest(descriptor)
        if binding.descriptor_digest is not None and current_descriptor_digest != binding.descriptor_digest:
            return (
                f"Descriptor '{binding.descriptor_id}' content has changed since attach "
                "(descriptor_digest mismatch)."
            )
        current_operation = descriptor.operation(binding.operation)
        if current_operation is None:
            return f"Operation '{binding.operation}' no longer exists on descriptor '{binding.descriptor_id}'."
        if not current_operation.is_bindable:
            return (
                f"Operation '{binding.descriptor_id}.{binding.operation}' is no longer bindable "
                f"({current_operation.maturity.value} maturity / {current_operation.lifecycle.value} "
                "lifecycle) — rebind and re-review before release/invoke."
            )
        if binding.operation_version is not None and current_operation.version != binding.operation_version:
            return (
                f"Operation '{binding.descriptor_id}.{binding.operation}' version has changed since attach "
                f"(operation_version mismatch: pinned '{binding.operation_version}', now "
                f"'{current_operation.version}') — rebind and re-review before release/invoke."
            )
        if (
            binding.destination_constraints
            and tuple(current_operation.side_effect_destinations) != tuple(binding.destination_constraints)
        ):
            return (
                f"Operation '{binding.descriptor_id}.{binding.operation}' side-effect destinations have "
                "changed since attach (destination_constraints mismatch) — rebind and re-review before "
                "release/invoke."
            )
        if binding.instance_id is not None:
            instance = self._instances.get(binding.instance_id)
            if instance is None:
                return f"Capability instance '{binding.instance_id}' is no longer registered."
            if instance.readiness == InstanceReadiness.UNAVAILABLE:
                return (
                    f"Capability instance '{binding.instance_id}' is unavailable: "
                    f"{instance.unavailable_reason or 'no reason supplied'}."
                )
            if binding.instance_fingerprint is not None:
                current_fingerprint = instance.instance_fingerprint or compute_instance_fingerprint(
                    descriptor, instance
                )
                if current_fingerprint != binding.instance_fingerprint:
                    return (
                        f"Capability instance '{binding.instance_id}' has been reconfigured since attach "
                        "(instance_fingerprint mismatch) — rebind and re-review before release/invoke."
                    )
        return None


def default_registry(source: CapabilityDiscoverySource | None = None) -> CapabilityRegistry:
    """Build the process-default capability registry.

    When ``source`` is supplied (a real provider-integration adapter), the
    registry is built entirely from its discovery output via
    ``CapabilityRegistry.from_source`` — the local hard-coded seed catalog
    is not consulted at all. When no source is supplied (no adapter wired
    at this call site yet), this falls back to the documented local seed as
    a transitional default.
    """
    if source is not None:
        return CapabilityRegistry.from_source(source)
    return CapabilityRegistry()
