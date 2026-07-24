from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from research_assistant_api.agent_studio.models import DeploymentEnvironment
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
    RuntimeDeploymentProducer,
)
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore

CLIENT = "client-app-1"
NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)


def _mapping(
    *,
    deployment_id: str = "dep-1",
    backend_version: str = "1.2.3",
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
        revoked_at=revoked_at,
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="control-plane",
    )


def _producer() -> tuple[
    RuntimeDeploymentProducer, InMemoryRuntimeDeploymentMappingStore, InMemoryClientDeploymentBindingIndex
]:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    return RuntimeDeploymentProducer(store, index), store, index


# --- GRANT -----------------------------------------------------------------


def test_grant_writes_mapping_then_binds_current_revision() -> None:
    producer, store, index = _producer()
    mapping = _mapping()
    returned = producer.grant(mapping)
    assert returned is mapping
    # Mapping revision landed first...
    assert store.get("dep-1", mapping.revision_id) is mapping
    # ...then the binding points at exactly that revision.
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_id == mapping.revision_id


def test_grant_is_idempotent_for_identical_inputs() -> None:
    # The real-mechanism idempotency proof: a mapping re-materialized from
    # identical inputs round-trips as idempotent through the store, and the
    # binding still resolves to the same current revision.
    producer, store, index = _producer()
    producer.grant(_mapping())
    reconstructed = _mapping()
    producer.grant(reconstructed)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_id == reconstructed.revision_id
    assert store.get("dep-1", reconstructed.revision_id) is not None


def test_grant_rejects_non_active_mapping() -> None:
    producer, store, index = _producer()
    retired = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED)
    with pytest.raises(NonGrantableMappingError):
        producer.grant(retired)
    # Nothing written, nothing bound (fail closed before any side effect).
    assert store.get("dep-1", retired.revision_id) is None
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_grant_rejects_revoked_mapping() -> None:
    producer, _store, _index = _producer()
    revoked = _mapping(revoked_at=datetime(2026, 1, 1, 13, 0, 0, tzinfo=UTC))
    with pytest.raises(NonGrantableMappingError):
        producer.grant(revoked)


# --- REVOKE ----------------------------------------------------------------


def test_revoke_unbinds_first_then_writes_retiring_revision() -> None:
    producer, store, index = _producer()
    active = _mapping()
    producer.grant(active)
    revoking = _mapping(
        backend_version="1.2.3",
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    persisted = producer.revoke(revoking, now=NOW)
    # Authority withdrawn: the binding is gone.
    assert index.resolve_binding(CLIENT, "dep-1") is None
    # The revoking revision is a NEW immutable item (distinct revision id).
    assert revoking.revision_id != active.revision_id
    assert store.get("dep-1", revoking.revision_id) is persisted
    # ...and the original active revision remains point-readable for lineage.
    assert store.get("dep-1", active.revision_id) is active


def test_revoke_requires_a_revoking_revision() -> None:
    producer, _store, _index = _producer()
    not_revoking = _mapping()  # revoked_at unset
    with pytest.raises(NonRevokingMappingError):
        producer.revoke(not_revoking, now=NOW)


def test_revoke_rejects_future_dated_revocation() -> None:
    producer, _store, _index = _producer()
    future = _mapping(revoked_at=NOW + timedelta(hours=1))
    with pytest.raises(FutureDatedRevocationError):
        producer.revoke(future, now=NOW)


def test_revoke_permits_revocation_at_the_write_instant() -> None:
    producer, store, _index = _producer()
    at_now = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED, revoked_at=NOW)
    persisted = producer.revoke(at_now, now=NOW)
    assert store.get("dep-1", at_now.revision_id) is persisted


def test_revoke_rejects_naive_now() -> None:
    producer, _store, _index = _producer()
    revoking = _mapping(revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC))
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.revoke(revoking, now=datetime(2026, 1, 2, 12, 0, 0))


def test_revoke_normalizes_aware_non_utc_now() -> None:
    from datetime import timezone

    producer, store, _index = _producer()
    revoking = _mapping(
        lifecycle_state=RuntimeMappingLifecycleState.RETIRED,
        revoked_at=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC),
    )
    # now expressed in +02:00 for the same instant as NOW must be accepted.
    now_plus2 = NOW.astimezone(timezone(timedelta(hours=2)))
    persisted = producer.revoke(revoking, now=now_plus2)
    assert store.get("dep-1", revoking.revision_id) is persisted
