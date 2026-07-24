from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    AuthorizedMappingLoader,
    BindingPreconditionError,
    BindingResolution,
    ClientDeploymentBindingResolver,
    CosmosClientDeploymentBindingIndex,
    InMemoryClientDeploymentBindingIndex,
    NonMonotonicRepointError,
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
ACTIVE = RuntimeBindingStatus.ACTIVE
REVOKED = RuntimeBindingStatus.REVOKED


def _mapping(*, deployment_id: str = "dep-1", revision_sequence: int = 1) -> RuntimeDeploymentMapping:
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
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=CLIENT, app_role="research-assistant.runtime"),
        ),
        revision_sequence=revision_sequence,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


REV = _mapping().revision_id
REV2 = _mapping(revision_sequence=2).revision_id


# --- exact-membership resolver + CAS repoint (in-memory) -------------------


def test_unbound_client_resolves_to_none() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    assert index.resolve_binding("nobody", "dep-1") is None


def test_repoint_binds_exact_pair() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    expected = BindingResolution(deployment_id="dep-1", revision_id=REV, revision_sequence=1, status=ACTIVE)
    assert index.resolve_binding(CLIENT, "dep-1") == expected
    assert index.resolve_binding(CLIENT, "dep-2") is None


def test_repoint_monotonic_advance() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2


def test_repoint_idempotent_reaffirmation() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 1


def test_repoint_allows_monotonic_skip() -> None:
    # A binding may LAG the head and legitimately jump N -> N+2 if it missed an
    # intervening supersession; monotonic advance permits any strictly-greater
    # target (strict single-succession is enforced at the head, not per binding).
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 3, "rev-3", ACTIVE, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 3


def test_repoint_rejects_same_sequence_different_content() -> None:
    # Same sequence with a DIFFERENT digest is not idempotent -- it is a rollback
    # attempt against the pinned revision and is refused.
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 2, REV, ACTIVE, expected_current_sequence=None)
    with pytest.raises(NonMonotonicRepointError):
        index.repoint(CLIENT, "dep-1", 2, "rev-other", ACTIVE, expected_current_sequence=2)


def test_repoint_rejects_rollback() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=1)
    with pytest.raises(NonMonotonicRepointError):
        index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=2)


def test_repoint_precondition_on_stale_expected() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=1)
    with pytest.raises(BindingPreconditionError):
        index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=0)


def test_one_to_one_replace_to_other_deployment() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-2", 1, "rev-b", ACTIVE, expected_current_sequence=None)
    assert index.resolve_binding(CLIENT, "dep-1") is None
    assert index.resolve_binding(CLIENT, "dep-2") is not None


def test_revoke_is_a_tombstone_not_a_delete() -> None:
    # REVOKE repoints to a REVOKED tombstone at the strict-successor sequence; the
    # row is retained (counter + digest survive) and authorization denies.
    index = InMemoryClientDeploymentBindingIndex()
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, REVOKED, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is REVOKED
    assert resolution.revision_sequence == 2  # counter preserved


# --- authorized loader -----------------------------------------------------


class _CountingStore(InMemoryRuntimeDeploymentMappingStore):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        self.get_calls += 1
        return super().get(deployment_id, revision_sequence)


def _loader(
    *, bound: bool = True, mapping_present: bool = True, pin: str | None = None
) -> tuple[AuthorizedMappingLoader, RuntimeDeploymentMapping, _CountingStore]:
    index = InMemoryClientDeploymentBindingIndex()
    store = _CountingStore()
    mapping = _mapping()
    if mapping_present:
        store.commit_revision(mapping, expected_head_sequence=None)
    if bound:
        index.repoint(CLIENT, "dep-1", 1, pin or mapping.revision_id, ACTIVE, expected_current_sequence=None)
    return build_authorized_mapping_loader(index, store), mapping, store


def test_loader_returns_mapping_for_bound_client() -> None:
    load, mapping, _store = _loader()
    assert load(CLIENT, "dep-1") is mapping


def test_loader_unbound_reads_zero_mappings() -> None:
    load, _mapping, store = _loader(bound=False)
    assert load(CLIENT, "dep-1") is None
    assert store.get_calls == 0


def test_loader_wrong_deployment_reads_zero_mappings() -> None:
    load, _mapping, store = _loader()
    assert load(CLIENT, "dep-elsewhere") is None
    assert store.get_calls == 0


