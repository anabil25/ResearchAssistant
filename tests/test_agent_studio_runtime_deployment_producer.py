from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from research_assistant_api.agent_studio.audit_service import AuditService, InMemoryAuditStore
from research_assistant_api.agent_studio.models import AuditEvent, AuditEventKind, DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    BindingPreconditionError,
    InMemoryClientDeploymentBindingIndex,
    NonMonotonicRepointError,
    RuntimeBindingStatus,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeMappingLifecycleState,
    RuntimeOperationRef,
)
from research_assistant_api.agent_studio.runtime_deployment_producer import (
    FutureDatedRevocationError,
    NonGrantableMappingError,
    NonRevokingMappingError,
    RevisionStillReferencedError,
    RollbackRepointError,
    RuntimeDeploymentProducer,
)
from research_assistant_api.agent_studio.runtime_mapping_store import (
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeMappingConflictError,
)
from research_assistant_api.agent_studio.scope import ScopeContext

CLIENT = "client-app-1"
ACTOR = "control-plane-actor"
NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
SCOPE = ScopeContext(tenant_id="tenant-1", project_id="project-1")


def _mapping(
    *,
    deployment_id: str = "dep-1",
    backend_version: str = "1.2.3",
    revision_sequence: int = 1,
    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE,
    revoked_at: datetime | None = None,
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search"),
        operation_ref=RuntimeOperationRef(id="search"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-producer-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="release-1",
        backend_version=backend_version,
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=CLIENT, app_role="research-assistant.runtime"),
        ),
        lifecycle_state=lifecycle_state,
        revision_sequence=revision_sequence,
        revoked_at=revoked_at,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="control-plane",
    )


def _producer() -> tuple[
    RuntimeDeploymentProducer,
    InMemoryRuntimeDeploymentMappingStore,
    InMemoryClientDeploymentBindingIndex,
    InMemoryAuditStore,
]:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    audit_store = InMemoryAuditStore()
    producer = RuntimeDeploymentProducer(store, index, index, AuditService(audit_store))
    return producer, store, index, audit_store


def _events(audit_store: InMemoryAuditStore, kind: AuditEventKind, phase: str) -> list[AuditEvent]:
    return [e for e in audit_store.list_events(scope=SCOPE, kind=kind) if e.detail.get("phase") == phase]


# --- GRANT -----------------------------------------------------------------


def test_grant_writes_mapping_then_binds_and_audits_intent_and_applied() -> None:
    producer, store, index, audit = _producer()
    mapping = _mapping()
    assert producer.grant(mapping, actor_id=ACTOR, now=NOW) is mapping
    assert store.get("dep-1", 1) is mapping
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.ACTIVE
    # Intent-first: both an intent and an applied audit event exist for the grant.
    intents = _events(audit, AuditEventKind.RUNTIME_BINDING_GRANTED, "intent")
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_GRANTED, "applied")
    assert len(intents) == 1
    assert len(applied) == 1
    assert applied[0].detail["to_revision_sequence"] == "1"
    assert intents[0].detail["intent_id"] == applied[0].detail["intent_id"]


def test_grant_is_idempotent_for_identical_inputs() -> None:
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 1
    assert store.get("dep-1", 1) is not None


def test_grant_advances_and_repoints() -> None:
    producer, _store, index, audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    v2 = _mapping(revision_sequence=2, backend_version="2.0.0")
    producer.grant(v2, actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_REPOINTED, "applied")
    assert len(applied) == 1
    assert applied[0].detail["from_revision_sequence"] == "1"
    assert applied[0].detail["to_revision_sequence"] == "2"


def test_grant_rejects_rollback_to_older_revision() -> None:
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2


def test_grant_rejects_same_sequence_different_content() -> None:
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=2), actor_id=ACTOR, now=NOW)
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=2, backend_version="9.9.9"), actor_id=ACTOR, now=NOW)


def test_grant_rejects_non_active_mapping() -> None:
    producer, store, index, _audit = _producer()
    retired = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED)
    with pytest.raises(NonGrantableMappingError):
        producer.grant(retired, actor_id=ACTOR, now=NOW)
    assert store.get("dep-1", 1) is None
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_grant_rejects_revoked_mapping() -> None:
    producer, _store, _index, _audit = _producer()
    revoked = _mapping(revoked_at=datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC))
    with pytest.raises(NonGrantableMappingError):
        producer.grant(revoked, actor_id=ACTOR, now=NOW)


