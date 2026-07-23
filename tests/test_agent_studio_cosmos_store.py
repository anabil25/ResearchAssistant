"""Tests for the Cosmos-backed Agent Studio metadata store."""

# mypy: disable-error-code=import-untyped

from __future__ import annotations

import importlib
import threading
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.cosmos_store as cosmos_store
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    DeploymentEnvironment,
    DeploymentRecord,
    GateName,
    GateResult,
    GateStatus,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistrationKind,
    ToolRegistrationSpec,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import (
    AgentStudioStore,
    AgentStudioStoreError,
    DraftConflictError,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
AGENT_ID = "agent-cosmos-test"
OTHER_AGENT_ID = "agent-cosmos-other"
USER_ID = "user-1"

SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
SAME_TENANT_OTHER_PROJECT_SCOPE = ScopeContext(tenant_id=TENANT, project_id=OTHER_PROJECT)
OTHER_TENANT_SAME_PROJECT_SCOPE = ScopeContext(tenant_id=OTHER_TENANT, project_id=PROJECT)


def _token_credential() -> TokenCredential:
    return cast("TokenCredential", object())


def _manifest(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    display_name: str = "Cosmos Test Agent",
) -> AgentManifest:
    return AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        display_name=display_name,
        owner_kind=AgentOwnerKind.USER,
        owner_id=USER_ID,
    )


def _draft(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    etag: str = "etag-1",
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        updated_by=USER_ID,
        etag=etag,
    )


