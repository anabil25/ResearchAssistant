from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import ValidationError
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
    RUNTIME_MAPPING_DOCUMENT_TYPE,
    CosmosRuntimeDeploymentMappingStore,
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeDeploymentHead,
    RuntimeHeadClaimError,
    RuntimeHeadPreconditionError,
    RuntimeMappingConflictError,
)


def _mapping(
    *,
    deployment_id: str = "dep-1",
    backend_version: str = "1.2.3",
    revision_sequence: int = 1,
    client_app_id: str = "client-app-1",
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
            AllowedClientAppRoleBinding(client_app_id=client_app_id, app_role="research-assistant.runtime"),
        ),
        revision_sequence=revision_sequence,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


# --- RuntimeDeploymentHead model ------------------------------------------


def test_head_model_is_frozen_and_extra_forbid() -> None:
    head = RuntimeDeploymentHead(deployment_id="dep-1", current_sequence=1, current_revision_id="rev-1")
    assert head.current_sequence == 1
    with pytest.raises(ValidationError):
        RuntimeDeploymentHead(deployment_id="dep-1", current_sequence=0, current_revision_id="rev-1")


def test_control_plane_adapter_is_structurally_incompatible_with_the_reader_port() -> None:
    # Precision (option 1): Python Protocols are STRUCTURAL, so separate Protocols
    # with no inheritance are necessary but NOT sufficient -- an object exposing
    # every reader method would satisfy the reader port silently. The control-plane
    # adapter is therefore made structurally INCOMPATIBLE BY COMPOSITION: it does
    # NOT expose `get` itself (reads go through `.reader`), so it cannot satisfy the
    # runtime reader Protocol and a mis-wire is a mypy error, not a silent success.
    from research_assistant_api.agent_studio.runtime_mapping_store import (
        CosmosRuntimeDeploymentMappingStore,
        RuntimeDeploymentMappingControlPlane,
        RuntimeDeploymentMappingReader,
    )

    # No inheritance either way.
    assert RuntimeDeploymentMappingReader not in RuntimeDeploymentMappingControlPlane.__mro__
    assert RuntimeDeploymentMappingControlPlane not in RuntimeDeploymentMappingReader.__mro__
    # The runtime reader port declares ONLY `get` (keep it minimal; adding a method
    # is a security-relevant change).
    reader_methods = {name for name in vars(RuntimeDeploymentMappingReader) if not name.startswith("_")}
    assert reader_methods == {"get"}
    # The control-plane adapters expose NO `get` of their own (composition), so they
    # cannot be passed where a runtime reader is expected.
    assert not hasattr(InMemoryRuntimeDeploymentMappingStore, "get")
    assert not hasattr(CosmosRuntimeDeploymentMappingStore, "get")
    # ...but they DO expose a `.reader` and the write/head/enumerate surface.
    for control_plane in (InMemoryRuntimeDeploymentMappingStore, CosmosRuntimeDeploymentMappingStore):
        assert hasattr(control_plane, "reader")
        for method in ("get_head", "list_revisions", "commit_revision", "delete"):
            assert hasattr(control_plane, method)


def test_runtime_composition_module_does_not_import_the_control_plane_adapter() -> None:
    # Precision (option 2): a COMPOSITION-ROOT property type checking cannot see --
    # the runtime composition module must never import the control-plane adapter, so
    # it cannot construct a write-capable object and hand it to the runtime plane
    # (the factory-reuse case). Grep-checkable now, lint-enforceable later.
    import inspect

    from research_assistant_api.agent_studio import runtime_control_mount

    source = inspect.getsource(runtime_control_mount)
    assert "CosmosRuntimeDeploymentMappingStore" not in source
    assert "InMemoryRuntimeDeploymentMappingStore" not in source


# --- InMemory: reads -------------------------------------------------------


def test_get_returns_none_for_unknown_sequence() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    assert store.reader.get("missing", 1) is None


def test_get_head_is_none_before_bootstrap() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    assert store.get_head("dep-1") is None


