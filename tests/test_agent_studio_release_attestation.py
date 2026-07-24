# mypy: disable-error-code=import-untyped

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import (
    HARNESS_RELEASE_LINK_SCHEMA_VERSION,
    AgentRelease,
    DeploymentEnvironment,
    GateName,
    GateResult,
    GateStatus,
    ReleaseGateReport,
    ReleaseStatus,
)
from research_assistant_api.agent_studio.release_attestation import (
    ReleaseAttestationError,
    ReleaseAttestationOutcome,
    ReleaseAttestationRequest,
    ReleaseAttestationResult,
    StoreBackedReleaseAttestationPort,
    build_release_attestation,
    verify_release_attestation,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

TENANT = "tenant-1"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
OTHER_SCOPE = ScopeContext(tenant_id=TENANT, project_id=OTHER_PROJECT)
AGENT_ID = "agent-attestation-1"
USER_ID = "user-1"


def _passing_results() -> tuple[GateResult, ...]:
    return tuple(
        GateResult(name=name, status=GateStatus.PASSED)
        for name in (
            GateName.SCHEMA,
            GateName.BUILD,
            GateName.TEST,
            GateName.AUTH,
            GateName.POLICY,
            GateName.APPROVAL,
            GateName.SECURITY,
            GateName.SMOKE,
            GateName.BINDING,
        )
    )


def _failing_results() -> tuple[GateResult, ...]:
    return (
        GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED),
        GateResult(name=GateName.TEST, status=GateStatus.FAILED, detail="2 tests failed"),
        GateResult(name=GateName.SECURITY, status=GateStatus.SKIPPED, detail="no scan evidence"),
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    gate_report_id: str | None = "report-1",
    status: ReleaseStatus = ReleaseStatus.GATED,
    harness_release_id: str | None = None,
    harness_manifest_digest: str | None = None,
    harness_link_schema_version: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=AGENT_ID,
        tenant_id=tenant_id,
        project_id=project_id,
        status=status,
        environment=DeploymentEnvironment.DEVELOPMENT,
        manifest_hash="sha256:" + "a" * 64,
        gate_report_id=gate_report_id,
        created_by=USER_ID,
        harness_release_id=harness_release_id,
        harness_manifest_digest=harness_manifest_digest,
        harness_link_schema_version=harness_link_schema_version,
    )


def _gate_report(
    *,
    report_id: str = "report-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    results: tuple[GateResult, ...] | None = None,
) -> ReleaseGateReport:
    return ReleaseGateReport(
        id=report_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        results=results if results is not None else _passing_results(),
    )


# --- build_release_attestation --------------------------------------------


def test_build_attestation_reports_attested_status_and_no_blocking_gates_when_all_pass() -> None:
    release = _release()
    report = _gate_report()

    attestation = build_release_attestation(release=release, gate_report=report, signing_key=None)

    assert attestation.status.value == "attested"
    assert attestation.blocking_gates == ()
    assert attestation.release_id == release.id
    assert attestation.version_id == release.version_id
    assert attestation.gate_report_id == report.id
    assert attestation.manifest_hash == release.manifest_hash


def test_build_attestation_reports_failed_status_and_blocking_gates_when_any_gate_fails() -> None:
    release = _release()
    report = _gate_report(results=_failing_results())

    attestation = build_release_attestation(release=release, gate_report=report, signing_key=None)

    assert attestation.status.value == "failed"
    assert GateName.TEST in attestation.blocking_gates
    assert GateName.SECURITY in attestation.blocking_gates
    assert GateName.SCHEMA not in attestation.blocking_gates


def test_build_attestation_raises_when_gate_report_version_mismatches_release() -> None:
    release = _release(version_id="version-1")
    report = _gate_report(version_id="version-2")

    with pytest.raises(ReleaseAttestationError):
        build_release_attestation(release=release, gate_report=report, signing_key=None)


def test_build_attestation_raises_when_gate_report_tenant_mismatches_release() -> None:
    release = _release(tenant_id="tenant-1")
    report = _gate_report(tenant_id="tenant-other")

    with pytest.raises(ReleaseAttestationError):
        build_release_attestation(release=release, gate_report=report, signing_key=None)


