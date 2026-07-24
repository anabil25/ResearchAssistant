from __future__ import annotations

from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    AuthorizedMappingLoader,
    InMemoryClientDeploymentBindingIndex,
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


# --- exact-membership resolver ---------------------------------------------


def test_unbound_client_is_not_bound() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    assert index.is_bound("nobody", "dep-1") is False


def test_grant_makes_exact_pair_bound() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1")
    assert index.is_bound(CLIENT, "dep-1") is True
    # Exact membership: a different deployment for the same client is NOT bound.
    assert index.is_bound(CLIENT, "dep-2") is False


def test_client_may_hold_multiple_bindings() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1")
    index.grant(CLIENT, "dep-2")
    assert index.is_bound(CLIENT, "dep-1") is True
    assert index.is_bound(CLIENT, "dep-2") is True
    assert index.is_bound(CLIENT, "dep-3") is False


def test_revoke_removes_only_that_pair() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1")
    index.grant(CLIENT, "dep-2")
    index.revoke(CLIENT, "dep-1")
    assert index.is_bound(CLIENT, "dep-1") is False
    assert index.is_bound(CLIENT, "dep-2") is True


def test_revoke_last_binding_drops_the_client() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1")
    index.revoke(CLIENT, "dep-1")
    assert index.is_bound(CLIENT, "dep-1") is False


def test_revoke_unknown_client_is_noop() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.revoke("nobody", "dep-1")  # must not raise
    assert index.is_bound("nobody", "dep-1") is False


# --- authorized loader -----------------------------------------------------


def _loader(
    *, bound: bool = True, mapping_present: bool = True
) -> tuple[AuthorizedMappingLoader, RuntimeDeploymentMapping]:
    index = InMemoryClientDeploymentBindingIndex()
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping()
    if mapping_present:
        store.put(mapping)
    if bound:
        index.grant(CLIENT, "dep-1")
    return build_authorized_mapping_loader(index, store), mapping


def test_loader_returns_mapping_for_bound_client_and_deployment() -> None:
    load, mapping = _loader()
    assert load(CLIENT, "dep-1") is mapping


def test_loader_returns_none_when_client_unbound() -> None:
    load, _mapping = _loader(bound=False)
    assert load(CLIENT, "dep-1") is None


def test_loader_returns_none_for_bound_client_but_wrong_deployment() -> None:
    load, _mapping = _loader()
    assert load(CLIENT, "dep-elsewhere") is None


def test_loader_returns_none_when_bound_but_mapping_absent() -> None:
    # Binding-without-mapping is a denial, never repairable (fail-closed).
    load, _mapping = _loader(mapping_present=False)
    assert load(CLIENT, "dep-1") is None
