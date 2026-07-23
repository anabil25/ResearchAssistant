"""Hard deterministic release gates.

Each gate is a pure function over the manifest and externally-supplied
evidence. Gates never consult a model and never accept an "override" input:
a caller cannot pass a flag that skips or force-passes a gate. Build/test/
smoke gates require *evidence* supplied by the actual build/test/smoke
systems (``GateEvidence``); when evidence is missing the gate is
``SKIPPED`` which ``ReleaseGateReport.passed`` treats as non-passing (see
``models.py``), so a hard gate can never be silently bypassed by omission.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, ValidationError

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentVisibility,
    CapabilityDescriptor,
    GateName,
    GateResult,
    GateStatus,
    OperationMaturity,
    ReleaseGateReport,
    RuntimeTarget,
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)

_HIGH_RISK_TIERS = frozenset({"high"})
_BROAD_DATA_BOUNDARIES = frozenset({"tenant", "public"})


class GateEvidence(BaseModel):
    """Facts supplied by the actual build/test/smoke systems.

    ``None`` means "not yet run" and results in ``GateStatus.SKIPPED``
    (non-passing), never in an assumed pass.
    """

    model_config = ConfigDict(extra="forbid")

    build_succeeded: bool | None = None
    build_detail: str = ""
    tests_passed: bool | None = None
    test_detail: str = ""
    smoke_passed: bool | None = None
    smoke_detail: str = ""


def _schema_gate(manifest: AgentManifest) -> GateResult:
    try:
        AgentManifest.model_validate(manifest.model_dump(mode="json"))
    except ValidationError as exc:
        return GateResult(name=GateName.SCHEMA, status=GateStatus.FAILED, detail=str(exc))
    return GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED, detail="Manifest re-validated against schema.")


def _build_gate(evidence: GateEvidence, runtime_target: RuntimeTarget | None) -> GateResult:
    if runtime_target == RuntimeTarget.MANAGED_FOUNDRY:
        return GateResult(
            name=GateName.BUILD,
            status=GateStatus.NOT_APPLICABLE,
            detail="No separate build step exists for Managed Foundry agents.",
        )
    if evidence.build_succeeded is None:
        return GateResult(name=GateName.BUILD, status=GateStatus.SKIPPED, detail="No build evidence supplied.")
    if not evidence.build_succeeded:
        return GateResult(
            name=GateName.BUILD, status=GateStatus.FAILED, detail=evidence.build_detail or "Build failed."
        )
    return GateResult(
        name=GateName.BUILD, status=GateStatus.PASSED, detail=evidence.build_detail or "Build succeeded."
    )


def _test_gate(evidence: GateEvidence) -> GateResult:
    if evidence.tests_passed is None:
        return GateResult(name=GateName.TEST, status=GateStatus.SKIPPED, detail="No test evidence supplied.")
    if not evidence.tests_passed:
        return GateResult(name=GateName.TEST, status=GateStatus.FAILED, detail=evidence.test_detail or "Tests failed.")
    return GateResult(name=GateName.TEST, status=GateStatus.PASSED, detail=evidence.test_detail or "Tests passed.")


def _smoke_gate(evidence: GateEvidence) -> GateResult:
    if evidence.smoke_passed is None:
        return GateResult(name=GateName.SMOKE, status=GateStatus.SKIPPED, detail="No smoke-test evidence supplied.")
    if not evidence.smoke_passed:
        return GateResult(
            name=GateName.SMOKE, status=GateStatus.FAILED, detail=evidence.smoke_detail or "Smoke test failed."
        )
    return GateResult(
        name=GateName.SMOKE, status=GateStatus.PASSED, detail=evidence.smoke_detail or "Smoke test passed."
    )


def _auth_gate(
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
) -> GateResult:
    missing: list[str] = []
    for instance in manifest.capabilities:
        descriptor = capability_catalog.get(instance.descriptor_id)
        if descriptor is None:
            missing.append(f"capability '{instance.descriptor_id}' is not in the capability catalog")
            continue
        for requirement in descriptor.auth_requirements:
            if requirement.startswith("workspace_connection:") and instance.connection_ref is None:
                missing.append(
                    f"capability '{instance.descriptor_id}' requires a workspace connection but none is attached"
                )
    if missing:
        return GateResult(name=GateName.AUTH, status=GateStatus.FAILED, detail="; ".join(missing))
    return GateResult(
        name=GateName.AUTH, status=GateStatus.PASSED, detail="All capability auth requirements satisfied."
    )


def _policy_gate(
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
) -> GateResult:
    violations: list[str] = []
    for instance in manifest.capabilities:
        descriptor = capability_catalog.get(instance.descriptor_id)
        if descriptor is None:
            violations.append(f"capability '{instance.descriptor_id}' is not in the capability catalog")
            continue
        operation = descriptor.operation(instance.operation)
        if operation is None or operation.maturity != OperationMaturity.GA:
            violations.append(
                f"capability '{instance.descriptor_id}.{instance.operation}' is not a GA operation "
                "and cannot be released"
            )
        if (
            manifest.visibility in (AgentVisibility.ORG, AgentVisibility.PUBLISHED)
            and descriptor.risk_tier in _HIGH_RISK_TIERS
        ):
            violations.append(
                f"high-risk capability '{instance.descriptor_id}' cannot be released at "
                f"'{manifest.visibility.value}' visibility"
            )
    if violations:
        return GateResult(name=GateName.POLICY, status=GateStatus.FAILED, detail="; ".join(violations))
    return GateResult(name=GateName.POLICY, status=GateStatus.PASSED, detail="No policy violations found.")


def _contains_secret(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(_contains_secret(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(v) for v in value)
    return False


def _security_gate(
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
) -> GateResult:
    findings: list[str] = []
    for instance in manifest.capabilities:
        if _contains_secret(instance.config):
            findings.append(f"capability '{instance.descriptor_id}' config appears to contain an embedded secret")
        descriptor = capability_catalog.get(instance.descriptor_id)
        if (
            descriptor is not None
            and manifest.owner_kind.value == "system"
            and descriptor.data_boundary == "public"
            and descriptor.risk_tier in _HIGH_RISK_TIERS
        ):
            findings.append(
                f"system-owned agent attaches high-risk public-data-boundary capability '{instance.descriptor_id}'"
            )
    if findings:
        return GateResult(name=GateName.SECURITY, status=GateStatus.FAILED, detail="; ".join(findings))
    return GateResult(name=GateName.SECURITY, status=GateStatus.PASSED, detail="No security findings.")


def run_gates(
    *,
    version_id: str,
    report_id: str,
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
    evidence: GateEvidence,
    runtime_target: RuntimeTarget | None = None,
) -> ReleaseGateReport:
    """Run all seven hard gates deterministically and assemble a report.

    ``runtime_target`` makes the BUILD gate runtime-aware: a Managed Foundry
    agent has no separate build step, so BUILD is deterministically
    ``NOT_APPLICABLE`` rather than requiring synthetic build evidence. All
    other gates (including SMOKE, which still hard-blocks activation on
    deployment smoke failure) apply uniformly regardless of runtime.
    """
    results = (
        _schema_gate(manifest),
        _build_gate(evidence, runtime_target),
        _test_gate(evidence),
        _auth_gate(manifest, capability_catalog),
        _policy_gate(manifest, capability_catalog),
        _security_gate(manifest, capability_catalog),
        _smoke_gate(evidence),
    )
    return ReleaseGateReport(id=report_id, version_id=version_id, results=results)
