from __future__ import annotations

from research_assistant_api.agent_studio.capability_registry import default_registry
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentVisibility,
    CapabilityInstance,
    GateName,
    GateResult,
    GateStatus,
    ReleaseGateReport,
    RuntimeRequirements,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence, run_gates


def _manifest(**overrides: object) -> AgentManifest:
    base: dict[str, object] = {
        "logical_agent_id": "agent-test-gates",
        "tenant_id": "demo",
        "display_name": "Gates Test Agent",
        "owner_kind": AgentOwnerKind.USER,
        "owner_id": "user-1",
    }
    base.update(overrides)
    return AgentManifest(**base)  # type: ignore[arg-type]


def _gate(report: ReleaseGateReport, name: GateName) -> GateResult:
    return next(result for result in report.results if result.name == name)


def test_run_gates_all_pass_with_evidence_and_no_capabilities() -> None:
    registry = default_registry()
    manifest = _manifest()
    report = run_gates(
        version_id="version-1",
        report_id="report-1",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    assert report.passed
    for name in GateName:
        assert _gate(report, name).status == GateStatus.PASSED


def test_schema_gate_passes_for_valid_manifest() -> None:
    registry = default_registry()
    manifest = _manifest()
    report = run_gates(
        version_id="v", report_id="r", manifest=manifest, capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SCHEMA).status == GateStatus.PASSED


def test_build_test_smoke_gates_are_skipped_without_evidence() -> None:
    registry = default_registry()
    manifest = _manifest()
    report = run_gates(
        version_id="v", report_id="r", manifest=manifest, capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.BUILD).status == GateStatus.SKIPPED
    assert _gate(report, GateName.TEST).status == GateStatus.SKIPPED
    assert _gate(report, GateName.SMOKE).status == GateStatus.SKIPPED
    assert not report.passed


def test_build_test_smoke_gates_fail_with_negative_evidence() -> None:
    registry = default_registry()
    manifest = _manifest()
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(
            build_succeeded=False,
            build_detail="compile error",
            tests_passed=False,
            test_detail="assertion failed",
            smoke_passed=False,
            smoke_detail="endpoint down",
        ),
    )
    assert _gate(report, GateName.BUILD).status == GateStatus.FAILED
    assert _gate(report, GateName.BUILD).detail == "compile error"
    assert _gate(report, GateName.TEST).status == GateStatus.FAILED
    assert _gate(report, GateName.TEST).detail == "assertion failed"
    assert _gate(report, GateName.SMOKE).status == GateStatus.FAILED
    assert _gate(report, GateName.SMOKE).detail == "endpoint down"


def test_build_test_smoke_gates_pass_with_default_detail() -> None:
    registry = default_registry()
    manifest = _manifest()
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    assert _gate(report, GateName.BUILD).detail == "Build succeeded."
    assert _gate(report, GateName.TEST).detail == "Tests passed."
    assert _gate(report, GateName.SMOKE).detail == "Smoke test passed."


def test_auth_gate_fails_when_capability_missing_from_catalog() -> None:
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="unknown.capability", operation="run", attached_by="user-1"),
        )
    )
    report = run_gates(
        version_id="v", report_id="r", manifest=manifest, capability_catalog={}, evidence=GateEvidence()
    )
    assert _gate(report, GateName.AUTH).status == GateStatus.FAILED
    assert "not in the capability catalog" in _gate(report, GateName.AUTH).detail


def test_auth_gate_fails_when_workspace_connection_missing() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.file_search", operation="search", attached_by="user-1"),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.AUTH).status == GateStatus.FAILED
    assert "requires a workspace connection" in _gate(report, GateName.AUTH).detail


def test_auth_gate_passes_when_workspace_connection_attached() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.file_search",
                operation="search",
                attached_by="user-1",
                workspace_connection_id="conn-1",
            ),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.AUTH).status == GateStatus.PASSED


def test_policy_gate_fails_for_non_ga_operation() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.memory", operation="recall", attached_by="user-1"),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.POLICY).status == GateStatus.FAILED
    assert "not a GA operation" in _gate(report, GateName.POLICY).detail