def test_loader_denies_when_revision_absent() -> None:
    load, _mapping, _store = _loader(mapping_present=False)
    assert load(CLIENT, "dep-1") is None


def test_loader_denies_on_digest_pin_mismatch() -> None:
    load, _mapping, _store = _loader(pin="a-different-pinned-digest")
    assert load(CLIENT, "dep-1") is None


def test_loader_denies_revoked_tombstone_without_reading_mapping() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    store = _CountingStore()
    mapping = _mapping()
    store.commit_revision(mapping, expected_head_sequence=None)
    index.repoint(CLIENT, "dep-1", 1, mapping.revision_id, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, REVOKED, expected_current_sequence=1)
    load = build_authorized_mapping_loader(index, store)
    store.get_calls = 0
    assert load(CLIENT, "dep-1") is None
    assert store.get_calls == 0


class _RevokedResolver:
    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        return BindingResolution(
            deployment_id=asserted_deployment_id, revision_id=REV, revision_sequence=1, status=REVOKED
        )


def test_loader_denies_soft_revoked_via_resolver() -> None:
    store = _CountingStore()
    store.commit_revision(_mapping(), expected_head_sequence=None)
    resolver: ClientDeploymentBindingResolver = _RevokedResolver()
    load = build_authorized_mapping_loader(resolver, store)
    assert load(CLIENT, "dep-1") is None
    assert store.get_calls == 0


# --- durable Cosmos binding adapter (CAS) ----------------------------------


class _FakeBindingContainer:
    """Cosmos container double with ETag + If-Match conditional replace."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}
        self._etags: dict[str, int] = {}

    def _store(self, body: dict[str, object]) -> None:
        item_id = str(body["id"])
        self._etags[item_id] = self._etags.get(item_id, 0) + 1
        doc = dict(body)
        doc["_etag"] = f"etag-{self._etags[item_id]}"
        self.items[item_id] = doc

    def create_item(self, body: dict[str, object]) -> dict[str, object]:
        if str(body["id"]) in self.items:
            raise CosmosHttpResponseError(status_code=409, message="conflict")  # type: ignore[no-untyped-call]
        self._store(body)
        return dict(self.items[str(body["id"])])

    def replace_item(
        self, *, item: str, body: dict[str, object], etag: str, match_condition: MatchConditions
    ) -> dict[str, object]:
        assert match_condition is MatchConditions.IfNotModified
        current = self.items.get(item)
        if current is None or current.get("_etag") != etag:
            raise CosmosHttpResponseError(status_code=412, message="precondition failed")  # type: ignore[no-untyped-call]
        self._store(body)
        return dict(self.items[item])

    def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
        assert item == partition_key
        if item not in self.items:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])


def test_cosmos_repoint_then_resolve() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 1
    assert resolution.revision_id == REV


def test_cosmos_unknown_client_is_none() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_cosmos_resolve_wrong_deployment_is_none() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    assert index.resolve_binding(CLIENT, "dep-2") is None


def test_cosmos_cas_advances() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2


def test_cosmos_revoke_tombstone_is_retained() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    index.repoint(CLIENT, "dep-1", 2, REV2, REVOKED, expected_current_sequence=1)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is REVOKED


def test_cosmos_precondition_on_stale_expected() -> None:
    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _FakeBindingContainer()))
    index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
    with pytest.raises(BindingPreconditionError):
        index.repoint(CLIENT, "dep-1", 2, REV2, ACTIVE, expected_current_sequence=5)


def test_cosmos_concurrent_create_is_precondition() -> None:
    class _RacingContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]

        def create_item(self, body: dict[str, object]) -> dict[str, object]:
            raise CosmosHttpResponseError(status_code=409, message="conflict")  # type: ignore[no-untyped-call]

    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _RacingContainer()))
    with pytest.raises(BindingPreconditionError):
        index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)


def test_cosmos_repoint_reraises_unexpected_error() -> None:
    class _BrokenContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, object]:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]

        def create_item(self, body: dict[str, object]) -> dict[str, object]:
            raise CosmosHttpResponseError(status_code=503, message="unavailable")  # type: ignore[no-untyped-call]

    index = CosmosClientDeploymentBindingIndex(cast(ContainerProxy, _BrokenContainer()))
    with pytest.raises(CosmosHttpResponseError):
        index.repoint(CLIENT, "dep-1", 1, REV, ACTIVE, expected_current_sequence=None)
