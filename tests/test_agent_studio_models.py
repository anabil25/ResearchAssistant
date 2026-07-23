from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    CapabilityDescriptor,
    CapabilityOperation,
    GateName,
    GateResult,
    GateStatus,
    MemoryMechanism,
    OperationMaturity,
    ReleaseGateReport,
    RuntimeRequirements,
    role_at_least,
)


def test_role_at_least_ordering() -> None:
    assert role_at_least(AgentRole.OWNER, AgentRole.VIEWER)
    assert role_at_least(AgentRole.MAINTAINER, AgentRole.MAINTAINER)
    assert not role_at_least(AgentRole.CONTRIBUTOR, AgentRole.MAINTAINER)
    assert not role_at_least(AgentRole.VIEWER, AgentRole.OWNER)


def test_memory_mechanism_is_ga() -> None:
    assert MemoryMechanism.APPLICATION_THREAD.is_ga
    assert MemoryMechanism.APPLICATION_MEMORY_STORE.is_ga
    assert not MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE.is_ga


def test_capability_descriptor_operation_lookup() -> None:
    descriptor = CapabilityDescriptor(
        id="foundry.test",
        provider="microsoft_foundry",
        title="Test",
        description="A test capability.",
        operations=(
            CapabilityOperation(name="search", maturity=OperationMaturity.GA),
            CapabilityOperation(name="preview_op", maturity=OperationMaturity.PREVIEW, reason="preview"),
        ),
    )
    assert descriptor.operation("search") is not None
    op = descriptor.operation("search")
    assert op is not None
    assert op.maturity == OperationMaturity.GA
    assert descriptor.operation("missing") is None


def test_agent_manifest_requires_valid_logical_agent_id() -> None:
    with pytest.raises(ValidationError):
        AgentManifest(
            logical_agent_id="not-valid",
            tenant_id="demo",
            display_name="Agent",
            owner_kind=AgentOwnerKind.USER,
            owner_id="user-1",
        )


def test_agent_manifest_accepts_valid_logical_agent_id() -> None:
    manifest = AgentManifest(
        logical_agent_id="agent-valid-123",
        tenant_id="demo",
        display_name="Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
    )
    assert manifest.logical_agent_id == "agent-valid-123"
    assert manifest.runtime_requirements == RuntimeRequirements()


def test_release_gate_report_passed_requires_all_gates_passed() -> None:
    passing = ReleaseGateReport(
        id="report-1",
        version_id="version-1",
        results=(
            GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED),
            GateResult(name=GateName.BUILD, status=GateStatus.PASSED),
        ),
    )
    assert passing.passed
    assert passing.blocking_gates() == ()


def test_release_gate_report_skipped_gate_is_not_passing() -> None:
    report = ReleaseGateReport(
        id="report-2",
        version_id="version-1",
        results=(
            GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED),
            GateResult(name=GateName.BUILD, status=GateStatus.SKIPPED, detail="No build evidence supplied."),
        ),
    )
    assert not report.passed
    blocking = report.blocking_gates()
    assert len(blocking) == 1
    assert blocking[0].name == GateName.BUILD


def test_release_gate_report_failed_gate_is_not_passing() -> None:
    report = ReleaseGateReport(
        id="report-3",
        version_id="version-1",
        results=(GateResult(name=GateName.SECURITY, status=GateStatus.FAILED, detail="secret found"),),
    )
    assert not report.passed
    assert len(report.blocking_gates()) == 1
