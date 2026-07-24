"""Server-side resolution of a trusted ``ApprovalContext`` for a runtime
invocation, closing the "API never supplies trusted approval_id/invocation_id"
gap reported against ``dataset.compute`` and every other capability
operation gated by a ``CAPABILITY_OPERATION`` approval.

``approval_consumption.py`` already durably, atomically spends an approval
*given* an ``approval_id`` and ``invocation_id`` -- but until now, nothing in
this package told a runtime caller *which* ``approval_id`` currently
authorizes the specific binding/operation it is about to invoke, or minted
its ``invocation_id``. A caller that had to guess or otherwise supply its own
``approval_id``/``invocation_id`` could attempt to name an approval it was
never granted (relying entirely on ``StoreBackedApprovalConsumptionPort``'s
own revalidation to fail closed), or mint its own ``invocation_id`` with no
guarantee of unlinkability/traceability to a real resolution step.

This module closes that gap: given only the *plan* facts an authenticated
runtime invocation actually knows on its own (which release it is running
under, which binding, which operation), ``ApprovalContextResolver`` looks up
the release's own currently-*effectively-approved* ``CAPABILITY_OPERATION``
approval for that exact destination and returns a resolved
``ApprovalContext`` containing the real ``approval_id`` plus a freshly
server-generated ``invocation_id`` -- both values a caller could never have
forged, because both are chosen by this resolver, never accepted as input.
The caller is expected to pass this resolved context straight into
``POST /approvals/{approval_id}/consume`` (see ``router.py``), which
independently revalidates everything again before durably consuming it; this
resolver performs no durable write of its own and grants no authority by
itself -- it only tells an authenticated, in-scope caller which already-
decided approval (if any) currently covers its plan, exactly as
``compute_approval_effective_state`` would if asked directly, and mints a
fresh identifier for the *attempt* that is about to happen.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.approvals import compute_approval_effective_state
from research_assistant_api.agent_studio.models import ApprovalEffectiveState, ApprovalKind, StudioApprovalRecord
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

#: Versioned prefix for the durable approval-decision revision digest.
_APPROVAL_DECISION_REVISION_PREFIX = "approval-decision:v1:sha256:"


def compute_approval_decision_revision(record: StudioApprovalRecord) -> str:
    """Durable revision of an approval *decision record*.

    This is the value surfaced as ``approval_version`` -- deliberately a
    function of the decision's own authoritative fields (id, decided state,
    deciding approver, decision timestamp, rationale), NOT of the agent
    ``version_id`` the approval is pinned to. It changes if and only if the
    decision changes, so a consumer can pin the exact decision revision that
    authorized it; misusing the pinned AgentVersion id here was a confirmed
    backend blocker and is never done.
    """

    canonical = json.dumps(
        [
            record.id,
            record.state.value,
            record.approver_id,
            record.decided_at.isoformat() if record.decided_at is not None else None,
            record.rationale,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{_APPROVAL_DECISION_REVISION_PREFIX}{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"



class ApprovalContextRequest(BaseModel):
    """The plan facts an authenticated runtime invocation supplies to resolve
    a trusted ``ApprovalContext``.

    Deliberately excludes ``approval_id``/``invocation_id``/``principal_id``:
    a caller can never assert which approval authorizes it, mint its own
    invocation identifier, or claim an identity other than the one the
    request's own authentication already established (``principal_id`` is
    not even part of matching here -- an approval authorizes a *binding*,
    not a specific caller identity, matching how ``consume_approval`` itself
    already treats ``principal_id`` as record-keeping metadata, never a
    matching criterion).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ScopeContext
    release_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)


class ApprovalContextOutcome(StrEnum):
    """Outcome of a single ``resolve_context`` call.

    ``RESOLVED``: exactly one currently-effectively-``APPROVED``
    ``CAPABILITY_OPERATION`` approval exists for this release's binding's
    destination; its ``approval_id`` and a freshly minted ``invocation_id``
    are returned.
    ``NOT_APPROVED``: no such approval currently exists (never requested,
    still pending, rejected, expired, or revoked) -- fails closed rather
    than ever fabricating a context.
    ``NOT_FOUND``: the referenced release/binding/operation itself does not
    exist (or does not match) in this scope -- a distinct, non-authorization
    failure from "no approval exists for an otherwise-valid binding".
    """

    RESOLVED = "resolved"
    NOT_APPROVED = "not_approved"
    NOT_FOUND = "not_found"


