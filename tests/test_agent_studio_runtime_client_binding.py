from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    AuthorizedMappingLoader,
    BindingResolution,
    ClientDeploymentBindingResolver,
    CosmosClientDeploymentBindingIndex,
    InMemoryClientDeploymentBindingIndex,
    RuntimeBindingStatus,
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


def _mapping(*, deployment_id: str = "dep-1", backend_version: str = "1.2.3") -> RuntimeDeploymentMapping:
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
        logical_agent_id="agent-binding-1",
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
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


REV = _mapping().revision_id


# --- exact-membership resolver (one-to-one, carrying the current revision) ---


def test_unbound_client_resolves_to_none() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    assert index.resolve_binding("nobody", "dep-1") is None


def test_grant_binds_exact_pair_to_a_revision() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution == BindingResolution(deployment_id="dep-1", revision_id=REV, status=RuntimeBindingStatus.ACTIVE)


def test_resolve_wrong_deployment_is_none() -> None:
    # Exact membership: the asserted deployment must match the bound one.
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    assert index.resolve_binding(CLIENT, "dep-2") is None


def test_grant_is_one_to_one_and_replaces() -> None:
    # One client -> exactly one deployment; a new grant replaces the old binding.
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-2", "revision-2")
    assert index.resolve_binding(CLIENT, "dep-1") is None
    resolution = index.resolve_binding(CLIENT, "dep-2")
    assert resolution is not None
    assert resolution.deployment_id == "dep-2"
    assert resolution.revision_id == "revision-2"


def test_regrant_same_deployment_repoints_revision() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-1", "revision-2")
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_id == "revision-2"


def test_revoke_removes_matching_binding() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.revoke(CLIENT, "dep-1")
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_revoke_mismatched_deployment_is_noop() -> None:
    # A revoke targeting a deployment the client is not bound to must not clobber
    # its actual binding (guards against clobbering a re-grant).
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.revoke(CLIENT, "dep-2")
    assert index.resolve_binding(CLIENT, "dep-1") is not None


def test_revoke_unknown_client_is_noop() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.revoke("nobody", "dep-1")  # must not raise
    assert index.resolve_binding("nobody", "dep-1") is None


# --- authorized loader -----------------------------------------------------


