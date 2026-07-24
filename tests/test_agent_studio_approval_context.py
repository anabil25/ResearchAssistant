# mypy: disable-error-code=import-untyped

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.approval_context import (
    ApprovalContextOutcome,
    ApprovalContextRequest,
    ApprovalContextResult,
    StoreBackedApprovalContextResolver,
    compute_approval_decision_revision,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentVersion,
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
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

TENANT = "tenant-1"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
OTHER_SCOPE = ScopeContext(tenant_id=TENANT, project_id=OTHER_PROJECT)
AGENT_ID = "agent-context-1"
USER_ID = "user-1"

# Sentinel distinguishing "argument not supplied" from an explicit ``None``,
# typed ``Any`` so it satisfies every optional-parameter annotation below
# without provoking a mypy non-overlapping-identity-check false positive.
_UNSET: Any = object()


def _binding(
    *,
    binding_id: str = "binding-1",
    descriptor_id: str = "descriptor-1",
    operation_id: str = "search",
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=binding_id,
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id=descriptor_id),
        operation_ref=CapabilityOperationRef(id=operation_id),
        instance_ref=CapabilityInstanceRef(provider_id="provider-1", id="instance-1", fingerprint="fp-1"),
        connection_ref=CapabilityConnectionRef(id="connection-1"),
        policy_ref=CapabilityPolicyRef(id="policy-1"),
        attached_by=USER_ID,
    )


def _manifest(*, binding: CapabilityBinding | None = _UNSET) -> AgentManifest:
    resolved_binding = _binding() if binding is _UNSET else binding
    return AgentManifest(
        logical_agent_id=AGENT_ID,
        tenant_id=TENANT,
        project_id=PROJECT,
        display_name="Context Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id=USER_ID,
        capabilities=(resolved_binding,) if resolved_binding is not None else (),
    )


def _version(*, version_id: str = "version-1", manifest: AgentManifest | None = None) -> AgentVersion:
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


def _release(*, release_id: str = "release-1", version_id: str = "version-1") -> AgentRelease:
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
    kind: ApprovalKind = ApprovalKind.CAPABILITY_OPERATION,
    expires_at: datetime | None = None,
    decided_at: datetime | None = _UNSET,
) -> StudioApprovalRecord:
    resolved_decided_at = (
        (datetime.now(UTC) if state is not ApprovalState.PENDING else None)
        if decided_at is _UNSET
        else decided_at
    )
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=TENANT,
        project_id=PROJECT,
        kind=kind,
        state=state,
        gated_action="invoke_capability_operation",
        destination=destination,
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=f"approval-key-{approval_id}",
        approver_id=USER_ID if state is ApprovalState.APPROVED else None,
        decided_at=resolved_decided_at,
        expires_at=expires_at,
    )


def _request(
    *,
    scope: ScopeContext = SCOPE,
    release_id: str = "release-1",
    binding_id: str = "binding-1",
    operation_id: str = "search",
) -> ApprovalContextRequest:
    return ApprovalContextRequest(
        scope=scope,
        release_id=release_id,
        binding_id=binding_id,
        operation_id=operation_id,
    )


def _seed(store: AgentStudioStore, **approval_overrides: object) -> None:
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    store.create_approval(SCOPE, _approval(**approval_overrides))  # type: ignore[arg-type]


# --- model contract ---------------------------------------------------------


def test_decision_revision_is_deterministic_and_not_version_id() -> None:
    decided = datetime(2026, 5, 1, tzinfo=UTC)
    approved = _approval(state=ApprovalState.APPROVED, decided_at=decided)
    revision = compute_approval_decision_revision(approved)
    assert revision.startswith("approval-decision:v1:sha256:")
    assert revision == compute_approval_decision_revision(_approval(state=ApprovalState.APPROVED, decided_at=decided))
    # Never the pinned agent version_id.
    assert approved.version_id not in revision


def test_decision_revision_changes_with_the_decision() -> None:
    base = compute_approval_decision_revision(_approval(state=ApprovalState.APPROVED, approval_id="a1"))
    other_approver = compute_approval_decision_revision(
        _approval(state=ApprovalState.APPROVED, approval_id="a1").model_copy(update={"approver_id": "someone-else"})
    )
    assert base != other_approver


def test_decision_revision_handles_undecided_record() -> None:
    # decided_at None branch (a pending record has no decision timestamp).
    pending = _approval(state=ApprovalState.PENDING)
    assert pending.decided_at is None
    assert compute_approval_decision_revision(pending).startswith("approval-decision:v1:sha256:")


