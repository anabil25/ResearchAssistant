from __future__ import annotations

from research_assistant_api.agent_studio.capability_registry import default_registry
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    CapabilityInstance,
    RuntimeRequirements,
    RuntimeTarget,
)
from research_assistant_api.agent_studio.runtime_selection import select_runtime


def _manifest(**overrides: object) -> AgentManifest:
    base: dict[str, object] = {
        "logical_agent_id": "agent-test-runtime",
        "tenant_id": "demo",
        "display_name": "Runtime Test Agent",
        "owner_kind": AgentOwnerKind.USER,
        "owner_id": "user-1",
    }
    base.update(overrides)
    return AgentManifest(**base)  # type: ignore[arg-type]


def test_select_runtime_managed_foundry_when_no_disqualifiers() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.web_search", operation="search", attached_by="user-1"),
        )
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.MANAGED_FOUNDRY
    assert len(selection.reasons) == 3


def test_select_runtime_custom_hosted_when_custom_code_required() -> None:
    registry = default_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_custom_code=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_custom_code" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_custom_orchestration_required() -> None:
    registry = default_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_custom_orchestration_workflow=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_custom_orchestration_workflow" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_non_ga_tool_required() -> None:
    registry = default_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(requires_non_ga_tool=True))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("requires_non_ga_tool" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_model_source_not_project_deployed() -> None:
    registry = default_registry()
    manifest = _manifest(runtime_requirements=RuntimeRequirements(uses_project_deployed_model_only=False))
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("model source" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_capability_missing_from_catalog() -> None:
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="unknown.capability", operation="run", attached_by="user-1"),
        )
    )
    selection = select_runtime(manifest, {})
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("not present in the capability catalog" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_capability_not_foundry_native() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="custom.hosted_code", operation="run", attached_by="user-1"),
        )
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("no Managed Foundry native implementation" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_operation_not_declared() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.web_search", operation="not_declared", attached_by="user-1"),
        )
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("is not declared" in reason for reason in selection.reasons)


def test_select_runtime_custom_hosted_when_operation_not_ga() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.code_interpreter", operation="custom_environment", attached_by="user-1"
            ),
        )
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert any("not GA" in reason for reason in selection.reasons)


def test_select_runtime_collects_multiple_disqualifiers() -> None:
    registry = default_registry()
    manifest = _manifest(
        runtime_requirements=RuntimeRequirements(requires_custom_code=True, requires_non_ga_tool=True),
        capabilities=(
            CapabilityInstance(descriptor_id="custom.hosted_code", operation="run", attached_by="user-1"),
        ),
    )
    selection = select_runtime(manifest, registry.as_mapping())
    assert selection.target == RuntimeTarget.CUSTOM_HOSTED
    assert len(selection.reasons) == 3
