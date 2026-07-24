"""Control-plane producer for runtime deployment mappings and their bindings.

This is the ONE mutable authority in the runtime-trust chain and the only writer
of mappings and bindings. It is exercised exclusively by the human-authorized
control-plane deployment/release path, under the control-plane managed identity
(Cosmos Data Contributor on both containers). The runtime app-role identity is
Data Reader on the mapping container and Reader on the binding index (see the
IaC RBAC), so an attacker who can reach the runtime plane can neither write a
mapping revision nor land a binding.

Because immutable documents plus a mutable pointer give ROLLBACK exposure rather
than immutable authority (repointing a binding from revision N to an older N-k
restores that revision's weaker state, every document still valid and correctly
digested), the index write path is THE load-bearing control. This producer
therefore:

* enforces MONOTONIC repointing against the current pointer using a digest-
  covered integer ``revision_sequence`` -- never a timestamp (A2/A3): a grant may
  only move a (client, deployment) binding to a STRICTLY GREATER sequence, so a
  rollback is refused at the only place that can write the pointer;
* records every grant/repoint/revoke as an APPEND-ONLY audit event (from
  revision + digest, to revision + digest, actor, instant) via the established
  ``AuditService`` pattern (A4), so an illegitimate repoint is reconstructible
  afterwards even though the resulting state can be overwritten;
* preserves the ratified fail-closed ordering (GRANT writes the mapping revision
  FIRST -- now as ONE atomic single-partition batch [create revision, create/
  replace head] -- then repoints the binding(s); REVOKE is a single in-place CAS
  status-flip of the binding to a REVOKED tombstone, no new mapping revision);
* under contention retries the succession-head CAS a BOUNDED number of times
  (re-reading the head and rebuilding the revision at the new ``N+1`` each time),
  then fails TERMINAL and LOUD -- with NO non-atomic fallback -- and refuses to
  retire a revision a binding currently points at (retention interlock).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from research_assistant_api.agent_studio.models import AuditEventKind
from research_assistant_api.agent_studio.runtime_client_binding import (
    BindingPreconditionError,
    ClientDeploymentBindingResolver,
    ClientDeploymentBindingWriter,
    NonMonotonicRepointError,
    RuntimeBindingStatus,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_mapping_store import (
    RuntimeDeploymentMappingControlPlane,
    RuntimeHeadPreconditionError,
)

#: Bounded CAS retry budget for a repoint racing a concurrent control-plane
#: write. Each retry RE-READS the current pointer (never a blind retry).
_MAX_REPOINT_ATTEMPTS = 5

#: Bounded retry budget for the succession-head CAS when a SUPERSEDE races
#: another superseder. Each retry RE-READS the head, rebuilds the revision at the
#: new ``N+1``, and re-runs the full admission. On exhaustion the producer fails
#: TERMINAL and LOUD (``SuccessionExhaustedError``) -- there is deliberately NO
#: non-atomic fallback (splitting the batch into two sequential writes would
#: silently destroy the single-batch atomicity guarantee), so persistent
#: contention is surfaced, never worked around.
_MAX_SUCCESSION_ATTEMPTS = 5


class RuntimeDeploymentProducerError(RuntimeError):
    """Base error for control-plane runtime deployment production."""


class RollbackRepointError(RuntimeDeploymentProducerError):
    """Raised when a grant would repoint a binding to an OLDER-or-equal revision.

    The core rollback control: a binding may only advance to a strictly greater
    ``revision_sequence`` for the same deployment. Refusing this at the sole
    index writer is what prevents a silent downgrade to a weaker revision.
    """


class RevisionStillReferencedError(RuntimeDeploymentProducerError):
    """Raised when retention is asked to retire a revision a binding still points at."""


class SuccessionExhaustedError(RuntimeDeploymentProducerError):
    """Raised when the succession-head CAS did not converge within the bounded budget.

    Two superseders can each re-read the head, recompute ``N+1``, and retry, so
    under persistent contention the batch can keep aborting. This is the TERMINAL,
    LOUD failure on exhaustion: the control plane MUST surface it and re-drive the
    operation, and MUST NOT fall back to a non-atomic path. Splitting the atomic
    ``[create revision, create/replace head]`` batch into two sequential writes
    would silently destroy the single-batch atomicity the whole succession design
    rests on, so no such fallback exists.
    """


class SuccessionBuilderContractError(RuntimeDeploymentProducerError):
    """Raised when a succession ``build_revision`` callback returns a mapping that
    does not match the deployment/sequence it was asked to build (a programming
    error in the caller, surfaced loudly rather than silently committed)."""


class RuntimeBindingAuditRecorder(Protocol):
    """The subset of ``AuditService`` the producer needs to append binding events."""

    def record(
        self,
        *,
        tenant_id: str,
        project_id: str,
        kind: AuditEventKind,
        actor_id: str,
        subject_id: str,
        logical_agent_id: str | None = None,
        detail: dict[str, str] | None = None,
    ) -> object: ...


def _require_aware_utc_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC).")
    return now.astimezone(UTC)


class RuntimeDeploymentProducer:
    """Control-plane writer of mapping revisions and their client bindings."""

    def __init__(
        self,
        mapping_store: RuntimeDeploymentMappingControlPlane,
        binding_writer: ClientDeploymentBindingWriter,
        binding_resolver: ClientDeploymentBindingResolver,
        audit: RuntimeBindingAuditRecorder,
    ) -> None:
        self._mapping_store = mapping_store
        self._binding_writer = binding_writer
        self._binding_resolver = binding_resolver
        self._audit = audit

    def grant(self, mapping: RuntimeDeploymentMapping, *, actor_id: str, now: datetime) -> RuntimeDeploymentMapping:
        """Publish ``mapping`` as the current revision, then repoint the bindings.

        Succession is decided by the per-deployment HEAD (the single source of
        ``next``), NOT by the per-client bindings. The head read tells us whether
        this is a BOOTSTRAP (no head -> mapping must be sequence 1) or a SUPERSEDE
        (mapping must be head.current_sequence + 1). ``commit_revision`` writes the
        revision item and the head as ONE atomic unit (create-only on bootstrap,
        If-Match on supersede), so ``next`` can never skip or collide. Only then
        are the client bindings CAS-repointed to this revision; those may lag
        (monotonic advance, not strict successor) and are reconcilable.
        """
        now = _require_aware_utc_now(now)
        head = self._mapping_store.get_head(mapping.deployment_id)
        if head is None:
            if mapping.revision_sequence != 1:
                raise RollbackRepointError(
                    f"bootstrap grant for '{mapping.deployment_id}' must be sequence 1, "
                    f"got {mapping.revision_sequence}."
                )
            self._mapping_store.commit_revision(mapping, expected_head_sequence=None)
        else:
            if mapping.revision_sequence == head.current_sequence:
                # A re-grant of the CURRENT sequence is only legal if it is the
                # identical revision (idempotent replay); the same sequence with
                # different content is a rollback/forgery.
                if mapping.revision_id != head.current_revision_id:
                    raise RollbackRepointError(
                        f"grant for '{mapping.deployment_id}' at current sequence {head.current_sequence} "
                        "carries different content than the head revision."
                    )
            elif mapping.revision_sequence != head.current_sequence + 1:
                raise RollbackRepointError(
                    f"supersede grant for '{mapping.deployment_id}' must be the strict successor of head "
                    f"{head.current_sequence}, got {mapping.revision_sequence}."
                )
            self._mapping_store.commit_revision(mapping, expected_head_sequence=head.current_sequence)
        self._repoint_all_active(mapping, actor_id=actor_id, now=now)
        return mapping

    def grant_succession(
        self,
        deployment_id: str,
        build_revision: Callable[[int], RuntimeDeploymentMapping],
        *,
        actor_id: str,
        now: datetime,
    ) -> RuntimeDeploymentMapping:
        """Place the NEXT revision of ``deployment_id`` atomically, retrying head contention.

        The control-plane derives ``next`` from the HEAD (the single source of
        truth): ``next = current + 1`` (or ``1`` at bootstrap). It asks
        ``build_revision(next)`` for the revision content at that sequence, then
        commits it as ONE atomic single-partition batch ([create revision, create/
        replace head]). If a concurrent superseder moved the head first, the batch
        aborts (``RuntimeHeadPreconditionError``); this method RE-READS the head,
        rebuilds at the new ``next``, and retries -- BOUNDED by
        ``_MAX_SUCCESSION_ATTEMPTS`` -- then fails TERMINAL and LOUD with
        ``SuccessionExhaustedError``. There is deliberately NO non-atomic fallback:
        the only mapping write is the atomic batch inside ``commit_revision``, so
        persistent contention is surfaced, never worked around by two sequential
        writes. A ``RuntimeMappingConflictError`` (divergent content already at
        that sequence -- a forgery) is NOT retried; it propagates. Only after the
        atomic commit succeeds are the bindings CAS-repointed (their own bounded
        retry + full admission, including terminal revocation).
        """
        now = _require_aware_utc_now(now)
        for _attempt in range(_MAX_SUCCESSION_ATTEMPTS):
            head = self._mapping_store.get_head(deployment_id)
            next_sequence = 1 if head is None else head.current_sequence + 1
            mapping = build_revision(next_sequence)
            if mapping.deployment_id != deployment_id or mapping.revision_sequence != next_sequence:
                raise SuccessionBuilderContractError(
                    f"build_revision must return deployment '{deployment_id}' at sequence {next_sequence}, "
                    f"got '{mapping.deployment_id}' at {mapping.revision_sequence}."
                )
            try:
                self._mapping_store.commit_revision(
                    mapping, expected_head_sequence=None if head is None else head.current_sequence
                )
            except RuntimeHeadPreconditionError:
                # The head moved under us (a concurrent superseder won). Re-read,
                # rebuild at the new next, retry -- never a blind or non-atomic retry.
                continue
            self._repoint_all_active(mapping, actor_id=actor_id, now=now)
            return mapping
        raise SuccessionExhaustedError(
            f"succession for '{deployment_id}' did not converge within {_MAX_SUCCESSION_ATTEMPTS} attempts."
        )

    def _repoint_all_active(
        self, mapping: RuntimeDeploymentMapping, *, actor_id: str, now: datetime
    ) -> None:
        for binding in mapping.allowed_client_app_role_bindings:
            self._cas_repoint(binding.client_app_id, mapping, RuntimeBindingStatus.ACTIVE, actor_id=actor_id, now=now)

    def _cas_repoint(
        self,
        client_app_id: str,
        mapping: RuntimeDeploymentMapping,
        status: RuntimeBindingStatus,
        *,
        actor_id: str,
        now: datetime,
    ) -> None:
        """CAS-repoint one client's binding to ``mapping`` at ``status``, audited INTENT-FIRST.

        Ordering (A2): write the repoint INTENT (from-revision -> to-revision)
        BEFORE the CAS, perform the CAS, then mark it APPLIED. If a crash occurs
        between the two, the worst case is a recorded intent that never landed --
        recoverable and inspectable -- rather than a repoint that happened with
        no record (which would defeat A2). A dangling unresolved intent is itself
        a reconciliation signal. On a concurrent-modification precondition
        failure it RE-READS and retries (never blind); a non-monotonic repoint
        surfaces as ``RollbackRepointError``.
        """
        intent_id = uuid4().hex
        prior = self._binding_resolver.resolve_binding(client_app_id, mapping.deployment_id)
        kind = self._repoint_kind(status, prior)
        self._record_binding_event(
            mapping, kind=kind, phase="intent", intent_id=intent_id, actor_id=actor_id, now=now,
            client_app_id=client_app_id, prior=prior,
        )
        for _attempt in range(_MAX_REPOINT_ATTEMPTS):
            resolution = self._binding_resolver.resolve_binding(client_app_id, mapping.deployment_id)
            expected = None if resolution is None else resolution.revision_sequence
            try:
                self._binding_writer.repoint(
                    client_app_id,
                    mapping.deployment_id,
                    mapping.revision_sequence,
                    mapping.revision_id,
                    status,
                    expected_current_sequence=expected,
                )
            except BindingPreconditionError:
                continue
            except NonMonotonicRepointError as exc:
                raise RollbackRepointError(str(exc)) from exc
            self._record_binding_event(
                mapping, kind=kind, phase="applied", intent_id=intent_id, actor_id=actor_id, now=now,
                client_app_id=client_app_id, prior=resolution,
            )
            return
        raise BindingPreconditionError(
            f"repoint for '{client_app_id}' did not converge within {_MAX_REPOINT_ATTEMPTS} attempts."
        )

    @staticmethod
    def _repoint_kind(status: RuntimeBindingStatus, prior: object) -> AuditEventKind:
        if status is RuntimeBindingStatus.REVOKED:
            return AuditEventKind.RUNTIME_BINDING_REVOKED
        return AuditEventKind.RUNTIME_BINDING_GRANTED if prior is None else AuditEventKind.RUNTIME_BINDING_REPOINTED

    def revoke(self, mapping: RuntimeDeploymentMapping, *, actor_id: str, now: datetime) -> None:
        """Revoke authority for ``mapping``'s deployment: tombstone the binding(s).

        Under the ratified split, revocation is NOT a mapping fact (an immutable,
        digest-covered document can never be flipped to revoked). It is a SINGLE
        CAS write per client that flips the binding ``status`` to ``REVOKED`` at
        the SAME (sequence, revision_id) -- a tombstone that retains the
        succession counter and pinned digest so a later re-grant can derive the
        next sequence without a latest-query. No new mapping revision is written,
        so the cross-container ordering problem and the half-finished-revoke
        reconciliation case disappear. Authority is withdrawn immediately (the
        loader denies on ``status != ACTIVE``); ``mapping`` must be the CURRENT
        active revision, else the CAS precondition fails.
        """
        now = _require_aware_utc_now(now)
        for binding in mapping.allowed_client_app_role_bindings:
            self._cas_repoint(binding.client_app_id, mapping, RuntimeBindingStatus.REVOKED, actor_id=actor_id, now=now)

    def retire_revision(
        self, deployment_id: str, revision_sequence: int, revision_id: str, client_app_ids: tuple[str, ...]
    ) -> None:
        """Delete a superseded revision under the retention interlock.

        Refuses (``RevisionStillReferencedError``) to delete a revision any live
        binding still points at, then performs an EXACT ``(deployment_id,
        revision_sequence)`` deletion. Never deletes "the latest" and never
        scope-queries.
        """
        self.assert_safe_to_retire(deployment_id, revision_id, client_app_ids)
        self._mapping_store.delete(deployment_id, revision_sequence)

    def assert_safe_to_retire(self, deployment_id: str, revision_id: str, client_app_ids: tuple[str, ...]) -> None:
        """Retention interlock: refuse to retire a revision a binding still points at.

        Verifies against the CURRENT binding pointers (fresh reads) that no
        client's active binding references ``revision_id`` before it may be
        deleted. A pointer to a missing revision is the constraint-5 denial state
        handled by the loader, never a repairable one; retention only ever
        removes revisions no pointer references.
        """
        for client_app_id in client_app_ids:
            resolution = self._binding_resolver.resolve_binding(client_app_id, deployment_id)
            if resolution is not None and resolution.revision_id == revision_id:
                raise RevisionStillReferencedError(
                    f"revision '{revision_id}' of deployment '{deployment_id}' is still referenced by a "
                    f"binding for client '{client_app_id}'; it must not be retired."
                )

    def _record_binding_event(
        self,
        mapping: RuntimeDeploymentMapping,
        *,
        kind: AuditEventKind,
        phase: str,
        intent_id: str,
        actor_id: str,
        now: datetime,
        client_app_id: str,
        prior: object,
    ) -> None:
        from_sequence = getattr(prior, "revision_sequence", None)
        from_revision_id = getattr(prior, "revision_id", None)
        self._audit.record(
            tenant_id=mapping.tenant_id,
            project_id=mapping.project_id,
            kind=kind,
            actor_id=actor_id,
            subject_id=f"{client_app_id}:{mapping.deployment_id}",
            logical_agent_id=mapping.logical_agent_id,
            detail={
                "phase": phase,
                "intent_id": intent_id,
                "client_app_id": client_app_id,
                "deployment_id": mapping.deployment_id,
                "from_revision_sequence": "" if from_sequence is None else str(from_sequence),
                "to_revision_sequence": str(mapping.revision_sequence),
                "from_revision_id": from_revision_id or "",
                "to_revision_id": mapping.revision_id,
                "actor_id": actor_id,
                "instant": now.isoformat(),
            },
        )
