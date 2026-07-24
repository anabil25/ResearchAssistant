"""Behavioral approval workflow and admin escalation.

Two concerns live here, both pure/deterministic:

* **Behavioral approvals** — non-devs (below ``AgentRole.MAINTAINER``)
  requesting a release or fork promotion must have a human maintainer/owner
  decide before the promotion proceeds.
* **Admin escalation** — any request for an elevated role (e.g. contributor
  asking to become maintainer/owner) always requires approval from an
  existing owner, regardless of the requester's current role.

Approval records are append-only: once decided (approved/rejected) a record
is never mutated again; a fresh request is required for another attempt.
Idempotency is enforced by ``idempotency_key`` — callers (the persistence
layer) must look up an existing record by that key before creating a new one.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from research_assistant_api.agent_studio.models import (
    AgentRole,
    ApprovalEffectiveState,
    ApprovalKind,
    ApprovalRevocation,
    ApprovalState,
    DeploymentEnvironment,
    StudioApprovalRecord,
    role_at_least,
    utc_now,
)

DEFAULT_APPROVAL_VALIDITY = timedelta(days=7)
"""How long a decided-pending approval remains actionable if no explicit
``expires_at`` is supplied. An approval is never a blanket, open-ended grant."""


class ApprovalError(RuntimeError):
    pass


def requires_approval(*, actor_role: AgentRole, kind: ApprovalKind) -> bool:
    """Return True when ``kind`` requires a behavioral approval for ``actor_role``.

    Admin escalation always requires approval. Release/fork promotion
    requires approval unless the actor already holds at least
    ``AgentRole.MAINTAINER`` (i.e. is a "dev" for this agent).
    """
    if kind is ApprovalKind.ADMIN_ESCALATION:
        return True
    return not role_at_least(actor_role, AgentRole.MAINTAINER)


def decider_minimum_role(kind: ApprovalKind) -> AgentRole:
    """The minimum role required to decide -- or revoke on another
    requester's behalf -- an approval of this ``kind``."""
    if kind is ApprovalKind.ADMIN_ESCALATION:
        return AgentRole.OWNER
    return AgentRole.MAINTAINER


def idempotency_key(
    *,
    kind: ApprovalKind,
    version_id: str,
    requested_by: str,
    destination: str,
) -> str:
    """Deterministic idempotency key for a would-be approval request.

    Same (kind, version, requester, destination) always yields the same key,
    so a persistence layer can detect and reuse an existing pending request
    instead of creating a duplicate.
    """
    payload = "|".join((kind.value, version_id, requested_by, destination))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_approval_request(
    *,
    approval_id: str,
    tenant_id: str,
    project_id: str,
    version_id: str,
    kind: ApprovalKind,
    gated_action: str,
    destination: str,
    requested_by: str,
    evidence_summary: str,
    risk: str,
    requested_role: AgentRole | None = None,
    content_hash: str | None = None,
    environment: DeploymentEnvironment | None = None,
    permissions_policy_ref: str | None = None,
    destination_policy_ref: str | None = None,
    expires_at: datetime | None = None,
) -> StudioApprovalRecord:
    if kind is ApprovalKind.ADMIN_ESCALATION and requested_role is None:
        raise ApprovalError("Admin escalation requests must specify requested_role.")
    resolved_expiry = expires_at if expires_at is not None else utc_now() + DEFAULT_APPROVAL_VALIDITY
    return StudioApprovalRecord(
        id=approval_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        kind=kind,
        gated_action=gated_action,
        destination=destination,
        requested_by=requested_by,
        evidence_summary=evidence_summary,
        risk=risk,
        idempotency_key=idempotency_key(
            kind=kind, version_id=version_id, requested_by=requested_by, destination=destination
        ),
        requested_role=requested_role,
        content_hash=content_hash,
        environment=environment,
        permissions_policy_ref=permissions_policy_ref,
        destination_policy_ref=destination_policy_ref,
        expires_at=resolved_expiry,
    )


