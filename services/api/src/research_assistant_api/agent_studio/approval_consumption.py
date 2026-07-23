"""Atomic, durable, one-time consumption of a capability-operation approval
at actual runtime invocation.

``StudioApprovalRecord``/``ApprovalEffectiveState`` (see ``approvals.py``)
answer "is this approval currently valid to act on" -- a purely
decision/expiry/revocation question. This module answers the orthogonal
question "has this specific runtime invocation already spent it", which
requires atomic, durable bookkeeping distinct from the approval decision
itself: a ``CAPABILITY_OPERATION`` approval is, by default, a **single-use**
grant. Exactly one invocation may ever durably consume it; every other
invocation attempting to reuse the same ``approval_id`` must be denied
(fail closed), except a genuine retry of the *same* invocation (identified
by ``idempotency_key``), which must reconcile to the original stored result
rather than being treated as a fresh consumption or a hard denial.

The port is **async** and expressed entirely in this package's own domain
types (``ApprovalConsumptionRequest``/``ApprovalConsumptionResult``/
``ApprovalConsumptionRecord``), mirroring the
``capability_discovery.CapabilityDiscoverySource`` port pattern: a
``Protocol`` defines the seam, ``StoreBackedApprovalConsumptionPort`` is the
production-safe default backed directly by this package's own
``AgentStudioStore`` (no external provider needed to consume a *backend*
approval), and a real runtime/provider adapter can later wrap or replace it
via composition-root injection -- "Context factory injects adapter after
provider merges" -- without ``agent_studio`` importing that adapter's types.

Every field on ``ApprovalConsumptionRequest`` that identifies *what* is
being invoked (binding, instance fingerprint, operation, version, policy) is
independently revalidated here against the approval's own pinned
``version_id`` and that version's exact ``CapabilityBinding`` -- never
trusted from the caller alone -- so a request naming the right
``approval_id`` cannot be used to spend it against a different
binding/operation/instance than what was actually approved (the same
fail-closed doctrine already applied to draft/gate/deploy binding
revalidation in ``policy_gates.py``).
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.approvals import compute_approval_effective_state
from research_assistant_api.agent_studio.models import (
    ApprovalConsumptionOutcome,
    ApprovalConsumptionRecord,
    ApprovalEffectiveState,
    ApprovalKind,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore


class ApprovalConsumptionRequest(BaseModel):
    """Non-optional context for a single approval-consumption attempt.

    There is deliberately no way to construct a request without naming
    exactly which binding/operation/instance/args/destination this
    invocation is exercising: every identifying field is required (or
    explicitly optional only when the underlying binding legitimately has
    no instance/policy pin), so ``StoreBackedApprovalConsumptionPort`` can
    always cross-check the request against the approval's own pinned
    version without guessing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ScopeContext
    approval_id: str = Field(min_length=1, max_length=200)
    principal_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    instance_fingerprint: str | None = None
    operation_id: str = Field(min_length=1, max_length=200)
    operation_version: str | None = None
    args_hash: str = Field(min_length=1)
    destination_hash: str = Field(min_length=1)
    policy_ref: str | None = None
    release_id: str | None = None
    invocation_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1)