def test_bootstrap_then_get_round_trips_and_sets_head() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    mapping = _mapping(deployment_id="dep-xyz")
    store.commit_revision(mapping, expected_head_sequence=None)
    assert store.reader.get("dep-xyz", 1) is mapping
    head = store.get_head("dep-xyz")
    assert head is not None
    assert head.current_sequence == 1
    assert head.current_revision_id == mapping.revision_id
    assert head.bound_client_app_id == "client-app-1"  # deployment->one-client claim set


# --- InMemory: bound-client claim + clear_head_claim -----------------------


def test_commit_refuses_a_different_client_claim() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadClaimError, match="claimed by client 'c1'"):
        store.commit_revision(
            _mapping(revision_sequence=2, client_app_id="c2", backend_version="9.9.9"), expected_head_sequence=1
        )


def test_clear_head_claim_then_new_client_may_claim() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    store.clear_head_claim("dep-1", expected_client="c1")
    head = store.get_head("dep-1")
    assert head is not None and head.bound_client_app_id is None
    # A new client can now claim it (migration).
    store.commit_revision(
        _mapping(revision_sequence=2, client_app_id="c2", backend_version="9.9.9"), expected_head_sequence=1
    )
    head = store.get_head("dep-1")
    assert head is not None and head.bound_client_app_id == "c2"


def test_clear_head_claim_is_idempotent_when_absent_or_already_null() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.clear_head_claim("missing", expected_client="c1")  # no head -> no-op
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    store.clear_head_claim("dep-1", expected_client="c1")
    store.clear_head_claim("dep-1", expected_client="c1")  # already null -> no-op


def test_clear_head_claim_wrong_client_is_refused() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadClaimError, match="held by 'c1'"):
        store.clear_head_claim("dep-1", expected_client="other")


def test_get_with_wrong_sequence_is_none() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(deployment_id="dep-1", revision_sequence=1), expected_head_sequence=None)
    assert store.reader.get("dep-1", 2) is None


# --- InMemory: commit_revision idempotency + conflict ----------------------


def test_bootstrap_replay_is_idempotent() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    first = _mapping(deployment_id="dep-1", revision_sequence=1)
    store.commit_revision(first, expected_head_sequence=None)
    second = _mapping(deployment_id="dep-1", revision_sequence=1)
    assert first.mapping_digest == second.mapping_digest
    store.commit_revision(second, expected_head_sequence=None)  # replay: no-op
    assert store.reader.get("dep-1", 1) is first
    assert store.list_revisions("dep-1") == (1,)


def test_supersede_replay_is_idempotent() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    two = _mapping(revision_sequence=2, backend_version="2.0.0")
    store.commit_revision(two, expected_head_sequence=1)
    store.commit_revision(_mapping(revision_sequence=2, backend_version="2.0.0"), expected_head_sequence=1)
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 2


def test_bootstrap_when_head_exists_is_precondition_error() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadPreconditionError, match="expected no head"):
        store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=None)


def test_supersede_with_wrong_expected_head_is_precondition_error() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadPreconditionError, match="expected head at 5"):
        store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=5)


def test_supersede_before_bootstrap_is_precondition_error() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    with pytest.raises(RuntimeHeadPreconditionError, match="observed None"):
        store.commit_revision(_mapping(revision_sequence=2), expected_head_sequence=1)


def test_supersede_advances_head_and_coexists() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(revision_sequence=1, backend_version="1.2.3")
    b = _mapping(revision_sequence=2, backend_version="9.9.9")
    store.commit_revision(a, expected_head_sequence=None)
    store.commit_revision(b, expected_head_sequence=1)
    assert store.reader.get("dep-1", 1) is a
    assert store.reader.get("dep-1", 2) is b
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 2