def _version(
    *,
    sequence: int = 1,
    version_id: str | None = None,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> AgentVersion:
    return AgentVersion(
        id=version_id or f"version-{sequence}",
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        sequence=sequence,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        manifest_hash=f"hash-{sequence}",
        created_by=USER_ID,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )


def _lineage(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    child_logical_agent_id: str = AGENT_ID,
    child_version_id: str = "version-2",
    parent_logical_agent_id: str = OTHER_AGENT_ID,
    parent_version_id: str = "version-1",
) -> LineageEdge:
    return LineageEdge(
        tenant_id=tenant_id,
        project_id=project_id,
        child_logical_agent_id=child_logical_agent_id,
        child_version_id=child_version_id,
        parent_logical_agent_id=parent_logical_agent_id,
        parent_version_id=parent_version_id,
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    status: ReleaseStatus = ReleaseStatus.GATED,
    previous_release_id: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        manifest_hash="hash-" + version_id,
        status=status,
        previous_release_id=previous_release_id,
        created_by=USER_ID,
    )


def _gate_report(
    report_id: str = "report-1",
    version_id: str = "version-1",
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> ReleaseGateReport:
    return ReleaseGateReport(
        id=report_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        results=(GateResult(name=GateName.TEST, status=GateStatus.PASSED, detail="ok"),),
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    idempotency_key: str = "key-1",
    state: ApprovalState = ApprovalState.PENDING,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        kind=ApprovalKind.RELEASE_PROMOTION,
        state=state,
        gated_action="promote_version",
        destination="prod",
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=idempotency_key,
    )


def _deployment(
    *,
    deployment_id: str = "deployment-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    version_id: str = "version-1",
    trace_ref: str | None = None,
) -> DeploymentRecord:
    return DeploymentRecord(
        id=deployment_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by=USER_ID,
        trace_ref=trace_ref,
    )


def _binding(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    version_id: str = "version-1",
) -> LogicalAgentBinding:
    return LogicalAgentBinding(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version_id,
        updated_by=USER_ID,
    )


def _tool_registration(
    *,
    registration_id: str = "reg-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> ToolRegistrationSpec:
    return ToolRegistrationSpec(
        id=registration_id,
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by=USER_ID,
    )


def _proposal(
    *,
    proposal_id: str = "proposal-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    state: BuilderProposalState = BuilderProposalState.PENDING,
) -> BuilderProposal:
    manifest = _manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id)
    return BuilderProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        draft_base_etag="etag-1",
        before_manifest=manifest,
        after_manifest=manifest,
        before_manifest_hash="hash-before",
        after_manifest_hash="hash-after",
        provenance=BuilderProvenance(
            generator="test-generator",
            message="Add a search tool.",
            requested_by=USER_ID,
        ),
        state=state,
    )


class FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.version = 0
        self.fail_replace_status: int | None = None
        self.query_calls = 0
        self.query_log: list[dict[str, Any]] = []
        self.read_log: list[tuple[str, str]] = []
        # Guards ``create_item``/``replace_item`` critical sections so
        # concurrent threads racing against the same fake container observe
        # genuine optimistic-concurrency semantics (one wins, the other gets
        # a 409/412) instead of silently corrupting ``self.documents`` via
        # unsynchronized dict mutation -- this is what makes the parallel
        # sequence-allocation tests meaningful.
        self._lock = threading.Lock()

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.version += 1
            stored = deepcopy(item)
            stored["_etag"] = str(self.version)
            key = (stored["scope_key"], stored["id"])
            self.documents[key] = stored
            return deepcopy(stored)

    def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = (body["scope_key"], body["id"])
            if key in self.documents:
                raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                    status_code=409,
                    message="conflict: document already exists",
                )
            self.version += 1
            stored = deepcopy(body)
            stored["_etag"] = str(self.version)
            self.documents[key] = stored
            return deepcopy(stored)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        self.read_log.append((partition_key, item))
        key = (partition_key, item)
        with self._lock:
            if key not in self.documents:
                raise CosmosResourceNotFoundError(  # type: ignore[no-untyped-call]
                    status_code=404,
                    message="missing",
                )
            return deepcopy(self.documents[key])

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, str]],
        partition_key: str,
    ) -> list[dict[str, Any]]:
        self.query_calls += 1
        values = {item["name"]: item["value"] for item in parameters}
        self.query_log.append(
            {
                "query": query,
                "parameters": deepcopy(parameters),
                "partition_key": partition_key,
            }
        )
        document_type = values["@documentType"]
        with self._lock:
            return [
                deepcopy(document)
                for (scope_key, _), document in self.documents.items()
                if scope_key == partition_key and document["documentType"] == document_type
            ]

    def replace_item(
        self,
        *,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        if self.fail_replace_status is not None:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=self.fail_replace_status,
                message="simulated failure",
            )
        assert match_condition is MatchConditions.IfNotModified
        key = (body["scope_key"], item)
        with self._lock:
            if self.documents[key]["_etag"] != etag:
                raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                    status_code=412,
                    message="etag mismatch: document was modified concurrently",
                )
            self.version += 1
            stored = deepcopy(body)
            stored["_etag"] = str(self.version)
            self.documents[key] = stored
            return deepcopy(stored)

    def get_document(self, scope_key: str, document_id: str) -> dict[str, Any]:
        return deepcopy(self.documents[(scope_key, document_id)])

    def inject_document(
        self,
        *,
        scope_key: str,
        document_id: str,
        document_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.upsert_item(
            {
                "id": document_id,
                "documentType": document_type,
                "scope_key": scope_key,
                "payload": deepcopy(payload),
            }
        )


class FakeDatabase:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}
        self.requested_container_names: list[str] = []

    def get_container_client(self, name: str) -> FakeContainer:
        self.requested_container_names.append(name)
        return self.containers.setdefault(name, FakeContainer())


class FakeCosmosClient:
    def __init__(self, endpoint: str, credential: Any, database: FakeDatabase) -> None:
        self.endpoint = endpoint
        self.credential = credential
        self._database = database
        self.requested_database_names: list[str] = []

    def get_database_client(self, name: str) -> FakeDatabase:
        self.requested_database_names.append(name)
        return self._database


class FakeCosmosClientFactory:
    def __init__(self) -> None:
        self.database = FakeDatabase()
        self.clients: list[FakeCosmosClient] = []

    def __call__(self, endpoint: str, credential: Any) -> FakeCosmosClient:
        client = FakeCosmosClient(endpoint, credential, self.database)
        self.clients.append(client)
        return client


@pytest.fixture
def fake_client_factory(monkeypatch: pytest.MonkeyPatch) -> FakeCosmosClientFactory:
    factory = FakeCosmosClientFactory()
    monkeypatch.setattr(cosmos_store, "CosmosClient", factory)
    return factory


def _new_store(
    _factory: FakeCosmosClientFactory,
    *,
    metadata_container_name: str = "agentStudioMetadataV1",
) -> cosmos_store.CosmosAgentStudioStore:
    return cosmos_store.CosmosAgentStudioStore(
        "https://cosmos.example.test",
        "agent-studio",
        _token_credential(),
        metadata_container_name,
    )


