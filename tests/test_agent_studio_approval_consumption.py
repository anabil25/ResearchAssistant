# mypy: disable-error-code=import-untyped

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.approval_consumption import (
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    StoreBackedApprovalConsumptionPort,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentVersion,
    ApprovalConsumptionOutcome,
    ApprovalKind,
    ApprovalRevocation,
    ApprovalState,
    CapabilityBinding,
    CapabilityConnectionRef,
    CapabilityDescriptorRef,
    CapabilityInstanceRef,
    CapabilityOperationRef,
    CapabilityPolicyRef,
    DeploymentEnvironment,
    ReleaseStatus,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.scope import ScopeContext, compute_destination_hash
from research_assistant_api.agent_studio.store import AgentStudioStore

TENANT = "tenant-1"
PROJECT = "project-1"
SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
AGENT_ID = "agent-consumption-1"
USER_ID = "user-1"


def _binding(
    *,
    binding_id: str = "binding-1",
    descriptor_id: str = "descriptor-1",
    operation_id: str = "search",
    instance_fingerprint: str | None = "fingerprint-1",
    policy_id: str | None = "policy-1",
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=binding_id,
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id=descriptor_id),
        operation_ref=CapabilityOperationRef(id=operation_id),
        instance_ref=(
            CapabilityInstanceRef(provider_id="provider-1", id="instance-1", fingerprint=instance_fingerprint)
            if instance_fingerprint is not None
            else None
        ),
        connection_ref=CapabilityConnectionRef(id="connection-1"),
        policy_ref=(CapabilityPolicyRef(id=policy_id) if policy_id is not None else None),
        attached_by=USER_ID,
    )


def _manifest(*, binding: CapabilityBinding | None = ...) -> AgentManifest:  # type: ignore[assignment]
    resolved_binding = _binding() if binding is ... else binding
    return AgentManifest(
        logical_agent_id=AGENT_ID,
        tenant_id=TENANT,
        project_id=PROJECT,
        display_name="Consumption Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id=USER_ID,
        capabilities=(resolved_binding,) if resolved_binding is not None else (),
    )


def _version(
    *,
    version_id: str = "version-1",
    manifest: AgentManifest | None = None,
) -> AgentVersion:
    return AgentVersion(
        id=version_id,
        logical_agent_id=AGENT_ID,
        tenant_id=TENANT,
        project_id=PROJECT,
        sequence=1,
        manifest=manifest or _manifest(),
        manifest_hash="sha256:" + "a" * 64,
        created_by=USER_ID,
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=AGENT_ID,
        tenant_id=TENANT,
        project_id=PROJECT,
        status=ReleaseStatus.GATED,
        environment=DeploymentEnvironment.DEVELOPMENT,
        manifest_hash="sha256:" + "a" * 64,
        created_by=USER_ID,
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    state: ApprovalState = ApprovalState.APPROVED,
    destination: str = "descriptor-1.search",
    expires_at: datetime | None = None,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=TENANT,
        project_id=PROJECT,
        kind=ApprovalKind.CAPABILITY_OPERATION,
        state=state,
        gated_action="invoke_capability_operation",
        destination=destination,
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=f"approval-key-{approval_id}",
        approver_id=USER_ID if state is ApprovalState.APPROVED else None,
        decided_at=datetime.now(UTC) if state is not ApprovalState.PENDING else None,
        expires_at=expires_at,
    )


def _request(
    *,
    approval_id: str = "approval-1",
    binding_id: str = "binding-1",
    operation_id: str = "search",
    instance_fingerprint: str | None = "fingerprint-1",
    policy_ref: str | None = "policy-1",
    release_id: str | None = None,
    invocation_id: str = "invocation-1",
    idempotency_key: str = "invocation-key-1",
    destination_hash: str | None = None,
) -> ApprovalConsumptionRequest:
    resolved_destination_hash = (
        destination_hash
        if destination_hash is not None
        else compute_destination_hash(
            tenant_id=TENANT,
            project_id=PROJECT,
            release_id=release_id,
            binding_id=binding_id,
            operation_id=operation_id,
            instance_fingerprint=instance_fingerprint,
            policy_ref=policy_ref,
        )
    )
    return ApprovalConsumptionRequest(
        scope=SCOPE,
        approval_id=approval_id,
        principal_id=USER_ID,
        binding_id=binding_id,
        instance_fingerprint=instance_fingerprint,
        operation_id=operation_id,
        operation_version=None,
        args_hash="args-hash-1",
        destination_hash=resolved_destination_hash,
        policy_ref=policy_ref,
        release_id=release_id,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
    )