def decide_approval(
    record: StudioApprovalRecord,
    *,
    approver_id: str,
    approver_role: AgentRole,
    approve: bool,
    rationale: str | None = None,
) -> StudioApprovalRecord:
    """Apply a decision to a pending approval, returning a new immutable copy.

    Raises ``ApprovalError`` if the record is not pending, has already
    expired, or if ``approver_role`` does not meet the minimum role required
    to decide this kind of request (append-only: decided records are never
    re-decided).
    """
    if record.state != ApprovalState.PENDING:
        raise ApprovalError(f"Approval '{record.id}' has already been decided ({record.state.value}).")
    if record.expires_at is not None and record.expires_at < utc_now():
        raise ApprovalError(f"Approval '{record.id}' expired at {record.expires_at.isoformat()}.")
    if approver_id == record.requested_by:
        raise ApprovalError(
            f"Approver '{approver_id}' cannot decide an approval they requested themselves (self-approval)."
        )
    minimum = decider_minimum_role(record.kind)
    if not role_at_least(approver_role, minimum):
        raise ApprovalError(
            f"Approver role '{approver_role.value}' does not meet the minimum '{minimum.value}' "
            f"required to decide a {record.kind.value} request."
        )
    return record.model_copy(
        update={
            "state": ApprovalState.APPROVED if approve else ApprovalState.REJECTED,
            "approver_id": approver_id,
            "decided_at": utc_now(),
            "rationale": rationale,
            # Advance the monotonic decision revision (append-only: 0 -> 1).
            "decision_revision": record.decision_revision + 1,
        }
    )


def compute_approval_effective_state(
    record: StudioApprovalRecord,
    *,
    revoked: bool = False,
    now: datetime | None = None,
) -> ApprovalEffectiveState:
    """Derive the *current* effective state of ``record``.

    Never trusts ``record.state`` alone: revocation always wins (even over
    a still-``PENDING`` record -- withdrawing a pending request must block
    it from later being decided), then expiry (an ``APPROVED`` record past
    ``expires_at`` is no longer actionable even though it was never
    "rejected"), and only then falls back to the record's own immutable
    stored ``state``. Callers must recompute this at every read and
    enforcement point rather than caching or persisting the result --
    revocation/expiry are time-varying facts external to the record itself.
    """
    if revoked:
        return ApprovalEffectiveState.REVOKED
    effective_now = now if now is not None else utc_now()
    if (
        record.state == ApprovalState.APPROVED
        and record.expires_at is not None
        and record.expires_at <= effective_now
    ):
        return ApprovalEffectiveState.EXPIRED
    return ApprovalEffectiveState(record.state.value)


def revocation_idempotency_key(*, approval_id: str, actor_id: str, reason: str) -> str:
    """Deterministic idempotency key for a would-be revocation.

    Same (approval, actor, reason) always yields the same key, so a
    persistence layer can detect and return an existing revocation instead
    of appending a duplicate append-only event for a retried request.
    """
    payload = "|".join((approval_id, actor_id, reason))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def revoke_approval(
    record: StudioApprovalRecord,
    *,
    revocation_id: str,
    actor_id: str,
    actor_role: AgentRole,
    is_platform_owner: bool,
    reason: str,
    policy_ref: str | None = None,
) -> ApprovalRevocation:
    """Construct an append-only ``ApprovalRevocation`` for ``record``.

    Self-revocation (the original requester withdrawing their own request)
    is always permitted, symmetric with self-approval always being denied
    in :func:`decide_approval`. Any other actor must meet the same minimum
    role required to *decide* this approval kind, or be a platform owner.
    Revocation is independent of the record's current decided/expired
    state -- an already-rejected, already-expired, or still-pending record
    can all be revoked; this is a distinct, permanent "never honor this"
    signal, not a state transition on the record itself.
    """
    if actor_id != record.requested_by and not is_platform_owner:
        minimum = decider_minimum_role(record.kind)
        if not role_at_least(actor_role, minimum):
            raise ApprovalError(
                f"Actor role '{actor_role.value}' does not meet the minimum '{minimum.value}' required to "
                f"revoke a {record.kind.value} approval requested by another user."
            )
    return ApprovalRevocation(
        id=revocation_id,
        approval_id=record.id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        actor_id=actor_id,
        reason=reason,
        policy_ref=policy_ref,
        idempotency_key=revocation_idempotency_key(approval_id=record.id, actor_id=actor_id, reason=reason),
    )
