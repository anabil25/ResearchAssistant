"""Deterministic runtime auto-selection between Managed Foundry and Custom Hosted.

This is a pure function of the agent manifest and the capability catalog. It
never asks the model, never trusts a user-supplied "target" field, and always
defaults to the safer ``CUSTOM_HOSTED`` target unless every fact required for
Managed Foundry is proven true. All disqualifying reasons are collected (not
just the first) so the decision is fully auditable.
"""

from __future__ import annotations

from collections.abc import Mapping

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    CapabilityDescriptor,
    RuntimeSelection,
    RuntimeTarget,
)


def select_runtime(
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
) -> RuntimeSelection:
    """Select the runtime target for ``manifest``.

    Managed Foundry is only selected when *all* of the following hold:

    * the manifest does not declare a need for custom code, a custom
      ``agent_framework`` orchestration workflow, or a non-GA tool;
    * the manifest only uses project-deployed models;
    * every attached capability resolves to a catalog descriptor that is
      marked ``managed_foundry_native`` *and* whose specific attached
      operation is catalog-eligible (``OperationMaturity.GA`` **and**
      ``OperationLifecycle.ACTIVE``). Runtime selection is a pure function
      of manifest + catalog with no tenant/instance/connection/policy
      context, so it deliberately checks only this one catalog-level axis
      (not the full multi-axis bindability decision -- instance readiness,
      connection, policy/approval, freshness -- that
      ``CapabilityRegistry.validate_attachment``/``check_binding_freshness``
      perform for a live attach/release/deploy) — a capability that is
      catalog-eligible here can still fail full bindability at
      attach/gate/deploy time for reasons this function cannot see.

    Any violation of the above appends a disqualifying reason and forces
    ``RuntimeTarget.CUSTOM_HOSTED``.
    """
    disqualifiers: list[str] = []

    requirements = manifest.runtime_requirements
    if requirements.requires_custom_code:
        disqualifiers.append("Manifest declares requires_custom_code=true.")
    if requirements.requires_custom_orchestration_workflow:
        disqualifiers.append("Manifest declares requires_custom_orchestration_workflow=true.")
    if requirements.requires_non_ga_tool:
        disqualifiers.append("Manifest declares requires_non_ga_tool=true.")
    if not requirements.uses_project_deployed_model_only:
        disqualifiers.append("Manifest declares a model source other than project-deployed models.")

    for instance in manifest.capabilities:
        descriptor = capability_catalog.get(instance.descriptor_ref.id)
        if descriptor is None:
            disqualifiers.append(f"Capability '{instance.descriptor_ref.id}' is not present in the capability catalog.")
            continue
        if not descriptor.managed_foundry_native:
            disqualifiers.append(
                f"Capability '{instance.descriptor_ref.id}' has no Managed Foundry native implementation."
            )
            continue
        operation = descriptor.operation(instance.operation_ref.id)
        if operation is None:
            disqualifiers.append(
                f"Capability '{instance.descriptor_ref.id}' operation '{instance.operation_ref.id}' is not declared."
            )
            continue
        if not operation.is_catalog_eligible:
            disqualifiers.append(
                f"Capability '{instance.descriptor_ref.id}' operation '{instance.operation_ref.id}' is "
                f"{operation.maturity.value} maturity ({operation.lifecycle.value} lifecycle), "
                "not GA+ACTIVE."
            )

    if disqualifiers:
        return RuntimeSelection(target=RuntimeTarget.CUSTOM_HOSTED, reasons=tuple(disqualifiers))

    return RuntimeSelection(
        target=RuntimeTarget.MANAGED_FOUNDRY,
        reasons=(
            "No custom code, custom workflow, or non-GA tool is required.",
            "All attached capabilities are GA Managed Foundry-native operations.",
            "The manifest only uses project-deployed models.",
        ),
    )