def test_torn_bootstrap_retry_with_divergent_content_is_conflict() -> None:
    # A torn bootstrap left a revision item without its head; retrying at that
    # sequence with DIFFERENT content is a forged/racing competitor -> conflict.
    store = InMemoryRuntimeDeploymentMappingStore()
    original = _mapping(revision_sequence=1, backend_version="1.2.3")
    store._revisions["dep-1:1"] = original  # inject revision, leave head absent
    with pytest.raises(RuntimeMappingConflictError, match="different content"):
        store.commit_revision(_mapping(revision_sequence=1, backend_version="9.9.9"), expected_head_sequence=None)


def test_distinct_deployment_ids_are_isolated() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    a = _mapping(deployment_id="dep-a")
    b = _mapping(deployment_id="dep-b")
    store.commit_revision(a, expected_head_sequence=None)
    store.commit_revision(b, expected_head_sequence=None)
    assert store.reader.get("dep-a", 1) is a
    assert store.reader.get("dep-b", 1) is b


# --- InMemory: list_revisions + delete -------------------------------------


def test_list_revisions_is_ascending_and_partition_scoped() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(deployment_id="dep-1", revision_sequence=1), expected_head_sequence=None)
    store.commit_revision(
        _mapping(deployment_id="dep-1", revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1
    )
    store.commit_revision(_mapping(deployment_id="dep-2", revision_sequence=1), expected_head_sequence=None)
    assert store.list_revisions("dep-1") == (1, 2)
    assert store.list_revisions("dep-2") == (1,)
    assert store.list_revisions("dep-missing") == ()


def test_delete_removes_exact_revision() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.commit_revision(_mapping(deployment_id="dep-1", revision_sequence=1), expected_head_sequence=None)
    store.commit_revision(
        _mapping(deployment_id="dep-1", revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1
    )
    store.delete("dep-1", 1)
    assert store.reader.get("dep-1", 1) is None
    assert store.reader.get("dep-1", 2) is not None


def test_delete_missing_is_a_noop() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    store.delete("dep-1", 1)  # must not raise


# --- Cosmos adapter --------------------------------------------------------


class _FakeContainer:
    """Cosmos container double honoring transactional-batch + point-read semantics."""

    def __init__(self, *, force_batch_status: int | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self._etag = 0
        self.force_batch_status = force_batch_status
        self.batches: list[tuple[str, list[Any]]] = []

    def _bump(self, doc: dict[str, Any]) -> dict[str, Any]:
        self._etag += 1
        stored = dict(doc)
        stored["_etag"] = f"etag-{self._etag}"
        return stored

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        assert item.startswith(f"{partition_key}:")
        if item not in self.items:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        return dict(self.items[item])

    def delete_item(self, *, item: str, partition_key: str) -> None:
        assert item.startswith(f"{partition_key}:")
        if item not in self.items:
            raise CosmosResourceNotFoundError(message="not found")  # type: ignore[no-untyped-call]
        del self.items[item]

    def query_items(self, *, query: str, parameters: list[dict[str, Any]], partition_key: str) -> list[dict[str, Any]]:
        docs = [
            dict(doc)
            for doc in self.items.values()
            if doc.get("deployment_id") == partition_key and doc.get("documentType") == RUNTIME_MAPPING_DOCUMENT_TYPE
        ]
        docs.sort(key=lambda doc: int(doc["revision_sequence"]))
        return docs

    def execute_item_batch(self, *, batch_operations: list[Any], partition_key: str) -> list[Any]:
        self.batches.append((partition_key, list(batch_operations)))
        if self.force_batch_status is not None:
            raise CosmosHttpResponseError(status_code=self.force_batch_status, message="forced")  # type: ignore[no-untyped-call]
        staged: dict[str, dict[str, Any]] = {}
        for op in batch_operations:
            kind = op[0]
            if kind == "create":
                (doc,) = op[1]
                item_id = str(doc["id"])
                if item_id in self.items or item_id in staged:
                    raise CosmosHttpResponseError(status_code=409, message="conflict")  # type: ignore[no-untyped-call]
                staged[item_id] = doc
            else:  # replace
                item_id, doc = op[1]
                options = op[2] if len(op) > 2 else {}
                etag = options.get("if_match_etag")
                current = self.items.get(str(item_id))
                if etag is not None and (current is None or current.get("_etag") != etag):
                    raise CosmosHttpResponseError(status_code=412, message="precondition failed")  # type: ignore[no-untyped-call]
                staged[str(item_id)] = doc
        for item_id, doc in staged.items():
            self.items[item_id] = self._bump(doc)
        return []

    def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any
    ) -> dict[str, Any]:
        current = self.items.get(item)
        if current is None or current.get("_etag") != etag:
            raise CosmosHttpResponseError(status_code=412, message="precondition failed")  # type: ignore[no-untyped-call]
        self.items[item] = self._bump(body)
        return dict(self.items[item])


def test_cosmos_get_returns_none_when_absent() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    assert store.reader.get("missing", 1) is None


def test_cosmos_get_head_is_none_before_bootstrap() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    assert store.get_head("dep-1") is None


def test_cosmos_bootstrap_sets_and_reads_bound_client_claim() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    store.commit_revision(_mapping(client_app_id="c1"), expected_head_sequence=None)
    head = store.get_head("dep-1")
    assert head is not None and head.bound_client_app_id == "c1"


def test_cosmos_commit_refuses_a_different_client_claim() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadClaimError, match="claimed by client 'c1'"):
        store.commit_revision(
            _mapping(revision_sequence=2, client_app_id="c2", backend_version="9.9.9"), expected_head_sequence=1
        )


def test_cosmos_clear_head_claim_then_new_client_may_claim() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    store.commit_revision(_mapping(revision_sequence=1, client_app_id="c1"), expected_head_sequence=None)
    store.clear_head_claim("dep-1", expected_client="c1")
    head = store.get_head("dep-1")
    assert head is not None and head.bound_client_app_id is None
    store.commit_revision(
        _mapping(revision_sequence=2, client_app_id="c2", backend_version="9.9.9"), expected_head_sequence=1
    )
    head = store.get_head("dep-1")
    assert head is not None and head.bound_client_app_id == "c2"


def test_cosmos_clear_head_claim_idempotent_and_wrong_client() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    store.clear_head_claim("missing", expected_client="c1")  # no head -> no-op
    store.commit_revision(_mapping(client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadClaimError, match="held by 'c1'"):
        store.clear_head_claim("dep-1", expected_client="other")
    store.clear_head_claim("dep-1", expected_client="c1")
    store.clear_head_claim("dep-1", expected_client="c1")  # already null -> no-op


def test_cosmos_clear_head_claim_concurrent_modification_is_claim_error() -> None:
    class _RacingOnReplace(_FakeContainer):
        def replace_item(self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any) -> dict[str, Any]:
            raise CosmosHttpResponseError(status_code=412, message="precondition failed")  # type: ignore[no-untyped-call]

    container = _RacingOnReplace()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadClaimError, match="modified concurrently"):
        store.clear_head_claim("dep-1", expected_client="c1")


def test_cosmos_clear_head_claim_reraises_unexpected_error() -> None:
    class _BrokenOnReplace(_FakeContainer):
        def replace_item(self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any) -> dict[str, Any]:
            raise CosmosHttpResponseError(status_code=503, message="unavailable")  # type: ignore[no-untyped-call]

    container = _BrokenOnReplace()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(client_app_id="c1"), expected_head_sequence=None)
    with pytest.raises(CosmosHttpResponseError):
        store.clear_head_claim("dep-1", expected_client="c1")


