from __future__ import annotations

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
        logical_agent_id="agent-1",
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
        created_by="release-service",
    )


def test_get_returns_none_for_unknown_deployment() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    assert store.get("missing") is None


def test_put_then_get_round_trips_by_opaque_deployment_id() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping(deployment_id="dep-xyz")
    returned = store.put(mapping)
    assert returned is mapping
    assert store.get("dep-xyz") is mapping


def test_put_is_idempotent_for_identical_content() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    first = _mapping(deployment_id="dep-1")
    store.put(first)
    # A structurally-identical mapping (same digest) may be re-put.
    second = _mapping(deployment_id="dep-1")
    assert first.mapping_digest == second.mapping_digest
    store.put(second)
    assert store.get("dep-1") is second


def test_put_rejects_diverging_content_for_same_deployment_id() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.put(_mapping(deployment_id="dep-1", backend_version="1.2.3"))
    with pytest.raises(RuntimeMappingConflictError, match="already exists with different content"):
        store.put(_mapping(deployment_id="dep-1", backend_version="9.9.9"))


def test_distinct_deployment_ids_are_isolated() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-a")
    b = _mapping(deployment_id="dep-b")
    store.put(a)
    store.put(b)
    assert store.get("dep-a") is a
    assert store.get("dep-b") is b


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
        assert item == partition_key  # partitioned by /deployment_id
        if item not in self.items or (self._read_returns_none_on_conflict and item in self.items):
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])


def test_cosmos_get_returns_none_when_absent() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    assert store.get("missing") is None


def test_cosmos_put_then_get_round_trips() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    assert store.put(mapping) is mapping
    loaded = store.get("dep-1")
    assert loaded is not None
    assert loaded.mapping_digest == mapping.mapping_digest
    assert loaded.deployment_id == "dep-1"


def test_cosmos_put_is_idempotent_for_identical_content() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.put(_mapping(deployment_id="dep-1"))
    returned = store.put(_mapping(deployment_id="dep-1"))
    assert returned is not None
    assert returned.deployment_id == "dep-1"


def test_cosmos_put_rejects_diverging_content() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.put(_mapping(deployment_id="dep-1", backend_version="1.2.3"))
    with pytest.raises(RuntimeMappingConflictError, match="already exists with different content"):
        store.put(_mapping(deployment_id="dep-1", backend_version="9.9.9"))


def test_cosmos_put_conflict_but_vanished_existing_is_conflict() -> None:
    # 409 on create, but the existing doc is gone by the time we re-read
    # (concurrent delete): fail closed as a conflict, never silently succeed.
    container = _FakeContainer(read_returns_none_on_conflict=True)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    container.items["dep-1"] = {"id": "dep-1"}  # force create_item to 409
    with pytest.raises(RuntimeMappingConflictError):
        store.put(_mapping(deployment_id="dep-1"))


def test_cosmos_put_reraises_non_conflict_error() -> None:
    container = _FakeContainer(create_status=503)
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    container.items["dep-1"] = {"id": "dep-1"}  # force create_item to raise 503
    with pytest.raises(CosmosHttpResponseError):
        store.put(_mapping(deployment_id="dep-1"))
