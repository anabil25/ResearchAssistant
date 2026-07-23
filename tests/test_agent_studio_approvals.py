from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import research_assistant_api.agent_studio.approvals as approvals_module
from research_assistant_api.agent_studio.approvals import (
    DEFAULT_APPROVAL_VALIDITY,
    ApprovalError,
    build_approval_request,
    decide_approval,
    idempotency_key,
    requires_approval,
)
from research_assistant_api.agent_studio.models import (
    AgentRole,
    ApprovalKind,
    ApprovalState,
    DeploymentEnvironment,
    StudioApprovalRecord,
)

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _pending_record(
    *,
    kind: ApprovalKind = ApprovalKind.RELEASE_PROMOTION,
    expires_at: datetime | None = None,
) -> StudioApprovalRecord:
    return build_approval_request(
        approval_id="approval-1",
        tenant_id="tenant-a",
        project_id="project-a",
        version_id="version-1",
        kind=kind,
        gated_action="promote_version" if kind is not ApprovalKind.ADMIN_ESCALATION else "grant_role",
        destination="development",
        requested_by="requester-1",
        evidence_summary="All hard gates passed.",
        risk="medium",
        requested_role=AgentRole.MAINTAINER if kind is ApprovalKind.ADMIN_ESCALATION else None,
        expires_at=expires_at,
    )


def test_requires_approval_depends_on_kind_and_actor_role() -> None:
    assert requires_approval(actor_role=AgentRole.OWNER, kind=ApprovalKind.ADMIN_ESCALATION)
    assert requires_approval(actor_role=AgentRole.VIEWER, kind=ApprovalKind.ADMIN_ESCALATION)
    assert requires_approval(actor_role=AgentRole.CONTRIBUTOR, kind=ApprovalKind.RELEASE_PROMOTION)
    assert requires_approval(actor_role=AgentRole.VIEWER, kind=ApprovalKind.FORK_PROMOTION)
    assert not requires_approval(actor_role=AgentRole.MAINTAINER, kind=ApprovalKind.RELEASE_PROMOTION)
    assert not requires_approval(actor_role=AgentRole.OWNER, kind=ApprovalKind.FORK_PROMOTION)


def test_idempotency_key_is_deterministic_and_distinguishes_inputs() -> None:
    key_a = idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION,
        version_id="version-1",
        requested_by="requester-1",
        destination="production",
    )
    key_b = idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION,
        version_id="version-1",
        requested_by="requester-1",
        destination="production",
    )
    key_c = idempotency_key(
        kind=ApprovalKind.FORK_PROMOTION,
        version_id="version-1",
        requested_by="requester-1",
        destination="production",
    )
    assert key_a == key_b
    assert key_a != key_c


def test_build_approval_request_binds_context_and_default_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals_module, "utc_now", lambda: FIXED_NOW)

    record = build_approval_request(
        approval_id="approval-1",
        tenant_id="tenant-a",
        project_id="project-a",
        version_id="version-1",
        kind=ApprovalKind.RELEASE_PROMOTION,
        gated_action="promote_version",
        destination="development",
        requested_by="requester-1",
        evidence_summary="All hard gates passed.",
        risk="low",
        content_hash="manifest-sha",
        environment=DeploymentEnvironment.DEVELOPMENT,
        permissions_policy_ref="perm-policy-v1",
        destination_policy_ref="dest-policy-v1",
    )

    assert record.tenant_id == "tenant-a"
    assert record.state is ApprovalState.PENDING
    assert record.content_hash == "manifest-sha"
    assert record.environment is DeploymentEnvironment.DEVELOPMENT
    assert record.permissions_policy_ref == "perm-policy-v1"
    assert record.destination_policy_ref == "dest-policy-v1"
    assert record.expires_at == FIXED_NOW + DEFAULT_APPROVAL_VALIDITY
    assert record.idempotency_key == idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION,
        version_id="version-1",
        requested_by="requester-1",
        destination="development",
    )


