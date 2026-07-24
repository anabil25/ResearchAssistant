from __future__ import annotations

from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    AuthorizedMappingLoader,
    InMemoryClientDeploymentBindingResolver,
    build_authorized_mapping_loader,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeOperationRef,
)
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore

CLIENT = "client-app-1"


def _mapping(*, deployment_id: str = "dep-1") -> RuntimeDeploymentMapping:
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
        logical_agent_id="agent-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="release-1",
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=CLIENT, app_role="research-assistant.runtime"),
        ),
        created_by="release-service",
    )


def test_resolver_returns_none_for_unbound_client() -> None:
    resolver = InMemoryClientDeploymentBindingResolver()
    assert resolver.authorized_deployment_id("nobody") is None


def test_resolver_binds_and_resolves() -> None:
    resolver = InMemoryClientDeploymentBindingResolver()
    resolver.bind(CLIENT, "dep-1")
    assert resolver.authorized_deployment_id(CLIENT) == "dep-1"


def test_resolver_revoke_makes_old_reference_fail() -> None:
    resolver = InMemoryClientDeploymentBindingResolver()
    resolver.bind(CLIENT, "dep-1")
    resolver.revoke(CLIENT)
    assert resolver.authorized_deployment_id(CLIENT) is None


def test_resolver_rebind_supersedes_old_deployment() -> None:
    resolver = InMemoryClientDeploymentBindingResolver()
    resolver.bind(CLIENT, "dep-1")
    resolver.bind(CLIENT, "dep-2")
    assert resolver.authorized_deployment_id(CLIENT) == "dep-2"


def _loader() -> tuple[AuthorizedMappingLoader, RuntimeDeploymentMapping]:
    resolver = InMemoryClientDeploymentBindingResolver()
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping()
    store.put(mapping)
    resolver.bind(CLIENT, "dep-1")
    return build_authorized_mapping_loader(resolver, store), mapping


def test_loader_returns_mapping_for_bound_client_and_deployment() -> None:
    load, mapping = _loader()
    assert load(CLIENT, "dep-1") is mapping


def test_loader_returns_none_when_client_unbound() -> None:
    load, _mapping = _loader()
    assert load("stranger", "dep-1") is None


def test_loader_returns_none_when_deployment_not_the_bound_one() -> None:
    load, _mapping = _loader()
    assert load(CLIENT, "dep-elsewhere") is None


def test_loader_returns_none_when_mapping_absent_even_if_bound() -> None:
    resolver = InMemoryClientDeploymentBindingResolver()
    store = InMemoryRuntimeDeploymentMappingStore()
    resolver.bind(CLIENT, "dep-1")  # bound, but store has no such mapping
    load = build_authorized_mapping_loader(resolver, store)
    assert load(CLIENT, "dep-1") is None