def _metadata_container(
    factory: FakeCosmosClientFactory,
    *,
    name: str = "agentStudioMetadataV1",
) -> FakeContainer:
    return factory.database.containers[name]


def test_constructor_uses_requested_database_and_metadata_container(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    credential = _token_credential()
    store = cosmos_store.CosmosAgentStudioStore(
        "https://cosmos.example.test",
        "agent-studio",
        credential,
        "custom-metadata",
    )

    assert isinstance(store, cosmos_store.CosmosAgentStudioStore)
    assert len(fake_client_factory.clients) == 1
    client = fake_client_factory.clients[0]
    assert client.endpoint == "https://cosmos.example.test"
    assert client.credential is credential
    assert client.requested_database_names == ["agent-studio"]
    assert fake_client_factory.database.requested_container_names == ["custom-metadata"]


def test_drafts_round_trip_document_shape_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    draft = _draft()

    assert store.persistence == "Azure Cosmos DB"
    assert store.save_draft(SCOPE, draft) == draft

    container = _metadata_container(fake_client_factory)
    document = container.get_document(SCOPE.scope_key, "draft::agent-cosmos-test")
    assert document == {
        "id": "draft::agent-cosmos-test",
        "documentType": "draft",
        "scope_key": SCOPE.scope_key,
        "payload": draft.model_dump(mode="json"),
        "_etag": "1",
    }

    reloaded = _new_store(fake_client_factory)
    assert reloaded.get_draft(SCOPE, AGENT_ID) == draft
    assert reloaded.list_drafts(SCOPE) == (draft,)
    assert reloaded.get_draft(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) is None
    assert reloaded.list_drafts(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert reloaded.get_draft(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) is None
    assert reloaded.list_drafts(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()
    assert any(
        entry["partition_key"] == SCOPE.scope_key
        and entry["parameters"] == [{"name": "@documentType", "value": "draft"}]
        for entry in container.query_log
    )


def test_save_draft_enforces_expected_etag_app_level_and_cosmos_native_concurrency(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Review finding #6: Cosmos ``save_draft`` must enforce optimistic
    concurrency both at the app level (comparing the stored ``AgentDraft.etag``)
    and at the infra level (Cosmos ``MatchConditions.IfNotModified`` + native
    ``_etag``), converting either failure mode into ``DraftConflictError``."""
    store = _new_store(fake_client_factory)
    draft = _draft()
    assert store.save_draft(SCOPE, draft) == draft

    first_editor = _new_store(fake_client_factory)
    fetched = first_editor.get_draft(SCOPE, AGENT_ID)
    assert fetched is not None

    updated = fetched.model_copy(update={"display_name": "First Editor Update", "etag": "etag-after-first-editor"})
    assert first_editor.save_draft(SCOPE, updated, expected_etag=fetched.etag) == updated

    second_editor = _new_store(fake_client_factory)
    stale_update = fetched.model_copy(
        update={"display_name": "Second Editor Lost", "etag": "etag-after-second-editor"}
    )
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        second_editor.save_draft(SCOPE, stale_update, expected_etag=fetched.etag)
    assert second_editor.get_draft(SCOPE, AGENT_ID) == updated

    with pytest.raises(DraftConflictError, match="modified concurrently"):
        second_editor.save_draft(SCOPE, stale_update, expected_etag="never-issued-etag")

    missing_draft_store = _new_store(fake_client_factory)
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        missing_draft_store.save_draft(
            SAME_TENANT_OTHER_PROJECT_SCOPE,
            _draft(project_id=SAME_TENANT_OTHER_PROJECT_SCOPE.project_id),
            expected_etag="any-etag",
        )

    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    race_store = _new_store(fake_client_factory)
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        race_store.save_draft(
            SCOPE,
            updated.model_copy(update={"display_name": "Raced Out"}),
            expected_etag=updated.etag,
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        race_store.save_draft(
            SCOPE,
            updated.model_copy(update={"display_name": "Unexpected Failure"}),
            expected_etag=updated.etag,
        )
    container.fail_replace_status = None


def test_ownership_listing_role_resolution_and_partition_scoping(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    owner = OwnershipGrant(
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id=AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.OWNER,
        granted_by="admin",
    )
    unrelated = OwnershipGrant(
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id=OTHER_AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.VIEWER,
        granted_by="admin",
    )

    assert store.grant_ownership(SCOPE, owner) == owner
    assert store.grant_ownership(SCOPE, unrelated) == unrelated

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_ownership(SCOPE, AGENT_ID) == (owner,)
    assert reloaded.list_ownership(SCOPE, AGENT_ID) == (owner,)
    assert reloaded.list_ownership(SCOPE, OTHER_AGENT_ID) == (unrelated,)
    assert reloaded.role_for(SCOPE, AGENT_ID, USER_ID) is AgentRole.OWNER
    assert reloaded.role_for(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, USER_ID) is None
    assert reloaded.role_for(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, USER_ID) is None


def test_versions_create_allocate_get_list_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    first_version = _version(sequence=1, version_id="version-1")
    other_agent_version = _version(
        sequence=1,
        version_id="other-agent-version-1",
        logical_agent_id=OTHER_AGENT_ID,
    )
    assert first.create_version(SCOPE, first_version) == first_version
    assert first.create_version(SCOPE, other_agent_version) == other_agent_version

    second = _new_store(fake_client_factory)
    allocated = second.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )
    assert allocated.sequence == 2
    assert second.list_versions(SCOPE, AGENT_ID) == (first_version, allocated)
    assert second.list_versions(SCOPE, AGENT_ID) == (first_version, allocated)

    third = _new_store(fake_client_factory)
    assert third.get_version(SCOPE, first_version.id) == first_version
    assert third.get_version(SCOPE, first_version.id) == first_version
    assert third.get_version(SCOPE, "missing-version") is None
    assert third.get_version(SAME_TENANT_OTHER_PROJECT_SCOPE, first_version.id) is None
    assert third.list_versions(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert third.get_version(OTHER_TENANT_SAME_PROJECT_SCOPE, first_version.id) is None
    assert third.list_versions(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _version(sequence=7, version_id="version-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="version::version-mismatch",
        document_type="version",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_version(SCOPE, "version-mismatch") is None


def test_allocate_sequence_cas_retries_after_create_conflict_then_succeeds(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First sequence allocation for an agent: the counter document does not
    exist yet, so ``_allocate_sequence_cas`` takes the create-if-absent
    branch. Simulate another process winning the create race once (a 409)
    before this instance retries and succeeds."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item
    calls = {"count": 0}

    def flaky_create_item(body: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409,
                message="simulated concurrent counter creation",
            )
        return original_create_item(body)

    monkeypatch.setattr(container, "create_item", flaky_create_item)

    allocated = store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert allocated.sequence == 1
    assert calls["count"] == 2


def test_allocate_sequence_cas_retries_after_replace_conflict_then_succeeds(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second-and-later sequence allocations replace the existing counter
    document via ETag compare-and-swap. Simulate another process winning
    that replace race once (a 412) before this instance re-reads and
    succeeds."""
    store = _new_store(fake_client_factory)
    store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    container = _metadata_container(fake_client_factory)
    original_replace_item = container.replace_item
    calls = {"count": 0}

    def flaky_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=412,
                message="simulated concurrent counter replace",
            )
        return original_replace_item(item=item, body=body, etag=etag, match_condition=match_condition)

    monkeypatch.setattr(container, "replace_item", flaky_replace_item)

    allocated = store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert allocated.sequence == 2
    assert calls["count"] == 2


def test_allocate_sequence_cas_raises_after_exhausting_retry_budget(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the counter replace keeps losing the CAS race beyond the bounded
    retry budget, allocation must fail loudly (never silently reuse or
    fabricate a sequence number)."""
    store = _new_store(fake_client_factory)
    store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    container = _metadata_container(fake_client_factory)

    def always_conflicting_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=412,
            message="simulated permanent counter replace conflict",
        )

    monkeypatch.setattr(container, "replace_item", always_conflicting_replace_item)

    with pytest.raises(AgentStudioStoreError, match="Exceeded"):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )


def test_allocate_version_rejects_builder_returning_wrong_sequence(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A builder that ignores the atomically-reserved sequence and stamps a
    different one onto the version must be rejected rather than silently
    persisted under a mismatched sequence."""
    store = _new_store(fake_client_factory)

    with pytest.raises(AgentStudioStoreError, match="expected atomically-reserved"):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence + 41, version_id="version-wrong-sequence"),
        )


def test_allocate_sequence_cas_reraises_unexpected_create_and_replace_errors(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-conflict Cosmos errors from the counter create/replace calls must
    propagate immediately rather than being swallowed as a benign race."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)

    def failing_create_item(body: dict[str, Any]) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=500,
            message="simulated unexpected create failure",
        )

    monkeypatch.setattr(container, "create_item", failing_create_item)

    with pytest.raises(CosmosHttpResponseError):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )

    monkeypatch.undo()
    container_after_undo = _metadata_container(fake_client_factory)

    def failing_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=500,
            message="simulated unexpected replace failure",
        )

    with pytest.MonkeyPatch.context() as replace_patch:
        replace_patch.setattr(container_after_undo, "replace_item", failing_replace_item)
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )
        with pytest.raises(CosmosHttpResponseError):
            store.allocate_version(
                SCOPE,
                AGENT_ID,
                lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
            )