def test_request_is_frozen_and_rejects_unknown_fields() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        ApprovalContextRequest(**{**request.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        request.release_id = "other"


def test_result_is_frozen_and_rejects_unknown_fields() -> None:
    result = ApprovalContextResult(outcome=ApprovalContextOutcome.NOT_APPROVED, reason="no")
    with pytest.raises(ValidationError):
        ApprovalContextResult(**{**result.model_dump(), "unexpected": "value"})
    with pytest.raises(ValidationError):
        result.outcome = ApprovalContextOutcome.RESOLVED


def test_result_never_populates_ids_unless_resolved() -> None:
    result = ApprovalContextResult(outcome=ApprovalContextOutcome.NOT_APPROVED, reason="no")
    assert result.approval_id is None
    assert result.invocation_id is None


# --- NOT_FOUND paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_not_found_when_release_missing() -> None:
    store = AgentStudioStore()
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_FOUND
    assert result.approval_id is None
    assert result.invocation_id is None
    assert "was not found" in (result.reason or "")


@pytest.mark.asyncio
async def test_not_found_when_release_pins_missing_version() -> None:
    store = AgentStudioStore()
    store.create_release(SCOPE, _release(version_id="missing-version"))
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_FOUND
    assert "referenced by release" in (result.reason or "")


@pytest.mark.asyncio
async def test_not_found_when_binding_absent_from_version() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version(manifest=_manifest(binding=None)))
    store.create_release(SCOPE, _release())
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_FOUND
    assert "is not present on version" in (result.reason or "")


@pytest.mark.asyncio
async def test_not_found_when_operation_id_mismatches_binding() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request(operation_id="other-operation"))

    assert result.outcome is ApprovalContextOutcome.NOT_FOUND
    assert "is bound to operation" in (result.reason or "")


# --- NOT_APPROVED paths --------------------------------------------------


@pytest.mark.asyncio
async def test_not_approved_when_no_approval_exists_at_all() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED
    assert result.approval_id is None
    assert result.invocation_id is None
    assert "No currently-approved" in (result.reason or "")


@pytest.mark.asyncio
async def test_not_approved_when_approval_is_pending() -> None:
    store = AgentStudioStore()
    _seed(store, state=ApprovalState.PENDING)
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


@pytest.mark.asyncio
async def test_not_approved_when_approval_is_rejected() -> None:
    store = AgentStudioStore()
    _seed(store, state=ApprovalState.REJECTED)
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


@pytest.mark.asyncio
async def test_not_approved_when_approval_expired() -> None:
    store = AgentStudioStore()
    _seed(store, expires_at=datetime.now(UTC) - timedelta(hours=1))
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


@pytest.mark.asyncio
async def test_not_approved_when_approval_revoked() -> None:
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
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


@pytest.mark.asyncio
async def test_not_approved_when_only_wrong_kind_approval_exists() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    store.create_approval(SCOPE, _approval(kind=ApprovalKind.RELEASE_PROMOTION))
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


@pytest.mark.asyncio
async def test_not_approved_when_destination_mismatches() -> None:
    store = AgentStudioStore()
    _seed(store, destination="descriptor-1.other-operation")
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.NOT_APPROVED


# --- RESOLVED paths -------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_returns_approval_id_and_fresh_invocation_id() -> None:
    store = AgentStudioStore()
    _seed(store)
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.RESOLVED
    assert result.approval_id == "approval-1"
    # approval_version is the durable DECISION-RECORD revision, never the pinned
    # agent version_id ("version-1").
    assert result.approval_version is not None
    assert result.approval_version.startswith("approval-decision:v1:sha256:")
    assert result.approval_version != "version-1"
    assert result.invocation_id is not None
    assert result.invocation_id.startswith("inv-")


@pytest.mark.asyncio
async def test_resolved_mints_distinct_invocation_ids_across_calls() -> None:
    store = AgentStudioStore()
    _seed(store)
    resolver = StoreBackedApprovalContextResolver(store)

    first = await resolver.resolve_context(_request())
    second = await resolver.resolve_context(_request())

    assert first.invocation_id != second.invocation_id
    assert first.approval_id == second.approval_id == "approval-1"


@pytest.mark.asyncio
async def test_resolved_picks_most_recently_decided_approval_deterministically() -> None:
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    older = _approval(
        approval_id="approval-old",
        decided_at=datetime.now(UTC) - timedelta(hours=2),
    )
    newer = _approval(
        approval_id="approval-new",
        decided_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    # Insert the newer one first to prove ordering is by decided_at, not
    # insertion order.
    store.create_approval(SCOPE, newer)
    store.create_approval(SCOPE, older)
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.RESOLVED
    assert result.approval_id == "approval-new"


@pytest.mark.asyncio
async def test_resolved_falls_back_to_requested_at_when_decided_at_is_none() -> None:
    # PENDING approvals have no decided_at; mix with an APPROVED one to
    # confirm the sort key tolerates a None decided_at on excluded
    # candidates without raising, and still resolves the approved one.
    store = AgentStudioStore()
    store.create_version(SCOPE, _version())
    store.create_release(SCOPE, _release())
    store.create_approval(SCOPE, _approval(approval_id="approval-pending", state=ApprovalState.PENDING))
    store.create_approval(SCOPE, _approval(approval_id="approval-approved"))
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request())

    assert result.outcome is ApprovalContextOutcome.RESOLVED
    assert result.approval_id == "approval-approved"


# --- tenant/project isolation ---------------------------------------------


@pytest.mark.asyncio
async def test_cross_project_release_is_not_found() -> None:
    store = AgentStudioStore()
    _seed(store)
    resolver = StoreBackedApprovalContextResolver(store)

    result = await resolver.resolve_context(_request(scope=OTHER_SCOPE))

    assert result.outcome is ApprovalContextOutcome.NOT_FOUND