def test_policy_gate_fails_for_missing_capability_in_catalog() -> None:
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="unknown.capability", operation="run", attached_by="user-1"),
        )
    )
    report = run_gates(
        version_id="v", report_id="r", manifest=manifest, capability_catalog={}, evidence=GateEvidence()
    )
    assert _gate(report, GateName.POLICY).status == GateStatus.FAILED
    assert "not in the capability catalog" in _gate(report, GateName.POLICY).detail


def test_policy_gate_fails_for_high_risk_capability_at_org_visibility() -> None:
    registry = default_registry()
    manifest = _manifest(
        visibility=AgentVisibility.ORG,
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.browser_automation", operation="run", attached_by="user-1"
            ),
        ),
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.POLICY).status == GateStatus.FAILED
    assert "cannot be released at" in _gate(report, GateName.POLICY).detail


def test_policy_gate_passes_for_low_risk_capability_at_published_visibility() -> None:
    registry = default_registry()
    manifest = _manifest(
        visibility=AgentVisibility.PUBLISHED,
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.web_search", operation="search", attached_by="user-1"),
        ),
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.POLICY).status == GateStatus.PASSED


def test_security_gate_detects_embedded_secret_in_config() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.azure_functions",
                operation="invoke",
                attached_by="user-1",
                workspace_connection_id="conn-1",
                config={"api_key": "sk-abcdefghijklmnopqrstuvwx"},
            ),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SECURITY).status == GateStatus.FAILED
    assert "embedded secret" in _gate(report, GateName.SECURITY).detail


def test_security_gate_detects_secret_nested_in_list_config() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.azure_functions",
                operation="invoke",
                attached_by="user-1",
                workspace_connection_id="conn-1",
                config={"keys": ["sk-abcdefghijklmnopqrstuvwx"]},
            ),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SECURITY).status == GateStatus.FAILED


def test_security_gate_flags_system_owned_high_risk_public_capability() -> None:
    registry = default_registry()
    manifest = _manifest(
        owner_kind=AgentOwnerKind.SYSTEM,
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.browser_automation", operation="run", attached_by="user-1"
            ),
        ),
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SECURITY).status == GateStatus.FAILED
    assert "system-owned agent attaches high-risk" in _gate(report, GateName.SECURITY).detail


def test_schema_gate_fails_when_manifest_data_violates_its_own_schema() -> None:
    # Bypass Pydantic validation via model_construct to simulate a manifest
    # object that was mutated/corrupted after construction; the schema gate
    # re-validates the dumped payload and must catch it rather than assume
    # an already-constructed model is still valid.
    registry = default_registry()
    manifest = AgentManifest.model_construct(
        logical_agent_id="not a valid id!!",
        tenant_id="demo",
        display_name="Broken Agent",
        description="",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        visibility=AgentVisibility.PRIVATE,
        capabilities=(),
        runtime_requirements=RuntimeRequirements(),
        memory_scopes=(),
        workspace_connections=(),
        tags=(),
    )
    report = run_gates(
        version_id="v", report_id="r", manifest=manifest, capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SCHEMA).status == GateStatus.FAILED


def test_security_gate_ignores_non_string_non_container_config_values() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(
                descriptor_id="foundry.azure_functions",
                operation="invoke",
                attached_by="user-1",
                workspace_connection_id="conn-1",
                config={"retry_count": 3, "enabled": True},
            ),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SECURITY).status == GateStatus.PASSED


def test_security_gate_passes_with_no_findings() -> None:
    registry = default_registry()
    manifest = _manifest(
        capabilities=(
            CapabilityInstance(descriptor_id="foundry.web_search", operation="search", attached_by="user-1"),
        )
    )
    report = run_gates(
        version_id="v",
        report_id="r",
        manifest=manifest,
        capability_catalog=registry.as_mapping(),
        evidence=GateEvidence(),
    )
    assert _gate(report, GateName.SECURITY).status == GateStatus.PASSED