def test_sync_versions_swallows_benign_local_cache_race(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_sync_versions`` reads a document not yet present in this
    instance's local cache and tries to insert it. If a concurrent call on
    the same instance already inserted the exact same document between the
    membership check and the insert (the only way ``AgentStudioStore.
    create_version`` can raise here), that must be swallowed as a harmless
    race rather than propagated."""
    store = _new_store(fake_client_factory)
    version = _version(sequence=1, version_id="version-race")
    container = _metadata_container(fake_client_factory)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="version::version-race",
        document_type="version",
        payload=version.model_dump(mode="json"),
    )

    def _raise_already_exists(
        self: cosmos_store.CosmosAgentStudioStore, scope: ScopeContext, version: AgentVersion
    ) -> AgentVersion:
        raise AgentStudioStoreError(f"Version '{version.id}' already exists; versions are immutable.")

    monkeypatch.setattr(AgentStudioStore, "create_version", _raise_already_exists)

    store._sync_versions(SCOPE, AGENT_ID)  # must not raise

    assert version.id not in store._versions


def test_allocate_version_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances/processes racing to cut versions.

    Each thread uses its OWN ``CosmosAgentStudioStore`` instance (its own
    in-memory cache), but all instances share the same underlying
    ``FakeContainer`` documents -- mirroring multiple API replicas hitting
    the same Cosmos container concurrently. The CAS-based sequence counter
    must hand out a strictly-unique sequence to every successful caller with
    no duplicates, even though gaps are acceptable.
    """
    thread_count = 12
    allocated_sequences: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _allocate(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        barrier.wait()

        def _builder(sequence: int, worker_index: int = worker_index) -> AgentVersion:
            return _version(sequence=sequence, version_id=f"concurrent-version-{worker_index}")

        try:
            version = store.allocate_version(SCOPE, AGENT_ID, _builder)
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            allocated_sequences.append(version.sequence)

    threads = [threading.Thread(target=_allocate, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(allocated_sequences) == thread_count
    assert len(set(allocated_sequences)) == thread_count, (
        f"expected {thread_count} unique sequence numbers, got duplicates: {allocated_sequences}"
    )

    final_store = _new_store(fake_client_factory)
    persisted = final_store.list_versions(SCOPE, AGENT_ID)
    assert len(persisted) == thread_count
    assert sorted(v.sequence for v in persisted) == sorted(allocated_sequences)
    assert len({v.id for v in persisted}) == thread_count


def test_lineage_and_gate_reports_round_trip_without_scope_leakage(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    edge = _lineage()
    report = _gate_report()

    assert store.add_lineage_edge(SCOPE, edge) == edge
    assert store.save_gate_report(SCOPE, report) == report

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_lineage(SCOPE, AGENT_ID) == (edge,)
    assert reloaded.list_lineage(SCOPE, AGENT_ID) == (edge,)
    assert reloaded.list_lineage(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.list_lineage(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.get_gate_report(SCOPE, report.id) == report
    assert reloaded.get_gate_report(SCOPE, report.id) == report
    assert reloaded.get_gate_report(SCOPE, "missing-report") is None
    assert reloaded.get_gate_report(SAME_TENANT_OTHER_PROJECT_SCOPE, report.id) is None
    assert reloaded.get_gate_report(OTHER_TENANT_SAME_PROJECT_SCOPE, report.id) is None

    container = _metadata_container(fake_client_factory)
    gate_report_document = container.get_document(SCOPE.scope_key, "gate_report::report-1")
    assert gate_report_document["documentType"] == "gate_report"
    assert gate_report_document["scope_key"] == SCOPE.scope_key
    assert gate_report_document["payload"] == report.model_dump(mode="json")

    mismatched = _gate_report(report_id="mismatch", project_id=OTHER_PROJECT)
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.save_gate_report(SCOPE, mismatched)


def test_get_gate_report_rejects_document_with_mismatched_scope_payload(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """``save_gate_report`` always writes a payload whose tenant/project
    agree with the partition it is stored under, so an untampered read can
    never observe a scope mismatch -- this defends against a corrupted or
    hand-edited document landing in the right partition (e.g. an operator
    fixing up data, or a future bug in a sibling writer) with the wrong
    ``tenant_id``/``project_id`` recorded inside its payload. Inject such a
    document directly into the fake container (bypassing the store's own
    validated write path) to prove the read-time guard still refuses to
    return -- and does not cache -- a document whose payload scope disagrees
    with the partition it was read from."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)

    corrupted = _gate_report(report_id="corrupted", tenant_id=OTHER_TENANT, project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="gate_report::corrupted",
        document_type="gate_report",
        payload=corrupted.model_dump(mode="json"),
    )

    assert store.get_gate_report(SCOPE, "corrupted") is None
    # The mismatch guard must prevent caching, too -- a second read hits the
    # same rejected path rather than returning a stale, cached local copy.
    assert store.get_gate_report(SCOPE, "corrupted") is None
    assert AgentStudioStore.get_gate_report(store, SCOPE, "corrupted") is None


def test_releases_round_trip_latest_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    gated = _release(release_id="release-1")
    active = _release(
        release_id="release-2",
        status=ReleaseStatus.ACTIVE,
        previous_release_id=gated.id,
    )
    other_version = _release(release_id="release-3", version_id="version-2")
    assert first.create_release(SCOPE, gated) == gated
    assert first.create_release(SCOPE, active) == active
    assert first.create_release(SCOPE, other_version) == other_version

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_releases_for_version(SCOPE, "version-1") == (gated, active)
    assert reloaded.list_releases_for_version(SCOPE, "version-1") == (gated, active)
    assert reloaded.latest_release_for_version(SCOPE, "version-1") == active
    assert reloaded.latest_release_for_version(SCOPE, "missing-version") is None

    getter = _new_store(fake_client_factory)
    assert getter.get_release(SCOPE, gated.id) == gated
    assert getter.get_release(SCOPE, gated.id) == gated
    assert getter.get_release(SCOPE, "missing-release") is None
    assert getter.get_release(SAME_TENANT_OTHER_PROJECT_SCOPE, gated.id) is None
    assert getter.list_releases_for_version(SAME_TENANT_OTHER_PROJECT_SCOPE, "version-1") == ()
    assert getter.get_release(OTHER_TENANT_SAME_PROJECT_SCOPE, gated.id) is None
    assert getter.list_releases_for_version(OTHER_TENANT_SAME_PROJECT_SCOPE, "version-1") == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _release(release_id="release-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="release::release-mismatch",
        document_type="release",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_release(SCOPE, "release-mismatch") is None


def test_approvals_create_list_get_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    pending = _approval()
    assert first.create_approval(SCOPE, pending) == pending

    duplicate = _approval(approval_id="approval-duplicate")
    reloaded = _new_store(fake_client_factory)
    assert reloaded.create_approval(SCOPE, duplicate) == pending
    assert reloaded.list_approvals(SCOPE) == (pending,)
    assert reloaded.list_approvals(SCOPE, version_id=pending.version_id) == (pending,)
    assert reloaded.list_approvals(SCOPE, version_id="missing-version") == ()
    assert reloaded.get_approval(SCOPE, pending.id) == pending
    assert reloaded.get_approval(SAME_TENANT_OTHER_PROJECT_SCOPE, pending.id) is None
    assert reloaded.list_approvals(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert reloaded.get_approval(OTHER_TENANT_SAME_PROJECT_SCOPE, pending.id) is None
    assert reloaded.list_approvals(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()

    container = _metadata_container(fake_client_factory)
    approval_documents = [
        document
        for document in container.documents.values()
        if document["scope_key"] == SCOPE.scope_key and document["documentType"] == "approval"
    ]
    assert len(approval_documents) == 1


def test_save_approval_decision_handles_success_missing_decided_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    pending = _approval()
    store.create_approval(SCOPE, pending)

    approved = pending.model_copy(update={"state": ApprovalState.APPROVED, "approver_id": "approver-1"})
    assert store.save_approval_decision(SCOPE, approved) == approved
    assert store.get_approval(SCOPE, pending.id) == approved

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_approval_decision(SCOPE, approved)

    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Approval 'missing-approval' not found"):
        missing_store.save_approval_decision(SCOPE, _approval(approval_id="missing-approval"))

    conflict_store = _new_store(fake_client_factory)
    conflict_store.create_approval(SCOPE, _approval(approval_id="approval-conflict", idempotency_key="key-conflict"))
    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="decided concurrently"):
        conflict_store.save_approval_decision(
            SCOPE,
            _approval(
                approval_id="approval-conflict",
                idempotency_key="key-conflict",
                state=ApprovalState.APPROVED,
            ),
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        conflict_store.save_approval_decision(
            SCOPE,
            _approval(
                approval_id="approval-conflict",
                idempotency_key="key-conflict",
                state=ApprovalState.APPROVED,
            ),
        )
    container.fail_replace_status = None


def test_deployments_create_list_get_update_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    deployment = _deployment()
    other_agent = _deployment(deployment_id="deployment-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_deployment(SCOPE, deployment) == deployment
    assert first.create_deployment(SCOPE, other_agent) == other_agent

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_deployments(SCOPE, AGENT_ID) == (deployment,)
    assert reloaded.list_deployments(SCOPE, AGENT_ID) == (deployment,)
    assert reloaded.list_deployments(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    getter = _new_store(fake_client_factory)
    assert getter.get_deployment(SCOPE, deployment.id) == deployment
    assert getter.get_deployment(SCOPE, deployment.id) == deployment
    assert getter.get_deployment(SCOPE, "missing-deployment") is None
    assert getter.get_deployment(SAME_TENANT_OTHER_PROJECT_SCOPE, deployment.id) is None
    assert reloaded.list_deployments(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_deployment(OTHER_TENANT_SAME_PROJECT_SCOPE, deployment.id) is None
    assert reloaded.list_deployments(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    updated = deployment.model_copy(update={"trace_ref": "trace-1"})
    assert reloaded.update_deployment(SCOPE, updated) == updated
    assert reloaded.get_deployment(SCOPE, deployment.id) == updated

    container = _metadata_container(fake_client_factory)
    mismatched = _deployment(deployment_id="deployment-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="deployment::deployment-mismatch",
        document_type="deployment",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_deployment(SCOPE, "deployment-mismatch") is None


def test_update_deployment_handles_missing_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Deployment 'missing-deployment' not found"):
        missing_store.update_deployment(SCOPE, _deployment(deployment_id="missing-deployment"))

    store = _new_store(fake_client_factory)
    current = _deployment(deployment_id="deployment-conflict")
    store.create_deployment(SCOPE, current)

    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        store.update_deployment(SCOPE, current.model_copy(update={"trace_ref": "trace-2"}))

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.update_deployment(SCOPE, current.model_copy(update={"trace_ref": "trace-2"}))
    container.fail_replace_status = None


def test_bindings_and_tool_registrations_round_trip(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    binding = _binding()
    registration = _tool_registration()
    other_registration = _tool_registration(registration_id="reg-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.set_binding(SCOPE, binding) == binding
    assert first.create_tool_registration(SCOPE, registration) == registration
    assert first.create_tool_registration(SCOPE, other_registration) == other_registration

    reloaded = _new_store(fake_client_factory)
    assert reloaded.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert reloaded.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert reloaded.get_binding(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert reloaded.get_binding(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert reloaded.list_tool_registrations(SCOPE, AGENT_ID) == (registration,)
    assert reloaded.list_tool_registrations(SCOPE, AGENT_ID) == (registration,)
    assert reloaded.list_tool_registrations(SCOPE, OTHER_AGENT_ID) == (other_registration,)
    assert reloaded.list_tool_registrations(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.list_tool_registrations(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()


def test_builder_proposals_create_list_get_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    proposal = _proposal()
    other_agent = _proposal(proposal_id="proposal-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_builder_proposal(SCOPE, proposal) == proposal
    assert first.create_builder_proposal(SCOPE, other_agent) == other_agent

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_builder_proposals(SCOPE, AGENT_ID) == (proposal,)
    assert reloaded.list_builder_proposals(SCOPE, AGENT_ID) == (proposal,)
    assert reloaded.list_builder_proposals(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    getter = _new_store(fake_client_factory)
    assert getter.get_builder_proposal(SCOPE, proposal.id) == proposal
    assert getter.get_builder_proposal(SCOPE, proposal.id) == proposal
    assert getter.get_builder_proposal(SCOPE, "missing-proposal") is None
    assert getter.get_builder_proposal(SAME_TENANT_OTHER_PROJECT_SCOPE, proposal.id) is None
    assert reloaded.list_builder_proposals(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_builder_proposal(OTHER_TENANT_SAME_PROJECT_SCOPE, proposal.id) is None
    assert reloaded.list_builder_proposals(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _proposal(proposal_id="proposal-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="builder_proposal::proposal-mismatch",
        document_type="builder_proposal",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_builder_proposal(SCOPE, "proposal-mismatch") is None


def test_save_builder_proposal_decision_handles_success_missing_decided_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    pending = _proposal()
    store.create_builder_proposal(SCOPE, pending)

    applied = pending.model_copy(update={"state": BuilderProposalState.APPLIED, "decided_by": "approver-1"})
    assert store.save_builder_proposal_decision(SCOPE, applied) == applied
    assert store.get_builder_proposal(SCOPE, pending.id) == applied

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_builder_proposal_decision(SCOPE, applied)

    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Proposal 'missing-proposal' not found"):
        missing_store.save_builder_proposal_decision(SCOPE, _proposal(proposal_id="missing-proposal"))

    conflict_store = _new_store(fake_client_factory)
    current = _proposal(proposal_id="proposal-conflict")
    conflict_store.create_builder_proposal(SCOPE, current)
    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="was decided concurrently"):
        conflict_store.save_builder_proposal_decision(
            SCOPE,
            current.model_copy(update={"state": BuilderProposalState.APPLIED}),
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        conflict_store.save_builder_proposal_decision(
            SCOPE,
            current.model_copy(update={"state": BuilderProposalState.APPLIED}),
        )
    container.fail_replace_status = None


def test_build_agent_studio_store_raises_without_endpoint() -> None:
    with pytest.raises(AgentStudioStoreError, match="metadata persistence is unavailable"):
        cosmos_store.build_agent_studio_store(Settings(cosmos_endpoint=None))


def test_build_agent_studio_store_uses_default_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            self.database = FakeDatabase()
            captured["database"] = self.database

        def get_database_client(self, name: str) -> FakeDatabase:
            captured["database_name"] = name
            return self.database

    with monkeypatch.context() as patch:
        patch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
        patch.setattr(azure.identity, "DefaultAzureCredential", lambda: "default-credential")
        reloaded = importlib.reload(cosmos_store)
        store = reloaded.build_agent_studio_store(
            Settings(
                cosmos_endpoint="https://cosmos.example.test",
                agent_studio_cosmos_database="custom-db",
                agent_studio_metadata_container="custom-container",
            )
        )

        assert isinstance(store, reloaded.CosmosAgentStudioStore)
        assert captured == {
            "endpoint": "https://cosmos.example.test",
            "credential": "default-credential",
            "database": captured["database"],
            "database_name": "custom-db",
        }
        assert cast(FakeDatabase, captured["database"]).requested_container_names == ["custom-container"]

    importlib.reload(cosmos_store)


def test_build_agent_studio_store_uses_managed_identity_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            self.database = FakeDatabase()
            captured["database"] = self.database

        def get_database_client(self, name: str) -> FakeDatabase:
            captured["database_name"] = name
            return self.database

    with monkeypatch.context() as patch:
        patch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
        patch.setattr(azure.identity, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}")
        reloaded = importlib.reload(cosmos_store)
        store = reloaded.build_agent_studio_store(
            Settings(
                cosmos_endpoint="https://cosmos.example.test",
                managed_identity_client_id="client-123",
                agent_studio_cosmos_database="agent-studio-db",
                agent_studio_metadata_container="agentStudioMetadataV1",
            )
        )

        assert isinstance(store, reloaded.CosmosAgentStudioStore)
        assert captured == {
            "endpoint": "https://cosmos.example.test",
            "credential": "managed:client-123",
            "database": captured["database"],
            "database_name": "agent-studio-db",
        }
        assert cast(FakeDatabase, captured["database"]).requested_container_names == ["agentStudioMetadataV1"]

    importlib.reload(cosmos_store)
