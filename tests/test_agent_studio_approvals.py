from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.approvals import (
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
    StudioApprovalRecord,
)


def test_requires_approval_always_true_for_admin_escalation() -> None:
    assert requires_approval(actor_role=AgentRole.OWNER, kind=ApprovalKind.ADMIN_ESCALATION)
    assert requires_approval(actor_role=AgentRole.VIEWER, kind=ApprovalKind.ADMIN_ESCALATION)


def test_requires_approval_for_promotion_depends_on_role() -> None:
    assert requires_approval(actor_role=AgentRole.CONTRIBUTOR, kind=ApprovalKind.RELEASE_PROMOTION)
    assert requires_approval(actor_role=AgentRole.VIEWER, kind=ApprovalKind.FORK_PROMOTION)
    assert not requires_approval(actor_role=AgentRole.MAINTAINER, kind=ApprovalKind.RELEASE_PROMOTION)
    assert not requires_approval(actor_role=AgentRole.OWNER, kind=ApprovalKind.FORK_PROMOTION)


def test_idempotency_key_is_deterministic_and_distinguishes_inputs() -> None:
    key_a = idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION, version_id="v1", requested_by="u1", destination="prod"
    )
    key_b = idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION, version_id="v1", requested_by="u1", destination="prod"
    )
    key_c = idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION, version_id="v2", requested_by="u1", destination="prod"
    )
    assert key_a == key_b
    assert key_a != key_c


def test_build_approval_request_sets_tenant_id_and_idempotency_key() -> None:
    record = build_approval_request(
        approval_id="approval-1",
        tenant_id="demo",
        version_id="v1",
        kind=ApprovalKind.RELEASE_PROMOTION,
        gated_action="promote_version",
        destination="prod",
        requested_by="user-1",
        evidence_summary="All gates passed.",
        risk="medium",
    )
    assert record.tenant_id == "demo"
    assert record.state == ApprovalState.PENDING
    assert record.idempotency_key == idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION, version_id="v1", requested_by="user-1", destination="prod"
    )


def test_build_approval_request_admin_escalation_requires_requested_role() -> None:
    with pytest.raises(ApprovalError, match="requested_role"):
        build_approval_request(
            approval_id="approval-1",
            tenant_id="demo",
            version_id="agent-1",
            kind=ApprovalKind.ADMIN_ESCALATION,
            gated_action="grant_role",
            destination="agent-1",
            requested_by="user-1",
            evidence_summary="Needs elevated access.",
            risk="high",
        )


def test_build_approval_request_admin_escalation_with_role_succeeds() -> None:
    record = build_approval_request(
        approval_id="approval-1",
        tenant_id="demo",
        version_id="agent-1",
        kind=ApprovalKind.ADMIN_ESCALATION,
        gated_action="grant_role",
        destination="agent-1",
        requested_by="user-1",
        evidence_summary="Needs elevated access.",
        risk="high",
        requested_role=AgentRole.MAINTAINER,
    )
    assert record.requested_role == AgentRole.MAINTAINER


def _pending_record(kind: ApprovalKind = ApprovalKind.RELEASE_PROMOTION) -> StudioApprovalRecord:
    return build_approval_request(
        approval_id="approval-1",
        tenant_id="demo",
        version_id="v1",
        kind=kind,
        gated_action="promote_version",
        destination="prod",
        requested_by="user-1",
        evidence_summary="All gates passed.",
        risk="medium",
        requested_role=AgentRole.MAINTAINER if kind is ApprovalKind.ADMIN_ESCALATION else None,
    )


def test_decide_approval_approves_when_role_sufficient() -> None:
    record = _pending_record()
    decided = decide_approval(
        record, approver_id="approver-1", approver_role=AgentRole.MAINTAINER, approve=True, rationale="looks good"
    )
    assert decided.state == ApprovalState.APPROVED
    assert decided.approver_id == "approver-1"
    assert decided.rationale == "looks good"
    assert decided.decided_at is not None


def test_decide_approval_rejects_when_approve_false() -> None:
    record = _pending_record()
    decided = decide_approval(record, approver_id="approver-1", approver_role=AgentRole.OWNER, approve=False)
    assert decided.state == ApprovalState.REJECTED


def test_decide_approval_raises_when_already_decided() -> None:
    record = _pending_record()
    decided = decide_approval(record, approver_id="approver-1", approver_role=AgentRole.OWNER, approve=True)
    with pytest.raises(ApprovalError, match="already been decided"):
        decide_approval(decided, approver_id="approver-2", approver_role=AgentRole.OWNER, approve=True)


def test_decide_approval_raises_when_role_insufficient_for_promotion() -> None:
    record = _pending_record()
    with pytest.raises(ApprovalError, match="does not meet the minimum"):
        decide_approval(record, approver_id="approver-1", approver_role=AgentRole.CONTRIBUTOR, approve=True)


def test_decide_approval_raises_when_role_insufficient_for_escalation() -> None:
    record = _pending_record(kind=ApprovalKind.ADMIN_ESCALATION)
    with pytest.raises(ApprovalError, match="does not meet the minimum"):
        decide_approval(record, approver_id="approver-1", approver_role=AgentRole.MAINTAINER, approve=True)


def test_decide_approval_escalation_succeeds_with_owner_role() -> None:
    record = _pending_record(kind=ApprovalKind.ADMIN_ESCALATION)
    decided = decide_approval(record, approver_id="approver-1", approver_role=AgentRole.OWNER, approve=True)
    assert decided.state == ApprovalState.APPROVED
