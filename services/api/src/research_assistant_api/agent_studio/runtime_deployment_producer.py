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
* preserves the ratified fail-closed ordering across the two containers (no
  Cosmos transactional batch spans the ``/deployment_id`` mapping partition and
  the ``/client_app_id`` binding partition): GRANT writes the mapping revision
  FIRST then repoints the binding(s); REVOKE removes the binding(s) FIRST
  (authority withdrawn immediately) then writes the retiring revision;
* completes a REVOKE whose retiring-revision write failed (D), and refuses to
  retire a revision a binding currently points at (retention interlock).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from research_assistant_api.agent_studio.models import AuditEventKind
from research_assistant_api.agent_studio.runtime_client_binding import (
    ClientDeploymentBindingResolver,
    ClientDeploymentBindingWriter,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    RuntimeDeploymentMapping,
    RuntimeMappingLifecycleState,
)
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore


class RuntimeDeploymentProducerError(RuntimeError):
    """Base error for control-plane runtime deployment production."""


class NonGrantableMappingError(RuntimeDeploymentProducerError):
    """Raised when GRANT is asked to bind a mapping that is not live authority."""


class NonRevokingMappingError(RuntimeDeploymentProducerError):
    """Raised when REVOKE is given a revision that does not record a revocation."""


class FutureDatedRevocationError(RuntimeDeploymentProducerError):
    """Raised when a revoking revision's ``revoked_at`` is after the write instant."""


class RollbackRepointError(RuntimeDeploymentProducerError):
    """Raised when a grant would repoint a binding to an OLDER-or-equal revision.

    The core rollback control: a binding may only advance to a strictly greater
    ``revision_sequence`` for the same deployment. Refusing this at the sole
    index writer is what prevents a silent downgrade to a weaker revision.
    """


class RevisionStillReferencedError(RuntimeDeploymentProducerError):
    """Raised when retention is asked to retire a revision a binding still points at."""


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
        mapping_store: RuntimeDeploymentMappingStore,
        binding_writer: ClientDeploymentBindingWriter,
        binding_resolver: ClientDeploymentBindingResolver,
        audit: RuntimeBindingAuditRecorder,
    ) -> None:
        self._mapping_store = mapping_store
        self._binding_writer = binding_writer
        self._binding_resolver = binding_resolver
        self._audit = audit

    def grant(self, mapping: RuntimeDeploymentMapping, *, actor_id: str, now: datetime) -> RuntimeDeploymentMapping:
        """Publish ``mapping`` as live authority: write the revision, then bind.

        Enforces strictly-increasing repointing per (client, deployment) against
        the current pointer BEFORE any write (rollback protection), writes the
        mapping revision FIRST, repoints each allowed client's binding to this
        revision, and appends a grant/repoint audit event per client.
        """
        now = _require_aware_utc_now(now)
        if mapping.lifecycle_state is not RuntimeMappingLifecycleState.ACTIVE or mapping.revoked_at is not None:
            raise NonGrantableMappingError(
                "Only an ACTIVE, non-revoked mapping revision may be granted; "
                f"got lifecycle_state={mapping.lifecycle_state.value}, "
                f"revoked_at={'set' if mapping.revoked_at is not None else 'unset'}."
            )
        # Rollback guard FIRST, before any write: a bound (same-deployment) client
        # may only advance to a strictly greater revision_sequence.
        current = {
            binding.client_app_id: self._binding_resolver.resolve_binding(binding.client_app_id, mapping.deployment_id)
            for binding in mapping.allowed_client_app_role_bindings
        }
        for resolution in current.values():
            if (
                resolution is not None
                and resolution.revision_id != mapping.revision_id
                and mapping.revision_sequence <= resolution.revision_sequence
            ):
                raise RollbackRepointError(
                    "grant would repoint a binding to an older-or-equal revision "
                    f"(current sequence {resolution.revision_sequence}, requested {mapping.revision_sequence}); "
                    "a different revision may only advance the sequence."
                )
        persisted = self._mapping_store.put(mapping)
        for binding in mapping.allowed_client_app_role_bindings:
            prior = current[binding.client_app_id]
            self._binding_writer.grant(
                binding.client_app_id, mapping.deployment_id, mapping.revision_id, mapping.revision_sequence
            )
            self._record_binding_event(
                mapping,
                kind=(
                    AuditEventKind.RUNTIME_BINDING_GRANTED
                    if prior is None
                    else AuditEventKind.RUNTIME_BINDING_REPOINTED
                ),
                actor_id=actor_id,
                now=now,
                client_app_id=binding.client_app_id,
                from_sequence=None if prior is None else prior.revision_sequence,
                from_revision_id=None if prior is None else prior.revision_id,
            )
        return persisted

    def revoke(
        self, revoking_revision: RuntimeDeploymentMapping, *, actor_id: str, now: datetime
    ) -> RuntimeDeploymentMapping:
        """Withdraw authority for ``revoking_revision``'s deployment.

        ``revoking_revision`` is a NEW revision recording the revocation
        (``revoked_at`` set, not future-dated). Fail-closed ordering: every bound
        client is UNBOUND first (authority withdrawn immediately, with an audit
        event capturing the from-revision), THEN the revoking revision is
        persisted for durable lineage.
        """
        now = _require_aware_utc_now(now)
        if revoking_revision.revoked_at is None:
            raise NonRevokingMappingError(
                "REVOKE requires a revoking revision whose revoked_at is set; "
                "a lifecycle transition is a new revision, never an in-place edit."
            )
        if revoking_revision.revoked_at > now:
            raise FutureDatedRevocationError(
                "revoked_at must not be after the write instant; revocation records "
                "an act that has already happened (use expires_at for a future window)."
            )
        for binding in revoking_revision.allowed_client_app_role_bindings:
            prior = self._binding_resolver.resolve_binding(binding.client_app_id, revoking_revision.deployment_id)
            self._binding_writer.revoke(binding.client_app_id, revoking_revision.deployment_id)
            self._record_binding_event(
                revoking_revision,
                kind=AuditEventKind.RUNTIME_BINDING_REVOKED,
                actor_id=actor_id,
                now=now,
                client_app_id=binding.client_app_id,
                from_sequence=None if prior is None else prior.revision_sequence,
                from_revision_id=None if prior is None else prior.revision_id,
            )
        return self._mapping_store.put(revoking_revision)

    def reconcile_revoke(
        self, revoking_revision: RuntimeDeploymentMapping, *, actor_id: str, now: datetime
    ) -> RuntimeDeploymentMapping:
        """Idempotently COMPLETE a REVOKE whose retiring-revision write failed (D).

        The ratified ordering removes the binding first, so a crash between the
        unbind and the retiring-revision write leaves authority withdrawn but the
        audit record of *why* not yet durable. This re-ensures each client is
        unbound and re-persists the retiring revision (the store put is
        idempotent for identical content), so the lineage record is never lost.
        Safe to call repeatedly.
        """
        return self.revoke(revoking_revision, actor_id=actor_id, now=now)

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
        actor_id: str,
        now: datetime,
        client_app_id: str,
        from_sequence: int | None,
        from_revision_id: str | None,
    ) -> None:
        self._audit.record(
            tenant_id=mapping.tenant_id,
            project_id=mapping.project_id,
            kind=kind,
            actor_id=actor_id,
            subject_id=f"{client_app_id}:{mapping.deployment_id}",
            logical_agent_id=mapping.logical_agent_id,
            detail={
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