def _seed(store: AgentStudioStore, **approval_overrides: object) -> None:
    store.create_version(SCOPE, _version())
    store.create_approval(SCOPE, _approval(**approval_overrides))  # type: ignore[arg-type]


# --- model contract -----------------------------------------------------


def test_request_is_frozen_and_rejects_unknown_fields() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        ApprovalConsumptionRequest(**{**request.model_dump(), "unexpected": "value"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        request.approval_id = "other"  # type: ignore[misc]


def test_result_is_frozen_and_rejects_unknown_fields() -> None:
    result = ApprovalConsumptionResult(outcome=ApprovalConsumptionOutcome.DENIED, reason="no")
    with pytest.raises(ValidationError):
        ApprovalConsumptionResult(**{**result.model_dump(), "unexpected": "value"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        result.outcome = ApprovalConsumptionOutcome.CONSUMED  # type: ignore[misc]


# --- denial paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_when_approval_not_found() -> None:
    store = AgentStudioStore()
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "was not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_approval_kind_is_not_capability_operation() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    wrong_kind = _approval().model_copy(update={"kind": ApprovalKind.RELEASE_PROMOTION})
    store.create_approval(SCOPE, wrong_kind)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "not a capability-operation approval" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_approval_still_pending() -> None:
    store = AgentStudioStore()
    _seed(store, state=ApprovalState.PENDING)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "pending" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_approval_rejected() -> None:
    store = AgentStudioStore()
    _seed(store, state=ApprovalState.REJECTED)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "rejected" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_approval_expired() -> None:
    store = AgentStudioStore()
    _seed(store, expires_at=datetime.now(UTC) - timedelta(hours=1))
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "expired" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_approval_revoked() -> None:
    store = AgentStudioStore()
    _seed(store)
    store.create_revocation(
        SCOPE,
        ApprovalRevocation(
            id="revocation-1",
            approval_id="approval-1",
            tenant_id=TENANT,
            project_id=PROJECT,
            actor_id=USER_ID,
            reason="no longer needed",
            idempotency_key="rev-key-1",
        ),
    )
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "revoked" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_pinned_version_not_found() -> None:
    store = AgentStudioStore()
    # No version created at all -- approval references a version that
    # does not exist in this store.
    store.create_approval(SCOPE, _approval())
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "was not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_binding_not_present_on_version() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version(manifest=_manifest(binding=None)))
    store.create_approval(SCOPE, _approval())
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "is not present on version" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_binding_destination_does_not_match_approval() -> None:
    store = AgentStudioStore()
    _seed(store, destination="descriptor-1.other-operation")
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "resolves to destination" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_operation_id_does_not_match_binding() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(operation_id="other-operation"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "is bound to operation" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_instance_fingerprint_mismatches() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(instance_fingerprint="stale-fingerprint"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "instance fingerprint does not match" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_request_supplies_fingerprint_but_binding_has_none() -> None:
    store = AgentStudioStore()
    store.create_version(
        SCOPE,
        _version(manifest=_manifest(binding=_binding(instance_fingerprint=None))),
    )
    store.create_approval(SCOPE, _approval())
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(instance_fingerprint="fingerprint-1"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "instance fingerprint does not match" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_policy_ref_mismatches() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(policy_ref="different-policy"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "policy reference does not match" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_request_supplies_policy_ref_but_binding_has_none() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version(manifest=_manifest(binding=_binding(policy_id=None))))
    store.create_approval(SCOPE, _approval())
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(policy_ref="policy-1"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "policy reference does not match" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_release_not_found() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(release_id="missing-release"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "Release 'missing-release' was not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_release_pins_a_different_version() -> None:
    store = AgentStudioStore()
    _seed(store)
    store.create_version(SCOPE, _version(version_id="version-2"))
    store.create_release(SCOPE, _release(version_id="version-2"))
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(release_id="release-1"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "is pinned to version" in (result.reason or "")


@pytest.mark.asyncio
async def test_denied_when_destination_hash_does_not_match_canonical_recomputation() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(destination_hash="not-the-canonical-hash"))

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert result.record is None
    assert "destination_hash does not match" in (result.reason or "")


@pytest.mark.asyncio
async def test_destination_hash_check_runs_after_identity_revalidation() -> None:
    """A wrong destination_hash never masks a more specific identity denial:

    the operation-id mismatch (checked earlier) is what is reported, not a
    generic destination_hash mismatch, even though the request's supplied
    hash also happens to not match anything (it was computed for
    ``operation_id="search"``, not ``"other-operation"``)."""
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(
        _request(operation_id="other-operation", destination_hash="irrelevant-value")
    )

    assert result.outcome is ApprovalConsumptionOutcome.DENIED
    assert "is bound to operation" in (result.reason or "")


# --- happy path / single-use enforcement -----------------------------------


@pytest.mark.asyncio
async def test_first_consumption_succeeds_and_records_all_pins() -> None:
    store = AgentStudioStore()
    _seed(store)
    store.create_release(SCOPE, _release())
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request(release_id="release-1"))

    assert result.outcome is ApprovalConsumptionOutcome.CONSUMED
    assert result.record is not None
    assert result.record.approval_id == "approval-1"
    assert result.record.binding_id == "binding-1"
    assert result.record.operation_id == "search"
    assert result.record.instance_fingerprint == "fingerprint-1"
    assert result.record.policy_ref == "policy-1"
    assert result.record.release_id == "release-1"
    assert result.record.invocation_id == "invocation-1"
    assert result.record.idempotency_key == "invocation-key-1"
    assert result.record.tenant_id == TENANT
    assert result.record.project_id == PROJECT
    # Finding #4 (complete approval receipt): approval_version/approver_id/
    # expires_at are copied verbatim from the spent StudioApprovalRecord, and
    # consumption_version is this record's own schema tag -- a caller never
    # has to re-fetch the approval or invent these values itself.
    assert result.record.approval_version == "version-1"
    assert result.record.approver_id == USER_ID
    assert result.record.expires_at is None
    assert result.record.consumption_version == "approval-consumption-record:v1"

    stored = store.get_approval_consumption(SCOPE, "approval-1")
    assert stored == result.record


@pytest.mark.asyncio
async def test_receipt_copies_approval_expiry_verbatim() -> None:
    store = AgentStudioStore()
    expires_at = datetime.now(UTC) + timedelta(hours=2)
    _seed(store, expires_at=expires_at)
    port = StoreBackedApprovalConsumptionPort(store)

    result = await port.consume_approval(_request())

    assert result.outcome is ApprovalConsumptionOutcome.CONSUMED
    assert result.record is not None
    assert result.record.expires_at == expires_at


@pytest.mark.asyncio
async def test_retry_with_same_idempotency_key_reconciles_to_already_consumed() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    first = await port.consume_approval(_request())
    assert first.outcome is ApprovalConsumptionOutcome.CONSUMED

    retry = await port.consume_approval(_request())

    assert retry.outcome is ApprovalConsumptionOutcome.ALREADY_CONSUMED
    assert retry.record == first.record


@pytest.mark.asyncio
async def test_different_invocation_is_denied_as_exhausted() -> None:
    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    first = await port.consume_approval(_request(invocation_id="invocation-1", idempotency_key="key-1"))
    assert first.outcome is ApprovalConsumptionOutcome.CONSUMED

    second = await port.consume_approval(_request(invocation_id="invocation-2", idempotency_key="key-2"))

    assert second.outcome is ApprovalConsumptionOutcome.EXHAUSTED
    assert second.record == first.record
    assert "invocation-1" in (second.reason or "")


@pytest.mark.asyncio
async def test_concurrent_consumption_attempts_yield_exactly_one_winner() -> None:
    import asyncio

    store = AgentStudioStore()
    _seed(store)
    port = StoreBackedApprovalConsumptionPort(store)

    requests = [
        _request(invocation_id=f"invocation-{index}", idempotency_key=f"key-{index}") for index in range(8)
    ]
    results = await asyncio.gather(*(port.consume_approval(request) for request in requests))

    consumed = [result for result in results if result.outcome is ApprovalConsumptionOutcome.CONSUMED]
    exhausted = [result for result in results if result.outcome is ApprovalConsumptionOutcome.EXHAUSTED]
    assert len(consumed) == 1
    assert len(exhausted) == 7
    assert all(result.record == consumed[0].record for result in exhausted)