def test_grant_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.grant(_mapping(), actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


def test_divergent_content_at_same_sequence_is_conflict() -> None:
    # Real mechanism: two control-plane grants construct DIFFERENT content at the
    # same sequence; the store adjudicates and the second is a hard conflict.
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1, backend_version="1.2.3"), actor_id=ACTOR, now=NOW)
    # A different content forced at the SAME sequence (bypassing the successor
    # pre-check by using an unbound second client is not possible under 1:1, so
    # drive the store directly through a fresh producer sharing the store).
    with pytest.raises(RuntimeMappingConflictError):
        _store.put(_mapping(revision_sequence=1, backend_version="9.9.9"))


# --- REVOKE (tombstone) ----------------------------------------------------


def test_revoke_tombstones_binding_then_writes_retiring_revision() -> None:
    producer, store, index, audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    revoking = _mapping(
        revision_sequence=2,
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    persisted = producer.revoke(revoking, actor_id=ACTOR, now=NOW)
    # Authority withdrawn as a TOMBSTONE, not a delete: the row survives REVOKED.
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED
    assert resolution.revision_sequence == 2
    assert store.get("dep-1", 2) is persisted
    assert store.get("dep-1", 1) is active
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_REVOKED, "applied")
    assert len(applied) == 1


def test_revoke_requires_a_revoking_revision() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(NonRevokingMappingError):
        producer.revoke(_mapping(), actor_id=ACTOR, now=NOW)


def test_revoke_rejects_future_dated_revocation() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(FutureDatedRevocationError):
        producer.revoke(_mapping(revoked_at=NOW + timedelta(hours=1)), actor_id=ACTOR, now=NOW)


def test_revoke_permits_revocation_at_the_write_instant() -> None:
    producer, store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    at_now = _mapping(revision_sequence=2, lifecycle_state=RuntimeMappingLifecycleState.RETIRED, revoked_at=NOW)
    producer.revoke(at_now, actor_id=ACTOR, now=NOW)
    assert store.get("dep-1", 2) is not None


def test_revoke_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    revoking = _mapping(revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.revoke(revoking, actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


def test_reconcile_revoke_is_idempotent() -> None:
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    revoking = _mapping(
        revision_sequence=2,
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    producer.revoke(revoking, actor_id=ACTOR, now=NOW)
    producer.reconcile_revoke(revoking, actor_id=ACTOR, now=NOW)  # must not raise
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED
    assert store.get("dep-1", 2) is not None


# --- retention interlock ---------------------------------------------------


def test_retire_revision_refuses_a_referenced_revision() -> None:
    producer, store, _index, _audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    with pytest.raises(RevisionStillReferencedError):
        producer.retire_revision("dep-1", 1, active.revision_id, (CLIENT,))
    assert store.get("dep-1", 1) is active  # not deleted


def test_retire_revision_deletes_an_unreferenced_revision() -> None:
    producer, store, _index, _audit = _producer()
    v1 = _mapping(revision_sequence=1)
    producer.grant(v1, actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    # v1 is superseded (binding now points at seq 2), so it may be retired.
    producer.retire_revision("dep-1", 1, v1.revision_id, (CLIENT,))
    assert store.get("dep-1", 1) is None
    assert store.get("dep-1", 2) is not None


# --- CAS concurrency paths (intent-first, retry, non-convergence) ----------


class _FlakyWriter:
    def __init__(self, index: InMemoryClientDeploymentBindingIndex, fail_times: int) -> None:
        self._index = index
        self._remaining = fail_times

    def repoint(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        status: RuntimeBindingStatus,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise BindingPreconditionError("flaky")
        self._index.repoint(
            client_app_id, deployment_id, revision_sequence, revision_id, status,
            expected_current_sequence=expected_current_sequence,
        )


def test_grant_retries_on_precondition_then_succeeds() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(store, _FlakyWriter(index, 1), index, AuditService(InMemoryAuditStore()))
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    assert index.resolve_binding(CLIENT, "dep-1") is not None


def test_grant_non_convergence_leaves_intent_without_applied() -> None:
    # Intent-first: when the CAS never converges, the INTENT audit event exists
    # but no APPLIED -- a dangling reconciliation signal, not a silent loss.
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    audit_store = InMemoryAuditStore()
    producer = RuntimeDeploymentProducer(store, _FlakyWriter(index, 999), index, AuditService(audit_store))
    with pytest.raises(BindingPreconditionError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_GRANTED, "intent")) == 1
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_GRANTED, "applied")) == 0


class _NonMonotonicWriter:
    def repoint(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        status: RuntimeBindingStatus,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        raise NonMonotonicRepointError("cas-detected rollback")


def test_grant_maps_cas_nonmonotonic_to_rollback() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(store, _NonMonotonicWriter(), index, AuditService(InMemoryAuditStore()))
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