class ApprovalContextResult(BaseModel):
    """Result of a single ``resolve_context`` call.

    ``approval_id``/``invocation_id`` are populated only for ``RESOLVED``;
    both are always ``None`` otherwise, so a caller can never mistake a
    denial for a usable context by only checking for a non-``None`` field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ApprovalContextOutcome
    approval_id: str | None = None
    #: The selected approval's durable *decision-record* revision (see
    #: ``compute_approval_decision_revision``) -- a function of the decision's
    #: own fields, never the pinned agent ``version_id``. Populated only for
    #: ``RESOLVED``.
    approval_version: str | None = None
    invocation_id: str | None = None
    reason: str | None = None


class ApprovalContextResolver(Protocol):
    """Port implemented by whatever composition root wires runtime invocation
    to trusted-context resolution.

    Async so a future adapter (e.g. one that also needs to confirm a
    runtime/provider-side precondition before minting a context) can perform
    I/O; the default ``StoreBackedApprovalContextResolver`` below does not
    need to await anything itself but still satisfies the async contract,
    mirroring ``ApprovalConsumptionPort``.
    """

    async def resolve_context(self, request: ApprovalContextRequest) -> ApprovalContextResult: ...


def _not_approved(reason: str) -> ApprovalContextResult:
    return ApprovalContextResult(outcome=ApprovalContextOutcome.NOT_APPROVED, reason=reason)


def _not_found(reason: str) -> ApprovalContextResult:
    return ApprovalContextResult(outcome=ApprovalContextOutcome.NOT_FOUND, reason=reason)


class StoreBackedApprovalContextResolver:
    """Default, production-safe resolver backed directly by ``AgentStudioStore``.

    Like ``StoreBackedApprovalConsumptionPort``, there is no external
    provider dependency here: resolving which of *this package's own*
    approval records currently authorizes a binding is something this
    package can do correctly on its own.
    """

    def __init__(self, store: AgentStudioStore) -> None:
        self._store = store

    async def resolve_context(self, request: ApprovalContextRequest) -> ApprovalContextResult:
        scope = request.scope
        release = self._store.get_release(scope, request.release_id)
        if release is None:
            return _not_found(f"Release '{request.release_id}' was not found in this scope.")
        version = self._store.get_version(scope, release.version_id)
        if version is None:
            return _not_found(
                f"Version '{release.version_id}' referenced by release '{request.release_id}' was not found."
            )
        binding = next(
            (b for b in version.manifest.capabilities if b.binding_id == request.binding_id),
            None,
        )
        if binding is None:
            return _not_found(
                f"Binding '{request.binding_id}' is not present on version '{release.version_id}' "
                f"(release '{request.release_id}')."
            )
        if binding.operation_ref.id != request.operation_id:
            return _not_found(
                f"Binding '{request.binding_id}' is bound to operation '{binding.operation_ref.id}', not "
                f"'{request.operation_id}' as requested."
            )
        destination = f"{binding.descriptor_ref.id}.{binding.operation_ref.id}"

        candidates = [
            record
            for record in self._store.list_approvals(scope, version_id=release.version_id)
            if record.kind is ApprovalKind.CAPABILITY_OPERATION and record.destination == destination
        ]
        approved = [record for record in candidates if self._is_effectively_approved(scope, record)]
        if not approved:
            return _not_approved(
                f"No currently-approved capability-operation approval exists for destination "
                f"'{destination}' on version '{release.version_id}'."
            )
        # Deterministic selection when more than one approval is currently
        # effectively APPROVED for the same destination (e.g. a re-request
        # after the prior approval's original consumption/expiry): the most
        # recently decided one wins. Never ambiguous, never random.
        approved.sort(key=lambda record: record.decided_at or record.requested_at)
        winner = approved[-1]
        return ApprovalContextResult(
            outcome=ApprovalContextOutcome.RESOLVED,
            approval_id=winner.id,
            approval_version=compute_approval_decision_revision(winner),
            invocation_id=f"inv-{uuid4().hex}",
        )

    def _is_effectively_approved(self, scope: ScopeContext, record: StudioApprovalRecord) -> bool:
        revoked = bool(self._store.list_revocations(scope, record.id))
        return compute_approval_effective_state(record, revoked=revoked) is ApprovalEffectiveState.APPROVED
