from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptorRef,
    CapabilityOperationRef,
    DeploymentEnvironment,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDestinationHashPolicy,
)
from research_assistant_api.agent_studio.runtime_mapping_store import (
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeMappingConflictError,
)


def _mapping(*, deployment_id: str = "dep-1", backend_version: str = "1.2.3") -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=CapabilityDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=CapabilityOperationRef(id="search", version="1"),
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
