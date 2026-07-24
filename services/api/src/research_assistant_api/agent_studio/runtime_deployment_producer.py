"""Control-plane producer for runtime deployment mappings and their bindings.

This is the ONE mutable authority in the runtime-trust chain and the only writer
of mappings and bindings. It is exercised exclusively by the human-authorized
control-plane deployment/release path, under the control-plane managed identity
(Cosmos Data Contributor on both containers). The runtime app-role identity is
Data Reader on the mapping container and Reader on the binding index (see the
IaC RBAC), so an attacker who can reach the runtime plane can neither write a
mapping revision nor land a binding.

Two operations, each encoding the ratified fail-closed ordering across the two
containers (no Cosmos transactional batch spans the ``/deployment_id`` mapping
partition and the ``/client_app_id`` binding partition, so ordering is the
integrity mechanism):

* **GRANT** writes the mapping revision FIRST, then repoints the binding(s) to
  that revision. A binding therefore never points at a missing revision.
* **REVOKE** removes the binding(s) FIRST (authority withdrawn immediately),
  then writes the retiring revision that records the transition. A retired
  deployment's authority is gone the instant its binding is removed, before the
  retiring revision is even persisted.

Because mappings are immutable and content-addressed, a lifecycle transition is
a NEW revision, never an in-place mutation -- so REVOKE persists a *revoking
revision* (``revoked_at`` set) rather than editing the active one. The new
requirement that falls out of caller-supplied ``created_at`` -- a future-dated
``revoked_at`` would be expressible -- is enforced here against the injected
write instant: revocation records an act that has already happened.
"""

from __future__ import annotations

from datetime import UTC, datetime

from research_assistant_api.agent_studio.runtime_client_binding import ClientDeploymentBindingWriter
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    RuntimeDeploymentMapping,
    RuntimeMappingLifecycleState,
)
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore


class RuntimeDeploymentProducerError(RuntimeError):
    """Base error for control-plane runtime deployment production."""


class NonGrantableMappingError(RuntimeDeploymentProducerError):
    """Raised when GRANT is asked to bind a mapping that is not live authority.

    Only an ``ACTIVE`` mapping with no revocation may be granted; binding a
    superseded/retired/revoked revision would hand out authority the lifecycle
    evaluation is designed to deny.
    """


class NonRevokingMappingError(RuntimeDeploymentProducerError):
    """Raised when REVOKE is given a revision that does not record a revocation."""


class FutureDatedRevocationError(RuntimeDeploymentProducerError):
    """Raised when a revoking revision's ``revoked_at`` is after the write instant.

    Revocation records an action that HAS happened; a future withdrawal is
    already expressible via ``expires_at``. Permitting a future ``revoked_at``
    would create a second, overlapping mechanism for one outcome and let a
    "not yet revoked" mapping deny immediately.
    """


def _require_aware_utc_now(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC).")
    return now.astimezone(UTC)


class RuntimeDeploymentProducer:
    """Control-plane writer of mapping revisions and their client bindings.

    Holds the writable mapping store and the binding writer surface. Neither is
    ever exposed to the runtime plane; the runtime plane only ever receives the
    read-only resolver + a read view of the store via the authorized loader.
    """

    def __init__(
        self,
        mapping_store: RuntimeDeploymentMappingStore,
        binding_writer: ClientDeploymentBindingWriter,
    ) -> None:
        self._mapping_store = mapping_store
        self._binding_writer = binding_writer

    def grant(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        """Publish ``mapping`` as live authority: write the revision, then bind.

        Fail-closed ordering: the mapping revision is persisted FIRST (idempotent
        for identical content), then every ``client_app_id`` in the mapping's
        server-owned allowlist is repointed to this exact revision. If the store
        write raises (e.g. a divergent conflict) no binding is ever touched, so
        the index can never point at a revision that failed to land.
        """
        if mapping.lifecycle_state is not RuntimeMappingLifecycleState.ACTIVE or mapping.revoked_at is not None:
            raise NonGrantableMappingError(
                "Only an ACTIVE, non-revoked mapping revision may be granted; "
                f"got lifecycle_state={mapping.lifecycle_state.value}, "
                f"revoked_at={'set' if mapping.revoked_at is not None else 'unset'}."
            )
        persisted = self._mapping_store.put(mapping)
        for binding in mapping.allowed_client_app_role_bindings:
            self._binding_writer.grant(binding.client_app_id, mapping.deployment_id, mapping.revision_id)
        return persisted

    def revoke(self, revoking_revision: RuntimeDeploymentMapping, now: datetime) -> RuntimeDeploymentMapping:
        """Withdraw authority for ``revoking_revision``'s deployment.

        ``revoking_revision`` is a NEW revision (never the active document
        mutated) that records the revocation: ``revoked_at`` must be set and must
        not be future-dated against the injected ``now``. Fail-closed ordering:
        every bound ``client_app_id`` is UNBOUND first (authority withdrawn
        immediately), THEN the revoking revision is persisted for durable
        lineage. Because no binding points at it, the revoking revision is purely
        an immutable audit record and the deployment is unreachable the moment
        its bindings are removed.
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
            self._binding_writer.revoke(binding.client_app_id, revoking_revision.deployment_id)
        return self._mapping_store.put(revoking_revision)