def test_cosmos_bootstrap_then_get_and_head() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(deployment_id="dep-1")
    store.commit_revision(mapping, expected_head_sequence=None)
    loaded = store.reader.get("dep-1", 1)
    assert loaded is not None
    assert loaded.mapping_digest == mapping.mapping_digest
    head = store.get_head("dep-1")
    assert head is not None
    assert head.current_sequence == 1
    assert head.current_revision_id == mapping.revision_id


def test_cosmos_bootstrap_replay_is_idempotent() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(deployment_id="dep-1"), expected_head_sequence=None)
    store.commit_revision(_mapping(deployment_id="dep-1"), expected_head_sequence=None)  # replay
    assert store.list_revisions("dep-1") == (1,)


def test_cosmos_supersede_via_batch_advances_head() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1, backend_version="1.2.3"), expected_head_sequence=None)
    store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1)
    assert store.reader.get("dep-1", 1) is not None
    assert store.reader.get("dep-1", 2) is not None
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 2
    assert store.list_revisions("dep-1") == (1, 2)


def test_cosmos_bootstrap_batch_shape_and_single_partition_key() -> None:
    # SDK-CONTRACT-VERIFIED assumption is exercised at the SHAPE level only:
    # bootstrap submits TWO create ops in ONE batch under a SINGLE partition key
    # (create-only id uniqueness adjudicates concurrent bootstrap). We do NOT and
    # cannot assert Cosmos applied both-or-neither from a container double.
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(deployment_id="dep-1"), expected_head_sequence=None)
    assert len(container.batches) == 1
    partition_key, ops = container.batches[0]
    assert partition_key == "dep-1"  # ONE partition key
    assert [op[0] for op in ops] == ["create", "create"]
    # No precondition options on create ops (create is create-only by nature).
    assert all(len(op) == 2 for op in ops)


