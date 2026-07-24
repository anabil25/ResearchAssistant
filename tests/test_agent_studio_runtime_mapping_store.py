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


def _mapping(*, deployment_id: str = "dep-1", backend_version: str = "1.2.3") -> RuntimeDeploymentMapping:
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
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


def test_get_returns_none_for_unknown_revision() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    assert store.get("missing", "no-such-revision") is None


def test_put_then_get_round_trips_by_deployment_and_revision() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping(deployment_id="dep-xyz")
    returned = store.put(mapping)
    assert returned is mapping
    assert store.get("dep-xyz", mapping.revision_id) is mapping


def test_get_with_wrong_revision_is_none() -> None:
    # Exact point read: a present deployment but an unknown revision is a miss,
    # never a fall-through to "latest".
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping(deployment_id="dep-1")
    store.put(mapping)
    assert store.get("dep-1", "some-other-revision") is None


def test_put_is_idempotent_for_identical_content() -> None:
    # Identical inputs materialize the SAME revision id; a re-put retains and
    # returns the first-stored revision (the control-plane idempotent-retry path).
    store = InMemoryRuntimeDeploymentMappingStore()
    first = _mapping(deployment_id="dep-1")
    store.put(first)
    second = _mapping(deployment_id="dep-1")
    assert first.mapping_digest == second.mapping_digest
    assert first.revision_id == second.revision_id
    assert store.put(second) is first
    assert store.get("dep-1", second.revision_id) is first


def test_distinct_revisions_of_one_deployment_coexist() -> None:
    # The revision model: two revisions of one deployment_id have distinct
    # revision ids and both remain point-readable (supersession without conflict).
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-1", backend_version="1.2.3")
    b = _mapping(deployment_id="dep-1", backend_version="9.9.9")
    assert a.revision_id != b.revision_id
    store.put(a)
    store.put(b)
    assert store.get("dep-1", a.revision_id) is a
    assert store.get("dep-1", b.revision_id) is b


def test_distinct_deployment_ids_are_isolated() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-a")
    b = _mapping(deployment_id="dep-b")
    store.put(a)
    store.put(b)
    assert store.get("dep-a", a.revision_id) is a
    assert store.get("dep-b", b.revision_id) is b


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
        # Item id carries the revision; the partition key is the deployment_id.
        assert item.startswith(f"{partition_key}::")
        if item not in self.items or (self._read_returns_none_on_conflict and item in self.items):
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])


def test_cosmos_get_returns_none_when_absent() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    assert store.get("missing", "no-such-revision") is None


def test_cosmos_put_then_get_round_trips() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    assert store.put(mapping) is mapping
    loaded = store.get("dep-1", mapping.revision_id)
    assert loaded is not None
    assert loaded.mapping_digest == mapping.mapping_digest
    assert loaded.deployment_id == "dep-1"


def test_cosmos_put_is_idempotent_for_identical_content() -> None:
    # Real mechanism: two constructions from identical inputs materialize the
    # SAME revision id, so the second create 409s and the store returns the
    # stored revision -- the idempotent-retry path a control-plane retry needs.
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    first = _mapping(deployment_id="dep-1")
    store.put(first)
    second = _mapping(deployment_id="dep-1")
    assert first.revision_id == second.revision_id
    returned = store.put(second)
    assert returned is not None
    assert returned.deployment_id == "dep-1"
    assert returned.mapping_digest == first.mapping_digest


def test_cosmos_distinct_revisions_coexist() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    a = _mapping(deployment_id="dep-1", backend_version="1.2.3")
    b = _mapping(deployment_id="dep-1", backend_version="9.9.9")
    store.put(a)
    store.put(b)  # different revision id -> new item, never a conflict
    assert store.get("dep-1", a.revision_id) is not None
    assert store.get("dep-1", b.revision_id) is not None


def test_cosmos_put_conflict_but_vanished_existing_is_conflict() -> None:
    # 409 on create, but the existing revision is gone by the time we re-read
    # (concurrent delete): fail closed as a conflict, never silently succeed.
    container = _FakeContainer(read_returns_none_on_conflict=True)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    container.items[f"dep-1::{mapping.revision_id}"] = {"id": f"dep-1::{mapping.revision_id}"}
    with pytest.raises(RuntimeMappingConflictError):
        store.put(mapping)


def test_cosmos_put_conflict_with_tampered_stored_payload_is_conflict() -> None:
    # 409 on create and the stored revision is present but its payload hashes to
    # a DIFFERENT digest than the id claims (out-of-band tampering/corruption):
    # fail closed rather than treat it as an idempotent re-put.
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1", backend_version="1.2.3")
    tampered_payload = _mapping(deployment_id="dep-1", backend_version="9.9.9").model_dump(mode="json")
    item_id = f"dep-1::{mapping.revision_id}"
    container.items[item_id] = {"id": item_id, "payload": tampered_payload}
    with pytest.raises(RuntimeMappingConflictError, match="already exists with different content"):
        store.put(mapping)


def test_cosmos_put_reraises_non_conflict_error() -> None:
    container = _FakeContainer(create_status=503)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    container.items[f"dep-1::{mapping.revision_id}"] = {"id": f"dep-1::{mapping.revision_id}"}
    with pytest.raises(CosmosHttpResponseError):
        store.put(mapping)
