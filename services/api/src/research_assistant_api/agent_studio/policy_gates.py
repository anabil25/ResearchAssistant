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
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentVisibility,
    ApprovalKind,
    ApprovalState,
    CapabilityDescriptor,
    GateName,
    GateResult,
    GateStatus,
    ReleaseGateReport,
    RuntimeTarget,
    StudioApprovalRecord,
    utc_now,
)
from research_assistant_api.agent_studio.schema_ref_resolver import (
    InlineSchemaRefResolver,
    SchemaRefResolver,
    SchemaResolutionError,
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


def _schema_gate(manifest: AgentManifest, schema_resolver: SchemaRefResolver) -> GateResult:
    try:
        AgentManifest.model_validate(manifest.model_dump(mode="json"))
    except ValidationError as exc:
        return GateResult(name=GateName.SCHEMA, status=GateStatus.FAILED, detail=str(exc))
    for label, ref in (
        ("input_schema_ref", manifest.input_schema_ref),
        ("output_schema_ref", manifest.output_schema_ref),
    ):
        if ref is None:
            continue
        try:
            schema_resolver.resolve_and_verify(ref)
        except SchemaResolutionError as exc:
            return GateResult(name=GateName.SCHEMA, status=GateStatus.FAILED, detail=f"{label}: {exc}")
    return GateResult(
        name=GateName.SCHEMA,
        status=GateStatus.PASSED,
        detail="Manifest re-validated against schema; input/output schema refs resolved and digest-verified.",
    )


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
        descriptor = capability_catalog.get(instance.descriptor_ref.id)
        if descriptor is None:
            missing.append(f"capability '{instance.descriptor_ref.id}' is not in the capability catalog")
            continue
        for requirement in descriptor.auth_requirements:
            if requirement.startswith("workspace_connection:") and instance.connection_ref is None:
                missing.append(
                    f"capability '{instance.descriptor_ref.id}' requires a workspace connection but none is attached"
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
        descriptor = capability_catalog.get(instance.descriptor_ref.id)
        if descriptor is None:
            violations.append(f"capability '{instance.descriptor_ref.id}' is not in the capability catalog")
            continue
        operation = descriptor.operation(instance.operation_ref.id)
        if operation is None or not operation.is_bindable:
            violations.append(
                f"capability '{instance.descriptor_ref.id}.{instance.operation_ref.id}' is not a GA operation "
                "and cannot be released"
            )
        if (
            manifest.visibility in (AgentVisibility.ORG, AgentVisibility.PUBLISHED)
            and descriptor.risk_tier in _HIGH_RISK_TIERS
        ):
            violations.append(
                f"high-risk capability '{instance.descriptor_ref.id}' cannot be released at "
                f"'{manifest.visibility.value}' visibility"
            )
    if violations:
        return GateResult(name=GateName.POLICY, status=GateStatus.FAILED, detail="; ".join(violations))
    return GateResult(name=GateName.POLICY, status=GateStatus.PASSED, detail="No policy violations found.")


def _approval_gate(
    manifest: AgentManifest,
    capability_catalog: Mapping[str, CapabilityDescriptor],
    manifest_hash: str,
    capability_approvals: tuple[StudioApprovalRecord, ...],
    now: datetime,
) -> GateResult:
    """Hard-block cut/release when a capability operation that
    ``requires_approval`` has no matching, approved, unexpired record.

    ``requires_approval`` is not informational: this gate resolves each
    binding's operation and requires a ``StudioApprovalRecord`` of kind
    ``CAPABILITY_OPERATION`` targeting this exact binding (by
    ``descriptor_id.operation`` destination), in ``APPROVED`` state, bound to
    this exact manifest content hash, and not expired. Missing, mismatched,
    or expired approvals all fail this gate identically — none are silently
    treated as satisfied.
    """
    violations: list[str] = []
    for binding in manifest.capabilities:
        descriptor = capability_catalog.get(binding.descriptor_ref.id)
        if descriptor is None:
            continue  # already reported by the AUTH/POLICY gates
        operation = descriptor.operation(binding.operation_ref.id)
        if operation is None or not operation.requires_approval:
            continue
        destination = f"{binding.descriptor_ref.id}.{binding.operation_ref.id}"
        matching = [
            record
            for record in capability_approvals
            if record.kind == ApprovalKind.CAPABILITY_OPERATION and record.destination == destination
        ]
        approved = next((record for record in matching if record.state == ApprovalState.APPROVED), None)
        if approved is None:
            violations.append(
                f"capability binding '{destination}' requires approval but no approved record was found"
            )
        elif approved.content_hash != manifest_hash:
            violations.append(
                f"capability binding '{destination}' approval is bound to a different manifest content hash"
            )
        elif approved.expires_at is not None and approved.expires_at <= now:
            violations.append(f"capability binding '{destination}' approval has expired")
    if violations:
        return GateResult(name=GateName.APPROVAL, status=GateStatus.FAILED, detail="; ".join(violations))
    return GateResult(
        name=GateName.APPROVAL, status=GateStatus.PASSED, detail="All required capability approvals are satisfied."
    )


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
            findings.append(f"capability '{instance.descriptor_ref.id}' config appears to contain an embedded secret")
        descriptor = capability_catalog.get(instance.descriptor_ref.id)
        if (
            descriptor is not None
            and manifest.owner_kind.value == "system"
            and descriptor.data_boundary == "public"
            and descriptor.risk_tier in _HIGH_RISK_TIERS
        ):
            findings.append(
                f"system-owned agent attaches high-risk public-data-boundary capability "
                f"'{instance.descriptor_ref.id}'"
            )
    if findings:
        return GateResult(name=GateName.SECURITY, status=GateStatus.FAILED, detail="; ".join(findings))
    return GateResult(name=GateName.SECURITY, status=GateStatus.PASSED, detail="No security findings.")


def run_gates(
    *,
    version_id: str,
    report_id: str,
    manifest: AgentManifest,
    manifest_hash: str,
    capability_catalog: Mapping[str, CapabilityDescriptor],
    evidence: GateEvidence,
    runtime_target: RuntimeTarget | None = None,
    capability_approvals: tuple[StudioApprovalRecord, ...] = (),
    schema_resolver: SchemaRefResolver | None = None,
    now: datetime | None = None,
) -> ReleaseGateReport:
    """Run all eight hard gates deterministically and assemble a report.

    ``runtime_target`` makes the BUILD gate runtime-aware: a Managed Foundry
    agent has no separate build step, so BUILD is deterministically
    ``NOT_APPLICABLE`` rather than requiring synthetic build evidence. All
    other gates (including SMOKE, which still hard-blocks activation on
    deployment smoke failure) apply uniformly regardless of runtime.
    ``manifest_hash`` and ``capability_approvals`` feed the APPROVAL gate
    (missing/expired/mismatched capability-operation approvals hard-block);
    ``schema_resolver`` feeds the SCHEMA gate's independent digest
    verification of ``input_schema_ref``/``output_schema_ref``.
    """
    resolver = schema_resolver if schema_resolver is not None else InlineSchemaRefResolver()
    effective_now = now if now is not None else utc_now()
    results = (
        _schema_gate(manifest, resolver),
        _build_gate(evidence, runtime_target),
        _test_gate(evidence),
        _auth_gate(manifest, capability_catalog),
        _policy_gate(manifest, capability_catalog),
        _approval_gate(manifest, capability_catalog, manifest_hash, capability_approvals, effective_now),
        _security_gate(manifest, capability_catalog),
        _smoke_gate(evidence),
    )
    return ReleaseGateReport(id=report_id, version_id=version_id, results=results)
