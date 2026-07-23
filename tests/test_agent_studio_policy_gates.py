# mypy: disable-error-code=import-untyped

from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_registry import default_registry
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentVisibility,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityOperation,
    GateName,
    GateResult,
    GateStatus,
    OperationMaturity,
    ReleaseGateReport,
    RuntimeTarget,
)
from research_assistant_api.agent_studio.policy_gates import (
    GateEvidence,
    _auth_gate,
    _build_gate,
    _contains_secret,
    _policy_gate,
    _schema_gate,
    _security_gate,
    _smoke_gate,
    _test_gate,
    run_gates,
)


def _manifest(**overrides: object) -> AgentManifest:
    base: dict[str, object] = {
        "logical_agent_id": "agent-policy-gates",
        "tenant_id": "tenant-1",
        "display_name": "Policy Gates Agent",
        "owner_kind": AgentOwnerKind.USER,
        "owner_id": "user-1",
    }
    base.update(overrides)
    return AgentManifest(**base)


def _binding(
    descriptor_id: str,
    operation: str,
    *,
    connection_ref: str | None = None,
    config: dict[str, object] | None = None,
) -> CapabilityBinding:
    return CapabilityBinding(
        descriptor_id=descriptor_id,
        operation=operation,
        attached_by="user-1",
        connection_ref=connection_ref,
        config=config or {},
    )


def _descriptor(
    descriptor_id: str,
    *operations: CapabilityOperation,
    auth_requirements: tuple[str, ...] = (),
    risk_tier: str = "low",
    data_boundary: str = "project",
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=descriptor_id,
        provider="custom",
        title=descriptor_id,
        description=f"Descriptor {descriptor_id}",
        operations=operations,
        auth_requirements=auth_requirements,
        risk_tier=risk_tier,
        data_boundary=data_boundary,
    )


def _gate(report: ReleaseGateReport, name: GateName) -> GateResult:
    return next(result for result in report.results if result.name is name)


def test_run_gates_all_pass_for_custom_hosted_with_evidence() -> None:
    report = run_gates(
        version_id="version-1",
        report_id="report-1",
        manifest=_manifest(),
        capability_catalog=default_registry().as_mapping(),
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )

    assert report.passed
    for name in GateName:
        assert _gate(report, name).status is GateStatus.PASSED


def test_run_gates_marks_build_not_applicable_for_managed_foundry() -> None:
    report = run_gates(
        version_id="version-1",
        report_id="report-1",
        manifest=_manifest(),
        capability_catalog=default_registry().as_mapping(),
        evidence=GateEvidence(tests_passed=True, smoke_passed=True),
        runtime_target=RuntimeTarget.MANAGED_FOUNDRY,
    )

    assert report.passed
    assert _gate(report, GateName.BUILD).status is GateStatus.NOT_APPLICABLE
    assert "No separate build step exists" in _gate(report, GateName.BUILD).detail


def test_schema_gate_revalidates_corrupted_manifest() -> None:
    corrupted = _manifest().model_copy(update={"logical_agent_id": "not valid"})

    result = _schema_gate(corrupted)

    assert result.status is GateStatus.FAILED
    assert "logical_agent_id" in result.detail


def test_build_test_and_smoke_gate_helpers_cover_skip_fail_and_pass_paths() -> None:
    assert _build_gate(GateEvidence(), RuntimeTarget.CUSTOM_HOSTED) == GateResult(
        name=GateName.BUILD,
        status=GateStatus.SKIPPED,
        detail="No build evidence supplied.",
    )
    assert _build_gate(
        GateEvidence(build_succeeded=False, build_detail="compile error"),
        None,
    ) == GateResult(name=GateName.BUILD, status=GateStatus.FAILED, detail="compile error")
    assert _build_gate(GateEvidence(build_succeeded=True), None) == GateResult(
        name=GateName.BUILD,
        status=GateStatus.PASSED,
        detail="Build succeeded.",
    )

    assert _test_gate(GateEvidence()) == GateResult(
        name=GateName.TEST,
        status=GateStatus.SKIPPED,
        detail="No test evidence supplied.",
    )
    assert _test_gate(GateEvidence(tests_passed=False, test_detail="assertion failed")) == GateResult(
        name=GateName.TEST,
        status=GateStatus.FAILED,
        detail="assertion failed",
    )
    assert _test_gate(GateEvidence(tests_passed=True)) == GateResult(
        name=GateName.TEST,
        status=GateStatus.PASSED,
        detail="Tests passed.",
    )

    assert _smoke_gate(GateEvidence()) == GateResult(
        name=GateName.SMOKE,
        status=GateStatus.SKIPPED,
        detail="No smoke-test evidence supplied.",
    )
    assert _smoke_gate(GateEvidence(smoke_passed=False, smoke_detail="endpoint down")) == GateResult(
        name=GateName.SMOKE,
        status=GateStatus.FAILED,
        detail="endpoint down",
    )
    assert _smoke_gate(GateEvidence(smoke_passed=True)) == GateResult(
        name=GateName.SMOKE,
        status=GateStatus.PASSED,
        detail="Smoke test passed.",
    )


