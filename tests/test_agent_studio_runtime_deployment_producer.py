from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from research_assistant_api.agent_studio.audit_service import AuditService, InMemoryAuditStore
from research_assistant_api.agent_studio.models import AuditEvent, AuditEventKind, DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import InMemoryClientDeploymentBindingIndex
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
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore
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


def _events(audit_store: InMemoryAuditStore, kind: AuditEventKind | None = None) -> tuple[AuditEvent, ...]:
    return audit_store.list_events(scope=SCOPE, kind=kind)


# --- GRANT -----------------------------------------------------------------


def test_grant_writes_mapping_then_binds_and_audits() -> None:
    producer, store, index, audit = _producer()
    mapping = _mapping()
    returned = producer.grant(mapping, actor_id=ACTOR, now=NOW)
    assert returned is mapping
    assert store.get("dep-1", mapping.revision_id) is mapping
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_id == mapping.revision_id
    assert resolution.revision_sequence == 1
    granted = _events(audit, AuditEventKind.RUNTIME_BINDING_GRANTED)
    assert len(granted) == 1
    assert granted[0].detail["to_revision_sequence"] == "1"
    assert granted[0].detail["from_revision_sequence"] == ""
    assert granted[0].actor_id == ACTOR


def test_grant_is_idempotent_for_identical_inputs() -> None:
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(), actor_id=ACTOR, now=NOW)
    reconstructed = _mapping()
    # Same revision id -> idempotent re-grant is allowed despite equal sequence.
    producer.grant(reconstructed, actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_id == reconstructed.revision_id
    assert store.get("dep-1", reconstructed.revision_id) is not None


def test_grant_advances_to_a_greater_revision_and_repoints() -> None:
    producer, _store, index, audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    v2 = _mapping(revision_sequence=2, backend_version="2.0.0")
    producer.grant(v2, actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2
    assert resolution.revision_id == v2.revision_id
    repointed = _events(audit, AuditEventKind.RUNTIME_BINDING_REPOINTED)
    assert len(repointed) == 1
    assert repointed[0].detail["from_revision_sequence"] == "1"
    assert repointed[0].detail["to_revision_sequence"] == "2"


def test_grant_rejects_rollback_to_older_revision() -> None:
    # The core rollback control: after advancing to seq 2, a grant of an OLDER
    # revision (seq 1, different content) is refused at the sole index writer.
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    older = _mapping(revision_sequence=1)
    with pytest.raises(RollbackRepointError):
        producer.grant(older, actor_id=ACTOR, now=NOW)
    # Pointer unchanged.
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2


def test_grant_rejects_same_sequence_different_content() -> None:
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=2), actor_id=ACTOR, now=NOW)
    ambiguous = _mapping(revision_sequence=2, backend_version="9.9.9")
    with pytest.raises(RollbackRepointError):
        producer.grant(ambiguous, actor_id=ACTOR, now=NOW)


def test_grant_rejects_non_active_mapping() -> None:
    producer, store, index, _audit = _producer()
    retired = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED)
    with pytest.raises(NonGrantableMappingError):
        producer.grant(retired, actor_id=ACTOR, now=NOW)
    assert store.get("dep-1", retired.revision_id) is None
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


# --- REVOKE ----------------------------------------------------------------


def test_revoke_unbinds_first_then_writes_retiring_revision_and_audits() -> None:
    producer, store, index, audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    revoking = _mapping(
        revision_sequence=2,
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    persisted = producer.revoke(revoking, actor_id=ACTOR, now=NOW)
    assert index.resolve_binding(CLIENT, "dep-1") is None
    assert store.get("dep-1", revoking.revision_id) is persisted
    assert store.get("dep-1", active.revision_id) is active
    revoked_events = _events(audit, AuditEventKind.RUNTIME_BINDING_REVOKED)
    assert len(revoked_events) == 1
    assert revoked_events[0].detail["from_revision_sequence"] == "1"


def test_revoke_requires_a_revoking_revision() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(NonRevokingMappingError):
        producer.revoke(_mapping(), actor_id=ACTOR, now=NOW)


def test_revoke_rejects_future_dated_revocation() -> None:
    producer, _store, _index, _audit = _producer()
    future = _mapping(revoked_at=NOW + timedelta(hours=1))
    with pytest.raises(FutureDatedRevocationError):
        producer.revoke(future, actor_id=ACTOR, now=NOW)


def test_revoke_permits_revocation_at_the_write_instant() -> None:
    producer, store, _index, _audit = _producer()
    at_now = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED, revoked_at=NOW)
    persisted = producer.revoke(at_now, actor_id=ACTOR, now=NOW)
    assert store.get("dep-1", at_now.revision_id) is persisted


def test_revoke_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    revoking = _mapping(revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.revoke(revoking, actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


# --- reconciliation & retention (Flaw D) -----------------------------------


def test_reconcile_revoke_completes_a_failed_retiring_write() -> None:
    # Simulate a REVOKE whose retiring-revision write failed: the binding is gone
    # but the retiring revision was never persisted. Reconciliation completes it.
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    revoking = _mapping(
        revision_sequence=2,
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    # Manually unbind to model "first write succeeded, second failed".
    index.revoke(CLIENT, "dep-1")
    assert store.get("dep-1", revoking.revision_id) is None
    producer.reconcile_revoke(revoking, actor_id=ACTOR, now=NOW)
    assert store.get("dep-1", revoking.revision_id) is not None
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_assert_safe_to_retire_refuses_a_referenced_revision() -> None:
    producer, _store, _index, _audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    with pytest.raises(RevisionStillReferencedError):
        producer.assert_safe_to_retire("dep-1", active.revision_id, (CLIENT,))


def test_assert_safe_to_retire_allows_an_unreferenced_revision() -> None:
    producer, _store, _index, _audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    # A different (older/superseded) revision id is not referenced by the pointer.
    producer.assert_safe_to_retire("dep-1", "some-older-revision-id", (CLIENT,))