class ApprovalConsumptionResult(BaseModel):
    """Result of a single ``consume_approval`` call.

    ``record`` is populated for ``CONSUMED``, ``ALREADY_CONSUMED``, and
    ``EXHAUSTED`` (the durable winning consumption, even when this call
    itself lost the race) but is always ``None`` for ``DENIED`` -- a denial
    never reaches the point of even attempting a durable write. ``reason``
    carries a human-readable, non-sensitive explanation for anything other
    than a first-time ``CONSUMED``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ApprovalConsumptionOutcome
    record: ApprovalConsumptionRecord | None = None
    reason: str | None = None


class ApprovalConsumptionPort(Protocol):
    """Port implemented by whatever composition root wires runtime
    invocation to approval consumption.

    ``consume_approval`` is async so a future adapter (e.g. one that must
    also confirm a runtime/provider-side execution before recording
    consumption) can perform I/O; the default
    ``StoreBackedApprovalConsumptionPort`` below does not need to await
    anything itself but still satisfies the async contract.
    """

    async def consume_approval(self, request: ApprovalConsumptionRequest) -> ApprovalConsumptionResult: ...


def _denied(reason: str) -> ApprovalConsumptionResult:
    return ApprovalConsumptionResult(outcome=ApprovalConsumptionOutcome.DENIED, reason=reason)


class StoreBackedApprovalConsumptionPort:
    """Default, production-safe consumption port backed directly by
    ``AgentStudioStore``.

    Unlike capability discovery (which genuinely requires an external
    provider integration), consuming a *backend-owned* approval record is
    something this package can do correctly on its own -- there is no
    "null"/unavailable variant here, because there is nothing external to
    be unavailable. A future runtime/provider adapter may still wrap this
    (e.g. to additionally confirm the actual tool execution succeeded
    before durably recording consumption), but never bypass its
    fail-closed validation.
    """

    def __init__(self, store: AgentStudioStore) -> None:
        self._store = store

    async def consume_approval(self, request: ApprovalConsumptionRequest) -> ApprovalConsumptionResult:
        scope = request.scope
        approval = self._store.get_approval(scope, request.approval_id)
        if approval is None:
            return _denied(f"Approval '{request.approval_id}' was not found in this scope.")
        if approval.kind is not ApprovalKind.CAPABILITY_OPERATION:
            return _denied(f"Approval '{request.approval_id}' is not a capability-operation approval.")

        revoked = bool(self._store.list_revocations(scope, request.approval_id))
        effective_state = compute_approval_effective_state(approval, revoked=revoked)
        if effective_state is not ApprovalEffectiveState.APPROVED:
            return _denied(
                f"Approval '{request.approval_id}' is not currently approved "
                f"(effective state: {effective_state.value})."
            )

        version = self._store.get_version(scope, approval.version_id)
        if version is None:
            return _denied(
                f"Version '{approval.version_id}' referenced by approval '{request.approval_id}' was not found."
            )
        binding = next(
            (b for b in version.manifest.capabilities if b.binding_id == request.binding_id),
            None,
        )
        if binding is None:
            return _denied(
                f"Binding '{request.binding_id}' is not present on version '{approval.version_id}', which "
                f"approval '{request.approval_id}' is pinned to."
            )
        destination = f"{binding.descriptor_ref.id}.{binding.operation_ref.id}"
        if destination != approval.destination:
            return _denied(
                f"Binding '{request.binding_id}' resolves to destination '{destination}', but approval "
                f"'{request.approval_id}' was granted for destination '{approval.destination}'."
            )
        if binding.operation_ref.id != request.operation_id:
            return _denied(
                f"Binding '{request.binding_id}' is bound to operation '{binding.operation_ref.id}', not "
                f"'{request.operation_id}' as this invocation claims."
            )
        if request.instance_fingerprint is not None and (
            binding.instance_ref is None or binding.instance_ref.fingerprint != request.instance_fingerprint
        ):
            return _denied(
                f"Binding '{request.binding_id}' instance fingerprint does not match this invocation "
                "(stale binding)."
            )
        if request.policy_ref is not None and (
            binding.policy_ref is None or binding.policy_ref.id != request.policy_ref
        ):
            return _denied(f"Binding '{request.binding_id}' policy reference does not match this invocation.")
        if request.release_id is not None:
            release = self._store.get_release(scope, request.release_id)
            if release is None:
                return _denied(f"Release '{request.release_id}' was not found in this scope.")
            if release.version_id != approval.version_id:
                return _denied(
                    f"Release '{request.release_id}' is for version '{release.version_id}', but approval "
                    f"'{request.approval_id}' is pinned to version '{approval.version_id}'."
                )

        candidate = ApprovalConsumptionRecord(
            id=str(uuid4()),
            approval_id=request.approval_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            principal_id=request.principal_id,
            binding_id=request.binding_id,
            instance_fingerprint=request.instance_fingerprint,
            operation_id=request.operation_id,
            operation_version=request.operation_version,
            args_hash=request.args_hash,
            destination_hash=request.destination_hash,
            policy_ref=request.policy_ref,
            release_id=request.release_id,
            invocation_id=request.invocation_id,
            idempotency_key=request.idempotency_key,
        )
        winner = self._store.create_approval_consumption(scope, candidate)
        if winner.id == candidate.id:
            return ApprovalConsumptionResult(outcome=ApprovalConsumptionOutcome.CONSUMED, record=winner)
        if winner.idempotency_key == request.idempotency_key:
            return ApprovalConsumptionResult(outcome=ApprovalConsumptionOutcome.ALREADY_CONSUMED, record=winner)
        return ApprovalConsumptionResult(
            outcome=ApprovalConsumptionOutcome.EXHAUSTED,
            record=winner,
            reason=(
                f"Approval '{request.approval_id}' was already consumed by a different invocation "
                f"('{winner.invocation_id}')."
            ),
        )