def test_auth_gate_fails_for_missing_catalog_and_missing_connection() -> None:
    missing_catalog = _auth_gate(
        _manifest(capabilities=(_binding("missing.capability", "search"),)),
        {},
    )
    registry = default_registry()
    missing_connection = _auth_gate(
        _manifest(capabilities=(_binding("foundry.file_search", "search"),)),
        registry.as_mapping(),
    )

    assert missing_catalog.status is GateStatus.FAILED
    assert "not in the capability catalog" in missing_catalog.detail
    assert missing_connection.status is GateStatus.FAILED
    assert "requires a workspace connection" in missing_connection.detail


def test_auth_gate_passes_for_non_workspace_requirements_and_attached_connection() -> None:
    catalog = {
        "custom.permission_only": _descriptor(
            "custom.permission_only",
            CapabilityOperation(name="search", maturity=OperationMaturity.GA),
            auth_requirements=("role:admin",),
        ),
        "custom.workspace": _descriptor(
            "custom.workspace",
            CapabilityOperation(name="search", maturity=OperationMaturity.GA),
            auth_requirements=("workspace_connection:file_store",),
        ),
    }

    permission_only = _auth_gate(
        _manifest(capabilities=(_binding("custom.permission_only", "search"),)),
        catalog,
    )
    attached_connection = _auth_gate(
        _manifest(capabilities=(_binding("custom.workspace", "search", connection_ref="conn-1"),)),
        catalog,
    )

    assert permission_only.status is GateStatus.PASSED
    assert attached_connection.status is GateStatus.PASSED


def test_policy_gate_rejects_missing_catalog_missing_operation_preview_and_high_risk_visibility() -> None:
    missing_catalog = _policy_gate(
        _manifest(capabilities=(_binding("missing.capability", "search"),)),
        {},
    )
    catalog = {
        "custom.safe": _descriptor(
            "custom.safe",
            CapabilityOperation(name="search", maturity=OperationMaturity.GA),
        ),
        "custom.preview": _descriptor(
            "custom.preview",
            CapabilityOperation(name="run", maturity=OperationMaturity.PREVIEW),
        ),
        "custom.high_risk": _descriptor(
            "custom.high_risk",
            CapabilityOperation(name="search", maturity=OperationMaturity.GA),
            risk_tier="high",
            data_boundary="public",
        ),
    }

    missing_operation = _policy_gate(
        _manifest(capabilities=(_binding("custom.safe", "missing"),)),
        catalog,
    )
    preview_operation = _policy_gate(
        _manifest(capabilities=(_binding("custom.preview", "run"),)),
        catalog,
    )
    high_risk = _policy_gate(
        _manifest(
            visibility=AgentVisibility.PUBLISHED,
            capabilities=(_binding("custom.high_risk", "search"),),
        ),
        catalog,
    )
    safe = _policy_gate(
        _manifest(
            visibility=AgentVisibility.PUBLISHED,
            capabilities=(_binding("custom.safe", "search"),),
        ),
        catalog,
    )

    assert missing_catalog.status is GateStatus.FAILED
    assert "not in the capability catalog" in missing_catalog.detail
    assert missing_operation.status is GateStatus.FAILED
    assert "custom.safe.missing" in missing_operation.detail
    assert preview_operation.status is GateStatus.FAILED
    assert "not a GA operation" in preview_operation.detail
    assert high_risk.status is GateStatus.FAILED
    assert "cannot be released at 'published' visibility" in high_risk.detail
    assert safe.status is GateStatus.PASSED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-----BEGIN RSA PRIVATE KEY-----", True),
        ("sk-abcdefghijklmnop", True),
        ({"nested": ["safe", "xoxb-1234567890abc"]}, True),
        ({"nested": ("still safe", 3)}, False),
        (3, False),
    ],
)
def test_contains_secret_recurses_through_supported_shapes(value: object, expected: bool) -> None:
    assert _contains_secret(value) is expected


def test_security_gate_detects_embedded_secret_and_system_high_risk_public_capability() -> None:
    registry = default_registry()
    embedded_secret = _security_gate(
        _manifest(
            capabilities=(
                _binding(
                    "foundry.azure_functions",
                    "invoke",
                    connection_ref="conn-1",
                    config={"api_key": "sk-abcdefghijklmnop"},
                ),
            ),
        ),
        registry.as_mapping(),
    )
    high_risk_system = _security_gate(
        _manifest(
            owner_kind=AgentOwnerKind.SYSTEM,
            capabilities=(_binding("foundry.browser_automation", "run"),),
        ),
        registry.as_mapping(),
    )

    assert embedded_secret.status is GateStatus.FAILED
    assert "embedded secret" in embedded_secret.detail
    assert high_risk_system.status is GateStatus.FAILED
    assert "system-owned agent attaches high-risk" in high_risk_system.detail


def test_security_gate_passes_for_safe_config_and_missing_descriptor_without_findings() -> None:
    safe = _security_gate(
        _manifest(capabilities=(_binding("foundry.web_search", "search", config={"retry_count": 3}),)),
        default_registry().as_mapping(),
    )
    missing_descriptor = _security_gate(
        _manifest(capabilities=(_binding("missing.capability", "search", config={"retry_count": 3}),)),
        {},
    )

    assert safe.status is GateStatus.PASSED
    assert missing_descriptor.status is GateStatus.PASSED