class _CountingStore(InMemoryRuntimeDeploymentMappingStore):
    """Store spy that counts point-reads, to prove denied callers read zero."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get(self, deployment_id: str, revision_id: str) -> RuntimeDeploymentMapping | None:
        self.get_calls += 1
        return super().get(deployment_id, revision_id)


def _loader(
    *, bound: bool = True, mapping_present: bool = True, bound_revision: str | None = None
) -> tuple[AuthorizedMappingLoader, RuntimeDeploymentMapping, _CountingStore]:
    index = InMemoryClientDeploymentBindingIndex()
    store = _CountingStore()
    mapping = _mapping()
    if mapping_present:
        store.put(mapping)
    if bound:
        index.grant(CLIENT, "dep-1", bound_revision or mapping.revision_id)
    return build_authorized_mapping_loader(index, store), mapping, store


def test_loader_returns_mapping_for_bound_client_and_current_revision() -> None:
    load, mapping, _store = _loader()
    assert load(CLIENT, "dep-1") is mapping


def test_loader_returns_none_when_client_unbound_and_reads_zero_mappings() -> None:
    # Ratified constraint 3: an unbound caller performs ZERO reads against the
    # mapping container -- no cross-tenant datastore read, no RU cost.
    load, _mapping, store = _loader(bound=False)
    assert load(CLIENT, "dep-1") is None
    assert store.get_calls == 0


def test_loader_returns_none_for_bound_client_but_wrong_deployment() -> None:
    load, _mapping, store = _loader()
    assert load(CLIENT, "dep-elsewhere") is None
    assert store.get_calls == 0  # wrong-deployment pair short-circuits before any read


def test_loader_returns_none_when_bound_but_revision_absent() -> None:
    # Binding-without-mapping is a denial, never repairable (fail-closed
    # reconciliation): the binding points at a revision the store does not hold.
    load, _mapping, _store = _loader(mapping_present=False)
    assert load(CLIENT, "dep-1") is None


def test_loader_returns_none_when_binding_points_at_stale_revision() -> None:
    load, _mapping, _store = _loader(bound_revision="revision-that-was-never-written")
    assert load(CLIENT, "dep-1") is None


class _RevokedResolver:
    """A resolver that returns a soft-revoked (tombstoned) binding."""

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        return BindingResolution(
            deployment_id=asserted_deployment_id, revision_id=REV, status=RuntimeBindingStatus.REVOKED
        )


def test_loader_denies_soft_revoked_binding_without_reading_mapping() -> None:
    # A present-but-revoked binding denies, and denies WITHOUT a mapping read.
    store = _CountingStore()
    store.put(_mapping())
    resolver: ClientDeploymentBindingResolver = _RevokedResolver()
    load = build_authorized_mapping_loader(resolver, store)
    assert load(CLIENT, "dep-1") is None
    assert store.get_calls == 0


# --- durable Cosmos binding adapter ----------------------------------------


class _FakeBindingContainer:
    """Minimal Cosmos container double for the binding index (upsert/read/delete)."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    def upsert_item(self, body: dict[str, object]) -> dict[str, object]:
        self.items[str(body["id"])] = dict(body)
        return dict(body)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
        assert item == partition_key  # one item per client, id == client_app_id
        if item not in self.items:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])

    def delete_item(self, *, item: str, partition_key: str) -> None:
        assert item == partition_key
        del self.items[item]


def test_cosmos_binding_grant_then_resolve() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.grant(CLIENT, "dep-1", REV)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution == BindingResolution(deployment_id="dep-1", revision_id=REV, status=RuntimeBindingStatus.ACTIVE)


def test_cosmos_binding_unknown_client_is_none() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_cosmos_binding_resolve_wrong_deployment_is_none() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.grant(CLIENT, "dep-1", REV)
    assert index.resolve_binding(CLIENT, "dep-2") is None


def test_cosmos_binding_grant_repoints_and_replaces() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-2", "revision-2")
    assert index.resolve_binding(CLIENT, "dep-1") is None
    resolution = index.resolve_binding(CLIENT, "dep-2")
    assert resolution is not None
    assert resolution.revision_id == "revision-2"


def test_cosmos_binding_revoke_removes_matching() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.grant(CLIENT, "dep-1", REV)
    index.revoke(CLIENT, "dep-1")
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_cosmos_binding_revoke_mismatched_deployment_is_noop() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.grant(CLIENT, "dep-1", REV)
    index.revoke(CLIENT, "dep-2")  # different deployment -> must not delete
    assert index.resolve_binding(CLIENT, "dep-1") is not None


def test_cosmos_binding_revoke_missing_is_idempotent_noop() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.revoke(CLIENT, "dep-1")  # must not raise
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_cosmos_binding_soft_revoked_tombstone_resolves_revoked() -> None:
    # A soft-revoked row (status=revoked) resolves with REVOKED so the loader
    # denies; the durable adapter faithfully surfaces the tombstone status.
    container = _FakeBindingContainer()
    container.items[CLIENT] = {
        "id": CLIENT,
        "client_app_id": CLIENT,
        "deployment_id": "dep-1",
        "current_revision_id": REV,
        "status": "revoked",
    }
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, container))
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED


def test_cosmos_binding_backs_the_authorized_loader() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping()
    store.put(mapping)
    index.grant(CLIENT, "dep-1", mapping.revision_id)
    load = build_authorized_mapping_loader(index, store)
    assert load(CLIENT, "dep-1") is mapping
    assert load("other-client", "dep-1") is None
