from __future__ import annotations

from datetime import UTC, datetime

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


# --- exact-membership resolver (now carrying the current revision) ----------


def test_unbound_client_has_no_current_revision() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    assert index.current_revision("nobody", "dep-1") is None


def test_grant_binds_exact_pair_to_a_revision() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    assert index.current_revision(CLIENT, "dep-1") == REV
    # Exact membership: a different deployment for the same client is unbound.
    assert index.current_revision(CLIENT, "dep-2") is None


def test_grant_repoints_to_new_current_revision() -> None:
    # A superseding grant repoints the binding to the new current revision.
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-1", "revision-2")
    assert index.current_revision(CLIENT, "dep-1") == "revision-2"


def test_client_may_hold_multiple_deployment_bindings() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-2", "revision-2")
    assert index.current_revision(CLIENT, "dep-1") == REV
    assert index.current_revision(CLIENT, "dep-2") == "revision-2"
    assert index.current_revision(CLIENT, "dep-3") is None


def test_revoke_removes_only_that_pair() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.grant(CLIENT, "dep-2", "revision-2")
    index.revoke(CLIENT, "dep-1")
    assert index.current_revision(CLIENT, "dep-1") is None
    assert index.current_revision(CLIENT, "dep-2") == "revision-2"


def test_revoke_last_binding_drops_the_client() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.grant(CLIENT, "dep-1", REV)
    index.revoke(CLIENT, "dep-1")
    assert index.current_revision(CLIENT, "dep-1") is None


def test_revoke_unknown_client_is_noop() -> None:
    index = InMemoryClientDeploymentBindingIndex()
    index.revoke("nobody", "dep-1")  # must not raise
    assert index.current_revision("nobody", "dep-1") is None


# --- authorized loader -----------------------------------------------------


class _CountingStore(InMemoryRuntimeDeploymentMappingStore):
    """Store spy that counts point-reads, to prove unbound callers read zero."""

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
    assert store.get_calls == 0  # unbound pair short-circuits before any read


def test_loader_returns_none_when_bound_but_revision_absent() -> None:
    # Binding-without-mapping is a denial, never repairable (fail-closed
    # reconciliation): the binding points at a revision the store does not hold.
    load, _mapping, _store = _loader(mapping_present=False)
    assert load(CLIENT, "dep-1") is None


def test_loader_returns_none_when_binding_points_at_stale_revision() -> None:
    # The binding's current-revision pointer is stale/dangling (points at a
    # revision id the store never stored): exact point read misses -> denial.
    load, _mapping, _store = _loader(bound_revision="revision-that-was-never-written")
    assert load(CLIENT, "dep-1") is None
