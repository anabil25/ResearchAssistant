# mypy: disable-error-code=import-untyped
from __future__ import annotations

from research_assistant_api.agent_studio.capability_registry import seeded_test_registry
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    CapabilityBinding,
    CapabilityDescriptorRef,
    CapabilityOperationRef,
    RuntimeRequirements,
    RuntimeTarget,
)
from research_assistant_api.agent_studio.runtime_selection import select_runtime


def _manifest(**overrides: object) -> AgentManifest:
    base: dict[str, object] = {
        "logical_agent_id": "agent-test-runtime",
        "tenant_id": "demo",
        "project_id": "proj-1",
        "display_name": "Runtime Test Agent",
        "owner_kind": AgentOwnerKind.USER,
        "owner_id": "user-1",
    }
    base.update(overrides)
    return AgentManifest(**base)  # type: ignore[arg-type]


def _binding(descriptor_id: str, operation: str) -> CapabilityBinding:
    return CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id=descriptor_id),
        operation_ref=CapabilityOperationRef(id=operation),
        attached_by="user-1",
    )


def test_select_runtime_managed_foundry_when_no_disqualifiers() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(
        capabilities=(_binding("foundry.web_search", "search"),)
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.MANAGED_FOUNDRY
    assert len(selection.reasons) == 3


def test_select_runtime_custom_hosted_when_custom_code_required() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_custom_code=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_custom_code" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_custom_orchestration_required() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_custom_orchestration_workflow=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_custom_orchestration_workflow" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_non_ga_tool_required() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_non_ga_tool=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_non_ga_tool" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_model_source_not_project_deployed() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(uses_project_deployed_model_only=False))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("model source" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_capability_missing_from_catalog() -> None:
    manifest = _manifest(
        capabilities=(_binding("unknown.capability", "run"),)
    )
    selection = select_runtime(manifest, {})
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("not present in the capability catalog" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_capability_not_foundry_native() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(
        capabilities=(_binding("custom.hosted_code", "run"),)
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("no Managed Foundry native implementation" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_operation_not_declared() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(
        capabilities=(_binding("foundry.web_search", "not_declared"),)
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("is not declared" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_operation_not_ga() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(
        capabilities=(_binding("foundry.code_interpreter", "custom_environment"),)
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("not GA" in reason for reason in selection.reasons)


def test_select_runtime_collects_multiple_disqualifiers() -> None:
    registry = seeded_test_registry()
    manifest = _manifest(
        runtime_requirements=RuntimeRequirements(requires_custom_code=True, requires_non_ga_tool=True),
        capabilities=(_binding("custom.hosted_code", "run"),),
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert len(selection.reasons) == 3