def test_build_approval_request_admin_escalation_requires_requested_role() -> None:
    with pytest.raises(ApprovalError, match="requested_role"):
        build_approval_request(
            approval_id="approval-1",
            tenant_id="tenant-a",
            project_id="project-a",
            version_id="agent-one",
            kind=ApprovalKind.ADMIN_ESCALATION,
            gated_action="grant_role",
            destination="agent-one",
            requested_by="requester-1",
            evidence_summary="Needs elevated access.",
            risk="high",
        )


def test_build_approval_request_admin_escalation_preserves_explicit_expiry() -> None:
    explicit_expiry = FIXED_NOW + timedelta(hours=12)

    record = build_approval_request(
        approval_id="approval-1",
        tenant_id="tenant-a",
        project_id="project-a",
        version_id="agent-one",
        kind=ApprovalKind.ADMIN_ESCALATION,
        gated_action="grant_role",
        destination="agent-one",
        requested_by="requester-1",
        evidence_summary="Needs elevated access.",
        risk="high",
        requested_role=AgentRole.MAINTAINER,
        expires_at=explicit_expiry,
    )

    assert record.requested_role is AgentRole.MAINTAINER
    assert record.expires_at == explicit_expiry


@pytest.mark.parametrize(
    ("approve", "expected_state", "rationale"),
    [
        (True, ApprovalState.APPROVED, "looks good"),
        (False, ApprovalState.REJECTED, None),
    ],
)
def test_decide_approval_sets_decision_fields(
    monkeypatch: pytest.MonkeyPatch,
    approve: bool,
    expected_state: ApprovalState,
    rationale: str | None,
) -> None:
    decided_at = FIXED_NOW + timedelta(minutes=5)
    monkeypatch.setattr(approvals_module, "utc_now", lambda: decided_at)

    decided = decide_approval(
        _pending_record(),
        approver_id="approver-1",
        approver_role=AgentRole.MAINTAINER,
        approve=approve,
        rationale=rationale,
    )

    assert decided.state is expected_state
    assert decided.approver_id == "approver-1"
    assert decided.decided_at == decided_at
    assert decided.rationale == rationale


def test_decide_approval_rejects_non_pending_records() -> None:
    decided = decide_approval(
        _pending_record(),
        approver_id="approver-1",
        approver_role=AgentRole.OWNER,
        approve=True,
    )

    with pytest.raises(ApprovalError, match="already been decided"):
        decide_approval(
            decided,
            approver_id="approver-2",
            approver_role=AgentRole.OWNER,
            approve=False,
        )


def test_decide_approval_rejects_expired_pending_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(approvals_module, "utc_now", lambda: FIXED_NOW)
    expired = _pending_record(expires_at=FIXED_NOW - timedelta(seconds=1))

    with pytest.raises(ApprovalError, match="expired at"):
        decide_approval(
            expired,
            approver_id="approver-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_approval_rejects_self_approval() -> None:
    """A requester may never decide their own approval, even holding a
    sufficient role (e.g. an owner requesting their own admin escalation)."""
    record = _pending_record(kind=ApprovalKind.ADMIN_ESCALATION)

    with pytest.raises(ApprovalError, match="self-approval"):
        decide_approval(
            record,
            approver_id=record.requested_by,
            approver_role=AgentRole.OWNER,
            approve=True,
        )


@pytest.mark.parametrize(
    ("kind", "approver_role"),
    [
        (ApprovalKind.RELEASE_PROMOTION, AgentRole.CONTRIBUTOR),
        (ApprovalKind.ADMIN_ESCALATION, AgentRole.MAINTAINER),
    ],
)
def test_decide_approval_enforces_minimum_approver_role(
    kind: ApprovalKind,
    approver_role: AgentRole,
) -> None:
    with pytest.raises(ApprovalError, match="does not meet the minimum"):
        decide_approval(
            _pending_record(kind=kind),
            approver_id="approver-1",
            approver_role=approver_role,
            approve=True,
        )


def test_decide_approval_allows_owner_to_approve_admin_escalation() -> None:
    decided = decide_approval(
        _pending_record(kind=ApprovalKind.ADMIN_ESCALATION),
        approver_id="owner-1",
        approver_role=AgentRole.OWNER,
        approve=True,
        rationale="approved",
    )

    assert decided.state is ApprovalState.APPROVED
    assert decided.approver_id == "owner-1"
