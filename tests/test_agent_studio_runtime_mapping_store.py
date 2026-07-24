from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeOperationRef,
)
from research_assistant_api.agent_studio.runtime_mapping_store import (
    CosmosRuntimeDeploymentMappingStore,
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeMappingConflictError,
)


def _mapping(
    *, deployment_id: str = "dep-1", backend_version: str = "1.2.3", revision_sequence: int = 1
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=RuntimeOperationRef(id="search", version="1"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-store-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="backend-release-1",
        backend_version=backend_version,
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id="client-app-1", app_role="research-assistant.runtime"),
        ),
        revision_sequence=revision_sequence,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


def test_get_returns_none_for_unknown_sequence() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    assert store.get("missing", 1) is None


def test_put_then_get_round_trips_by_deployment_and_sequence() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping(deployment_id="dep-xyz")
    returned = store.put(mapping)
    assert returned is mapping
    assert store.get("dep-xyz", 1) is mapping


def test_get_with_wrong_sequence_is_none() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.put(_mapping(deployment_id="dep-1", revision_sequence=1))
    assert store.get("dep-1", 2) is None


def test_put_is_idempotent_for_identical_content() -> None:
    # A control-plane retry of the identical caller-supplied payload lands on the
    # same deployment_id:sequence with the SAME digest -> idempotent (the branch
    # R4 showed was previously unreachable).
    store = InMemoryRuntimeDeploymentMappingStore()
    first = _mapping(deployment_id="dep-1", revision_sequence=1)
    store.put(first)
    second = _mapping(deployment_id="dep-1", revision_sequence=1)
    assert first.mapping_digest == second.mapping_digest
    assert store.put(second) is first
    assert store.get("dep-1", 1) is first


def test_put_rejects_diverging_content_at_same_sequence() -> None:
    # Store-adjudicated single succession: a DIFFERENT content at an occupied
    # sequence is a forged/racing competitor, refused (never a silent overwrite).
    store = InMemoryRuntimeDeploymentMappingStore()
    store.put(_mapping(deployment_id="dep-1", revision_sequence=1, backend_version="1.2.3"))
    with pytest.raises(RuntimeMappingConflictError, match="different content"):
        store.put(_mapping(deployment_id="dep-1", revision_sequence=1, backend_version="9.9.9"))


def test_distinct_sequences_of_one_deployment_coexist() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-1", revision_sequence=1, backend_version="1.2.3")
    b = _mapping(deployment_id="dep-1", revision_sequence=2, backend_version="9.9.9")
    store.put(a)
    store.put(b)
    assert store.get("dep-1", 1) is a
    assert store.get("dep-1", 2) is b


def test_distinct_deployment_ids_are_isolated() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-a")
    b = _mapping(deployment_id="dep-b")
    store.put(a)
    store.put(b)
    assert store.get("dep-a", 1) is a
    assert store.get("dep-b", 1) is b


# --- Cosmos adapter --------------------------------------------------------


class _FakeContainer:
    """Minimal Cosmos container double honoring create_item/read_item semantics."""

    def __init__(self, *, read_returns_none_on_conflict: bool = False, create_status: int = 409) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self._read_returns_none_on_conflict = read_returns_none_on_conflict
        self._create_status = create_status

    def create_item(self, body: dict[str, object]) -> dict[str, object]:
        item_id = str(body["id"])
        if item_id in self.items:
            raise CosmosHttpResponseError(status_code=self._create_status, message="conflict")  # type: ignore[no-untyped-call]
        self.items[item_id] = dict(body)
        return dict(body)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
        assert item.startswith(f"{partition_key}:")
        if item not in self.items or (self._read_returns_none_on_conflict and item in self.items):
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])


def test_cosmos_get_returns_none_when_absent() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    assert store.get("missing", 1) is None


def test_cosmos_put_then_get_round_trips() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    assert store.put(mapping) is mapping
    loaded = store.get("dep-1", 1)
    assert loaded is not None
    assert loaded.mapping_digest == mapping.mapping_digest


def test_cosmos_put_is_idempotent_for_identical_content() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    first = _mapping(deployment_id="dep-1")
    store.put(first)
    second = _mapping(deployment_id="dep-1")
    returned = store.put(second)
    assert returned is not None
    assert returned.mapping_digest == first.mapping_digest


def test_cosmos_distinct_sequences_coexist() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.put(_mapping(deployment_id="dep-1", revision_sequence=1, backend_version="1.2.3"))
    store.put(_mapping(deployment_id="dep-1", revision_sequence=2, backend_version="9.9.9"))
    assert store.get("dep-1", 1) is not None
    assert store.get("dep-1", 2) is not None


def test_cosmos_put_conflict_but_vanished_existing_is_conflict() -> None:
    container = _FakeContainer(read_returns_none_on_conflict=True)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    container.items["dep-1:1"] = {"id": "dep-1:1"}
    with pytest.raises(RuntimeMappingConflictError):
        store.put(mapping)


def test_cosmos_put_conflict_with_diverging_content_is_conflict() -> None:
    # 409 on create and the stored revision at this sequence hashes to a
    # DIFFERENT digest (a forged/racing competitor): fail closed.
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1", revision_sequence=1, backend_version="1.2.3")
    other_payload = _mapping(deployment_id="dep-1", revision_sequence=1, backend_version="9.9.9").model_dump(
        mode="json"
    )
    container.items["dep-1:1"] = {"id": "dep-1:1", "payload": other_payload}
    with pytest.raises(RuntimeMappingConflictError, match="different content"):
        store.put(mapping)


def test_cosmos_put_reraises_non_conflict_error() -> None:
    container = _FakeContainer(create_status=503)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    container.items["dep-1:1"] = {"id": "dep-1:1"}
    with pytest.raises(CosmosHttpResponseError):
        store.put(mapping)