def test_build_attestation_raises_when_gate_report_project_mismatches_release() -> None:
    release = _release(project_id="project-1")
    report = _gate_report(project_id="project-other")

    with pytest.raises(ReleaseAttestationError):
        build_release_attestation(release=release, gate_report=report, signing_key=None)


def test_unsigned_attestation_reports_digest_algorithm_and_prefixed_signature() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert attestation.signature_algorithm == "sha256-digest"
    assert attestation.signature.startswith("attestation:v1:sha256-digest:")


def test_signed_attestation_reports_hmac_algorithm_and_prefixed_signature() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="shared-secret", key_version="v1"
    )

    assert attestation.signature_algorithm == "hmac-sha256"
    assert attestation.signature.startswith("attestation:v1:hmac-sha256:")


def test_signed_and_unsigned_signatures_for_identical_content_differ() -> None:
    signed = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret", key_version="v1"
    )
    unsigned = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert signed.signature != unsigned.signature


def test_build_attestation_raises_when_signing_key_configured_without_key_version() -> None:
    with pytest.raises(ReleaseAttestationError, match="key_version"):
        build_release_attestation(
            release=_release(), gate_report=_gate_report(), signing_key="secret", key_version=None
        )


def test_build_attestation_ignores_key_version_when_no_signing_key_given() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key=None, key_version="v1"
    )

    assert attestation.key_version is None
    assert attestation.signature_algorithm == "sha256-digest"


def test_signed_attestation_embeds_configured_key_version() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret", key_version="v2"
    )

    assert attestation.key_version == "v2"


def test_unsigned_attestation_has_no_key_version() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert attestation.key_version is None


# --- verify_release_attestation --------------------------------------------


def test_verify_roundtrip_succeeds_for_unsigned_attestation() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert verify_release_attestation(attestation, signing_key=None) is True


def test_verify_roundtrip_succeeds_for_signed_attestation_with_correct_key() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="correct-key", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_key="correct-key") is True


def test_verify_fails_for_signed_attestation_with_wrong_key() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="correct-key", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_key="wrong-key") is False


def test_verify_fails_when_signing_key_omitted_at_verification_but_present_at_signing() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="correct-key", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_key=None) is False


def test_verify_fails_when_attestation_content_is_tampered() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)
    tampered = attestation.model_copy(update={"manifest_hash": "sha256:" + "f" * 64})

    assert verify_release_attestation(tampered, signing_key=None) is False


def test_verify_fails_when_claimed_key_version_is_tampered() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="correct-key", key_version="v1"
    )
    tampered = attestation.model_copy(update={"key_version": "v2"})

    assert verify_release_attestation(tampered, signing_key="correct-key") is False


# --- verify_release_attestation with a signing_keys map (rotation) ---------


def test_verify_via_signing_keys_map_succeeds_by_looking_up_embedded_key_version() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret-v1", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_keys={"v1": "secret-v1", "v2": "secret-v2"}) is True


def test_verify_via_signing_keys_map_fails_when_embedded_version_unknown_to_map() -> None:
    attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret-v1", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_keys={"v2": "secret-v2"}) is False


def test_verify_via_signing_keys_map_fails_for_unsigned_digest_attestation() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert verify_release_attestation(attestation, signing_keys={"v1": "secret-v1"}) is False


def test_verify_via_signing_keys_map_supports_rotation_across_two_versions() -> None:
    """An attestation signed under a retired key version remains verifiable
    as long as the verifier still retains that specific version's secret --
    the core "rotation" guarantee: signing moves to a new active version,
    but older attestations do not become unverifiable."""

    old_attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret-v1", key_version="v1"
    )
    new_attestation = build_release_attestation(
        release=_release(), gate_report=_gate_report(), signing_key="secret-v2", key_version="v2"
    )
    known_keys = {"v1": "secret-v1", "v2": "secret-v2"}

    assert verify_release_attestation(old_attestation, signing_keys=known_keys) is True
    assert verify_release_attestation(new_attestation, signing_keys=known_keys) is True
    # Cross-checking against the wrong version's secret must fail.
    assert verify_release_attestation(old_attestation, signing_keys={"v1": "secret-v2"}) is False


# --- model contract ---------------------------------------------------------