def test_cosmos_supersede_batch_shape_and_if_match_precondition() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1)
    partition_key, ops = container.batches[-1]
    assert partition_key == "dep-1"  # single partition key
    assert [op[0] for op in ops] == ["create", "replace"]
    # The head REPLACE carries an If-Match precondition (repoint), the revision
    # CREATE does not (create-only).
    create_op, replace_op = ops
    assert len(create_op) == 2  # revision create: no precondition options
    assert isinstance(replace_op[2], dict) and "if_match_etag" in replace_op[2]  # head replace: If-Match



    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    with pytest.raises(RuntimeHeadPreconditionError, match="observed None"):
        store.commit_revision(_mapping(revision_sequence=2), expected_head_sequence=1)


def test_cosmos_supersede_with_wrong_expected_head_is_precondition_error() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    with pytest.raises(RuntimeHeadPreconditionError, match="expected head at 5"):
        store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=5)


def test_cosmos_double_bootstrap_divergent_content_is_conflict() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1, backend_version="1.2.3"), expected_head_sequence=None)
    with pytest.raises(RuntimeMappingConflictError, match="different content"):
        store.commit_revision(_mapping(revision_sequence=1, backend_version="9.9.9"), expected_head_sequence=None)


def test_cosmos_torn_bootstrap_same_content_retry_is_head_precondition() -> None:
    # Revision landed but head did not (torn bootstrap); retry with the SAME
    # content: create 409s, digests match, so it is NOT a conflict -- it surfaces
    # as a head-precondition signal to re-read and retry.
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    mapping = _mapping(revision_sequence=1)
    container.items["dep-1:1"] = container._bump(store._revision_document(mapping))
    with pytest.raises(RuntimeHeadPreconditionError, match="re-read head"):
        store.commit_revision(mapping, expected_head_sequence=None)


def test_cosmos_supersede_head_moved_under_batch_is_precondition() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    container.force_batch_status = 412  # concurrent supersede moved the head
    with pytest.raises(RuntimeHeadPreconditionError, match="re-read head"):
        store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1)


def test_cosmos_commit_reraises_non_precondition_error() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    container.force_batch_status = 503
    with pytest.raises(CosmosHttpResponseError):
        store.commit_revision(_mapping(revision_sequence=2, backend_version="9.9.9"), expected_head_sequence=1)


def test_cosmos_delete_removes_exact_revision() -> None:
    container = _FakeContainer()
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, container))
    store.commit_revision(_mapping(revision_sequence=1), expected_head_sequence=None)
    store.delete("dep-1", 1)
    assert store.reader.get("dep-1", 1) is None


def test_cosmos_delete_missing_is_a_noop() -> None:
    store = CosmosRuntimeDeploymentMappingStore(cast(ContainerProxy, _FakeContainer()))
    store.delete("dep-1", 1)  # 404 swallowed, must not raise