def test_request_is_frozen_and_rejects_unknown_fields() -> None:
    request = ReleaseAttestationRequest(scope=SCOPE, release_id="release-1")
    with pytest.raises(ValidationError):
        ReleaseAttestationRequest(**{**request.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        request.release_id = "other"


def test_result_is_frozen_and_rejects_unknown_fields() -> None:
    result = ReleaseAttestationResult(outcome=ReleaseAttestationOutcome.NOT_FOUND, reason="no")
    with pytest.raises(ValidationError):
        ReleaseAttestationResult(**{**result.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        result.outcome = ReleaseAttestationOutcome.ATTESTED


def test_attestation_model_is_frozen_and_rejects_unknown_fields() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)
    with pytest.raises(ValidationError):
        type(attestation).model_validate({**attestation.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        attestation.status = attestation.status


# --- StoreBackedReleaseAttestationPort --------------------------------------


@pytest.mark.asyncio
async def test_port_returns_not_found_when_release_missing() -> None:
    store = AgentStudioStore()
    port = StoreBackedReleaseAttestationPort(store)

    result = await port.get_attestation(ReleaseAttestationRequest(scope=SCOPE, release_id="missing"))

    assert result.outcome is ReleaseAttestationOutcome.NOT_FOUND
    assert result.attestation is None
    assert "was not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_port_returns_not_found_when_release_never_gated() -> None:
    store = AgentStudioStore()
    store.create_release(SCOPE, _release(gate_report_id=None, status=ReleaseStatus.GATED))
    port = StoreBackedReleaseAttestationPort(store)

    result = await port.get_attestation(ReleaseAttestationRequest(scope=SCOPE, release_id="release-1"))

    assert result.outcome is ReleaseAttestationOutcome.NOT_FOUND
    assert "never had release gates run" in (result.reason or "")


@pytest.mark.asyncio
async def test_port_returns_not_found_when_gate_report_referenced_but_missing() -> None:
    store = AgentStudioStore()
    store.create_release(SCOPE, _release(gate_report_id="missing-report"))
    port = StoreBackedReleaseAttestationPort(store)

    result = await port.get_attestation(ReleaseAttestationRequest(scope=SCOPE, release_id="release-1"))

    assert result.outcome is ReleaseAttestationOutcome.NOT_FOUND
    assert "Gate report 'missing-report'" in (result.reason or "")


@pytest.mark.asyncio
async def test_port_returns_attested_result_with_populated_attestation() -> None:
    store = AgentStudioStore()
    store.save_gate_report(SCOPE, _gate_report())
    store.create_release(SCOPE, _release())
    port = StoreBackedReleaseAttestationPort(store)

    result = await port.get_attestation(ReleaseAttestationRequest(scope=SCOPE, release_id="release-1"))

    assert result.outcome is ReleaseAttestationOutcome.ATTESTED
    assert result.attestation is not None
    assert result.attestation.release_id == "release-1"
    assert result.attestation.status.value == "attested"


@pytest.mark.asyncio
async def test_port_uses_configured_signing_key() -> None:
    store = AgentStudioStore()
    store.save_gate_report(SCOPE, _gate_report())
    store.create_release(SCOPE, _release())
    port = StoreBackedReleaseAttestationPort(store, signing_key="operator-key", key_version="v1")

    result = await port.get_attestation(ReleaseAttestationRequest(scope=SCOPE, release_id="release-1"))

    assert result.attestation is not None
    assert result.attestation.signature_algorithm == "hmac-sha256"
    assert result.attestation.key_version == "v1"
    assert verify_release_attestation(result.attestation, signing_key="operator-key") is True


def test_port_construction_raises_when_signing_key_configured_without_key_version() -> None:
    store = AgentStudioStore()

    with pytest.raises(ReleaseAttestationError, match="key_version"):
        StoreBackedReleaseAttestationPort(store, signing_key="operator-key", key_version=None)


@pytest.mark.asyncio
async def test_port_scopes_lookup_to_project_and_does_not_leak_cross_project() -> None:
    store = AgentStudioStore()
    store.save_gate_report(SCOPE, _gate_report())
    store.create_release(SCOPE, _release())
    port = StoreBackedReleaseAttestationPort(store)

    result = await port.get_attestation(ReleaseAttestationRequest(scope=OTHER_SCOPE, release_id="release-1"))

    assert result.outcome is ReleaseAttestationOutcome.NOT_FOUND


def test_attested_at_defaults_to_recent_utc_timestamp() -> None:
    before = datetime.now(UTC)
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)
    after = datetime.now(UTC)

    assert before <= attestation.attested_at <= after


# --- harness release linkage (harness blocker #1: signed release linkage) --


def test_attestation_has_no_harness_linkage_when_release_has_none() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert attestation.harness_release_id is None
    assert attestation.harness_manifest_digest is None
    assert attestation.harness_link_schema_version is None


def test_attestation_carries_harness_linkage_fields_through_from_release() -> None:
    release = _release(
        harness_release_id="harness-release-abc",
        harness_manifest_digest="sha256:" + "b" * 64,
        harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION,
    )

    attestation = build_release_attestation(release=release, gate_report=_gate_report(), signing_key=None)

    assert attestation.harness_release_id == "harness-release-abc"
    assert attestation.harness_manifest_digest == "sha256:" + "b" * 64
    assert attestation.harness_link_schema_version == HARNESS_RELEASE_LINK_SCHEMA_VERSION
    # Never asserted equal to this package's own release id/manifest hash.
    assert attestation.harness_release_id != attestation.release_id
    assert attestation.harness_manifest_digest != attestation.manifest_hash


def test_attestation_signature_differs_when_harness_linkage_differs() -> None:
    """The signature must cover the harness-linkage fields, not just the
    backend-local ones -- otherwise an attacker could swap which harness
    release an attestation is linked to without invalidating the signature."""

    with_linkage = build_release_attestation(
        release=_release(
            harness_release_id="harness-release-1",
            harness_manifest_digest="sha256:" + "b" * 64,
            harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION,
        ),
        gate_report=_gate_report(),
        signing_key=None,
    )
    without_linkage = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)

    assert with_linkage.signature != without_linkage.signature


def test_verify_roundtrip_succeeds_when_harness_linkage_present() -> None:
    release = _release(
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:" + "b" * 64,
        harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION,
    )
    attestation = build_release_attestation(
        release=release, gate_report=_gate_report(), signing_key="secret", key_version="v1"
    )

    assert verify_release_attestation(attestation, signing_key="secret") is True


def test_verify_fails_when_harness_release_id_is_tampered() -> None:
    release = _release(
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:" + "b" * 64,
        harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION,
    )
    attestation = build_release_attestation(release=release, gate_report=_gate_report(), signing_key=None)
    tampered = attestation.model_copy(update={"harness_release_id": "harness-release-attacker"})

    assert verify_release_attestation(tampered, signing_key=None) is False


def test_agent_release_rejects_only_harness_release_id_set() -> None:
    with pytest.raises(ValidationError):
        _release(harness_release_id="harness-1")


def test_agent_release_rejects_only_harness_manifest_digest_set() -> None:
    with pytest.raises(ValidationError):
        _release(harness_manifest_digest="sha256:" + "b" * 64)


def test_agent_release_rejects_only_harness_link_schema_version_set() -> None:
    with pytest.raises(ValidationError):
        _release(harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION)


def test_agent_release_rejects_unsanctioned_harness_link_schema_version() -> None:
    with pytest.raises(ValidationError, match="harness_link_schema_version"):
        _release(
            harness_release_id="harness-1",
            harness_manifest_digest="sha256:" + "b" * 64,
            harness_link_schema_version="harness-release-link:v2",
        )


def test_agent_release_rejects_empty_harness_release_id_when_schema_version_set() -> None:
    with pytest.raises(ValidationError):
        _release(
            harness_release_id="",
            harness_manifest_digest="sha256:" + "b" * 64,
            harness_link_schema_version=HARNESS_RELEASE_LINK_SCHEMA_VERSION,
        )


def test_release_attestation_rejects_only_harness_release_id_set() -> None:
    attestation = build_release_attestation(release=_release(), gate_report=_gate_report(), signing_key=None)
    with pytest.raises(ValidationError):
        type(attestation).model_validate({**attestation.model_dump(), "harness_release_id": "harness-1"})
