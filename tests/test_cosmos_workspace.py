from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier, Thread
from typing import Any

import pytest
import research_assistant_api.cosmos_workspace as cosmos_workspace
import research_assistant_api.workspace as workspace
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ServiceRequestError
from azure.cosmos.exceptions import CosmosHttpResponseError
from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ApprovalDecision,
    ApprovalState,
    ChatThread,
    ConnectorUpdate,
    DatasetApprovalDecisionRequest,
    DatasetApprovalDenialReason,
    DatasetApprovalError,
    LibraryIngestRecord,
    RunStage,
    RunSummary,
    WorkspaceStore,
    utc_now,
)
from research_assistant_core.models import Capability, RunStatus


class FakeCredential(TokenCredential):
    def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: Any,
    ) -> AccessToken:
        return AccessToken("fake", int(datetime.now(UTC).timestamp()) + 3600)


class FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.version = 0
        # When set, the next ``replace_item`` call raises a Cosmos conflict
        # instead of applying the write, letting tests simulate a
        # concurrent writer winning the optimistic-concurrency race.
        self.fail_replace_status: int | None = None
        self.on_replace: Callable[
            [FakeContainer, str, dict[str, Any], str | None, Any], dict[str, Any]
        ] | None = None

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        self.version += 1
        stored = deepcopy(item)
        stored["_etag"] = str(self.version)
        self.documents[item["id"]] = stored
        return deepcopy(stored)

    def replace_item(
        self,
        *,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        if self.on_replace is not None:
            return self.on_replace(self, item, body, etag, match_condition)
        if self.fail_replace_status is not None:
            status = self.fail_replace_status
            self.fail_replace_status = None
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=status,
                message="simulated concurrent write conflict",
            )
        assert self.documents[item]["_etag"] == etag
        return self.upsert_item(body)

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, str]],
        enable_cross_partition_query: bool,
    ) -> list[dict[str, Any]]:
        values = {item["name"]: item["value"] for item in parameters}
        return [
            deepcopy(item)
            for item in self.documents.values()
            if item["documentType"] == values["@documentType"]
            and item["tenantId"] == values["@tenantId"]
            and item["projectId"] == values["@projectId"]
        ]


class FakeDatabase:
    def __init__(self) -> None:
        self.containers = {
            "projects": FakeContainer(),
            "sources": FakeContainer(),
            "runs": FakeContainer(),
        }

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers[name]


class FakeCosmosClient:
    def __init__(self, database: FakeDatabase | None = None) -> None:
        self.database = database or FakeDatabase()

    def get_database_client(self, _name: str) -> FakeDatabase:
        return self.database


def test_cosmos_workspace_seeds_and_reloads_operational_state(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    credential = FakeCredential()
    first = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )

    assert first.summary().persistence == "Azure Cosmos DB"
    assert len(fake_client.database.containers["sources"].documents) == 9
    assert len(fake_client.database.containers["runs"].documents) == 6
    foreign_item = deepcopy(
        next(iter(fake_client.database.containers["sources"].documents.values()))
    )
    foreign_item["id"] = "foreign-project-source"
    foreign_item["projectId"] = "foreign-project"
    foreign_item["tenantProjectKey"] = "demo|foreign-project"
    foreign_item["payload"]["id"] = "foreign-project-source"
    fake_client.database.containers["sources"].upsert_item(foreign_item)
    assert all(item.id != "foreign-project-source" for item in first.library())

    live_replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )

    settings = first.settings().model_copy(update={"description": "Persisted project description"})
    first.update_settings(settings)
    first.update_connector(
        "openalex",
        ConnectorUpdate(enabled=True, assigned_agents=["matching"]),
    )
    assert live_replica.settings().description == "Persisted project description"
    assert next(item for item in live_replica.connectors() if item.id == "openalex").assigned_agents == ["matching"]
    ingested = first.ingest(
        LibraryIngestRecord(
            source_id="source-abc123abc123",
            title="Persisted protocol",
            kind="Policy",
            source="Workspace upload",
            access="internal",
            license="Project supplied",
            description="A runtime ingestion record.",
        ),
        IdentityContext(
            user_id="researcher-1",
            display_name="Researcher One",
            tenant_id="demo",
            groups=("researchers",),
            source="test",
        ),
    )
    first.decide_approval(
        "approval-grant-export",
        ApprovalDecision(
            decision=ApprovalState.APPROVED,
            rationale="The exact package and destination were reviewed.",
        ),
        IdentityContext(
            user_id="reviewer-1",
            display_name="Reviewer One",
            tenant_id="demo",
            groups=("grant-reviewers",),
            source="test",
        ),
    )
    now = utc_now()
    first.save_chat_thread(
        ChatThread(
            id="chat-persisted",
            project_id=first.project_id,
            tenant_id=first.tenant_id,
            capability=Capability.LITERATURE,
            agent_name="literature-agent",
            owner_principal_id="researcher-1",
            conversation_id="conversation-persisted",
            session_id="session-persisted",
            delegated_user_identity="ra:persisted-user",
            created_at=now,
            updated_at=now,
        )
    )

    second = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )

    assert second.settings().description == "Persisted project description"
    assert any(item.id == ingested.item.id for item in second.library())
    assert second.run(ingested.run.id) is not None
    approval = next(item for item in second.approvals() if item.id == "approval-grant-export")
    assert approval.state == ApprovalState.APPROVED
    assert approval.approver_id == "reviewer-1"
    chat_thread = second.chat_thread(
        "chat-persisted",
        owner_principal_id="researcher-1",
    )
    assert chat_thread is not None
    assert chat_thread.conversation_id == "conversation-persisted"
    assert chat_thread.session_id == "session-persisted"
    assert chat_thread.delegated_user_identity == "ra:persisted-user"
    grant_run = second.run("run-grant-001")
    assert grant_run is not None
    assert grant_run.status.value == "completed"
    assert grant_run.progress == 100
    assert grant_run.current_stage == "Complete"
    assert grant_run.completed_at is not None


def _reviewer_identity() -> IdentityContext:
    return IdentityContext(
        user_id="reviewer-1",
        display_name="Reviewer One",
        tenant_id="demo",
        groups=("grant-reviewers",),
        source="test",
    )


def test_dataset_approval_request_is_persisted_and_visible_across_replicas(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    credential = FakeCredential()
    first = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )

    created = first.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )

    assert created.state.value == "pending"
    runs_documents = fake_client.database.containers["runs"].documents.values()
    assert any(item["documentType"] == "dataset_approval" for item in runs_documents)

    second = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )
    reloaded = second.dataset_approval_request(created.id)
    assert reloaded is not None
    assert reloaded.plan_fingerprint == "fp-abc"

    decided = second.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )
    assert decided is not None
    assert decided.state.value == "approved"

    consumed = second.consume_dataset_approval_request(
        created.id,
        plan_fingerprint="fp-abc",
        invocation_id="inv-1",
        consumed_by_principal_id="requester-1",
    )
    assert consumed.state.value == "consumed"
    assert consumed.consumed_invocation_id == "inv-1"

    third = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        credential,
    )
    third_request = third.dataset_approval_request(created.id)
    assert third_request is not None
    assert third_request.state.value == "consumed"


def test_decide_dataset_approval_request_missing_returns_none(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )

    result = store.decide_dataset_approval_request(
        "dsapproval-does-not-exist",
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )

    assert result is None


def test_decide_dataset_approval_request_conflict_with_same_decision_is_idempotent(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    container = fake_client.database.containers["runs"]
    original_replace_item = container.replace_item

    def _concurrent_same_decision_then_conflict(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        # Simulate a concurrent replica committing the identical "approved"
        # decision after our own fresh read but before our write lands.
        document = container.documents[item]
        document["payload"]["state"] = "approved"
        document["payload"]["approver_id"] = "reviewer-2"
        document["payload"]["approver_name"] = "Reviewer Two"
        document["payload"]["rationale"] = "Concurrent reviewer approved first."
        container.version += 1
        document["_etag"] = str(container.version)
        container.replace_item = original_replace_item  # type: ignore[method-assign]
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=412,
            message="simulated concurrent decision",
        )

    monkeypatch.setattr(container, "replace_item", _concurrent_same_decision_then_conflict)

    result = store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )

    assert result is not None
    assert result.state.value == "approved"
    assert result.approver_id == "reviewer-2"


def test_decide_dataset_approval_request_conflict_with_different_decision_fails_closed(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    container = fake_client.database.containers["runs"]

    def _concurrent_different_decision_then_conflict(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        # Simulate a concurrent replica rejecting the request instead.
        document = container.documents[item]
        document["payload"]["state"] = "rejected"
        container.version += 1
        document["_etag"] = str(container.version)
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=412,
            message="simulated concurrent decision",
        )

    monkeypatch.setattr(container, "replace_item", _concurrent_different_decision_then_conflict)

    try:
        store.decide_dataset_approval_request(
            created.id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
            _reviewer_identity(),
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "concurrently" in str(exc).lower()


def test_consume_dataset_approval_request_missing_raises_value_error(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )

    try:
        store.consume_dataset_approval_request(
            "dsapproval-does-not-exist",
            plan_fingerprint="fp-abc",
            invocation_id="inv-1",
            consumed_by_principal_id="requester-1",
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not found" in str(exc).lower()


def test_consume_dataset_approval_request_conflict_never_retries_and_fails_closed(
    monkeypatch: Any,
) -> None:
    """Unlike ``decide_dataset_approval_request``, consumption is strictly
    single-use: even if a concurrent writer's outcome happened to match
    what this caller would have produced, a losing racer on the ETag CAS
    must still fail closed rather than risk two invocations being
    authorized from one decided approval."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )
    fake_client.database.containers["runs"].fail_replace_status = 412

    try:
        store.consume_dataset_approval_request(
            created.id,
            plan_fingerprint="fp-abc",
            invocation_id="inv-1",
            consumed_by_principal_id="requester-1",
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "concurrently consumed" in str(exc).lower()

    # The record must remain APPROVED (not silently CONSUMED), since the
    # write never actually succeeded.
    reread = store.dataset_approval_request(created.id)
    assert reread is not None
    assert reread.state.value == "approved"


def test_decide_dataset_approval_request_propagates_non_conflict_replace_errors(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    fake_client.database.containers["runs"].fail_replace_status = 500

    try:
        store.decide_dataset_approval_request(
            created.id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
            _reviewer_identity(),
        )
        raise AssertionError("expected CosmosHttpResponseError")
    except CosmosHttpResponseError as exc:
        assert exc.status_code == 500


def test_consume_dataset_approval_request_propagates_non_conflict_replace_errors(
    monkeypatch: Any,
) -> None:
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )
    fake_client.database.containers["runs"].fail_replace_status = 500

    try:
        store.consume_dataset_approval_request(
            created.id,
            plan_fingerprint="fp-abc",
            invocation_id="inv-1",
            consumed_by_principal_id="requester-1",
        )
        raise AssertionError("expected CosmosHttpResponseError")
    except CosmosHttpResponseError as exc:
        assert exc.status_code == 500


def test_decide_dataset_approval_request_returns_none_if_record_vanishes_after_fresh_read(
    monkeypatch: Any,
) -> None:
    """Defensive branch: if the base in-memory decide somehow returns
    ``None`` despite the document having just been found in the fresh
    Cosmos read (a state this code cannot organically reach given how
    ``self._dataset_approvals`` is populated immediately beforehand), the
    Cosmos store must still return ``None`` rather than raise or persist
    a bogus write."""
    import research_assistant_api.workspace as workspace_module

    fake_client = FakeCosmosClient()
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    monkeypatch.setattr(
        workspace_module.WorkspaceStore,
        "decide_dataset_approval_request",
        lambda self, request_id, decision, identity: None,
    )

    result = store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )

    assert result is None


def _seed_and_decide(store: cosmos_workspace.CosmosWorkspaceStore) -> str:
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )
    store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        _reviewer_identity(),
    )
    return created.id


def test_dataset_approval_audit_trail_is_durable_across_replicas(monkeypatch: Any) -> None:
    """A decision and a consumption each emit an audit/outbox intent written
    atomically inside the same document, so a fresh replica (cold read) sees the
    full, still-undelivered trail -- nothing is lost after the state mutation."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())

    approval_id = _seed_and_decide(store)
    store.consume_dataset_approval_request(
        approval_id, plan_fingerprint="fp-abc", invocation_id="inv-1", consumed_by_principal_id="requester-1"
    )

    replica = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())
    trail = replica.dataset_approval_audit()
    assert sorted(entry.action for entry in trail) == ["consumed", "decided"]
    pending = replica.pending_dataset_approval_audit()
    assert len(pending) == 2

    marked = replica.mark_dataset_approval_audit_delivered(pending[0].id)
    assert marked is not None
    assert len(replica.pending_dataset_approval_audit()) == 1


def test_consume_reconciles_unknown_transport_outcome_that_actually_landed(monkeypatch: Any) -> None:
    """Ambiguous transport failure where the write *did* durably land: reconcile
    against the store and honor the single invocation rather than failing a
    genuinely-consumed approval."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())
    approval_id = _seed_and_decide(store)

    container = fake_client.database.containers["runs"]
    original_replace = container.replace_item

    def _apply_then_lose_response(**kwargs: Any) -> dict[str, Any]:
        original_replace(**kwargs)  # the write lands durably
        container.replace_item = original_replace  # type: ignore[method-assign]
        raise ServiceRequestError(message="connection reset before the response was read")

    monkeypatch.setattr(container, "replace_item", _apply_then_lose_response)

    record = store.consume_dataset_approval_request(
        approval_id, plan_fingerprint="fp-abc", invocation_id="inv-1", consumed_by_principal_id="requester-1"
    )
    assert record.state.value == "consumed"
    assert record.consumed_invocation_id == "inv-1"


def test_consume_reconciles_unknown_transport_outcome_that_did_not_land(monkeypatch: Any) -> None:
    """Ambiguous transport failure where the write did *not* land: fail closed
    (the approval remains APPROVED) and never blindly retry."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())
    approval_id = _seed_and_decide(store)

    container = fake_client.database.containers["runs"]
    original_replace = container.replace_item

    def _lose_response_without_writing(**kwargs: Any) -> dict[str, Any]:
        container.replace_item = original_replace  # type: ignore[method-assign]
        raise ServiceRequestError(message="connection reset before the write was applied")

    monkeypatch.setattr(container, "replace_item", _lose_response_without_writing)

    with pytest.raises(DatasetApprovalError) as excinfo:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint="fp-abc", invocation_id="inv-1",
            consumed_by_principal_id="requester-1")
    assert excinfo.value.reason == DatasetApprovalDenialReason.CONCURRENT_CONFLICT
    reread = store.dataset_approval_request(approval_id)
    assert reread is not None
    assert reread.state.value == "approved"


def test_dataset_approval_is_isolated_per_project(monkeypatch: Any) -> None:
    """Cross-project/tenant IDOR guard: an approval created in one project is
    invisible and unconsumable from another project's store (partition-scoped
    ``_query``)."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    project_a = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential(), tenant_id="demo", project_id="project-a"
    )
    project_b = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential(), tenant_id="demo", project_id="project-b"
    )
    created = project_a.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="requester-1",
    )

    assert project_b.dataset_approval_request(created.id) is None
    with pytest.raises(DatasetApprovalError) as excinfo:
        project_b.consume_dataset_approval_request(created.id, plan_fingerprint="fp-abc", invocation_id="inv-b")
    assert excinfo.value.reason == DatasetApprovalDenialReason.NOT_FOUND
    assert project_a.dataset_approval_request(created.id) is not None


def _identity_with(user_id: str) -> IdentityContext:
    return IdentityContext(
        user_id=user_id,
        display_name=user_id.title(),
        tenant_id="demo",
        groups=("research-reviewers",),
        source="container-apps-auth",
    )


def test_requester_principal_is_persisted_independent_of_shared_cache(monkeypatch: Any) -> None:
    """Regression for the SOD-bypass race: the requester principal must be
    written from the local create-time value, so even if a concurrent reload
    clears the shared cache it is still durably persisted and separation-of-duties
    stays enforceable on every replica."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())

    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="dual-1",
    )
    # Simulate a racing reload having wiped the in-memory requester cache.
    store._dataset_requester_principals.clear()
    raw_document = fake_client.database.containers["runs"].documents[created.id]
    assert raw_document["requesterPrincipalId"] == "dual-1"

    replica = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())
    with pytest.raises(DatasetApprovalError) as excinfo:
        replica.decide_dataset_approval_request(
            created.id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Self approval."),
            _identity_with("dual-1"),
        )
    assert excinfo.value.reason == DatasetApprovalDenialReason.SEPARATION_OF_DUTIES


def test_concurrent_governance_never_loses_audit_or_requester_principal(monkeypatch: Any) -> None:
    """Barrier-controlled concurrency: while workers create/decide/consume
    distinct approvals, a reader repeatedly reloads (which rebuilds the shared
    caches). Because every dataset operation holds the store lock across its
    whole query-mutate-persist cycle, no reload can interleave and drop a
    just-appended audit intent or a requester principal."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())

    worker_count = 6
    barrier = Barrier(worker_count + 1)
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            created = store.create_dataset_approval_request(
                plan_fingerprint=f"fp-{index}",
                filename="inline.csv",
                objective="Profile the supplied dataset.",
                requested_by=f"Researcher {index}",
                ttl_minutes=60,
                requested_by_principal_id=f"req-{index}",
            )
            store.decide_dataset_approval_request(
                created.id,
                DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
                _reviewer_identity(),
            )
            store.consume_dataset_approval_request(
                created.id, plan_fingerprint=f"fp-{index}", invocation_id=f"inv-{index}",
                consumed_by_principal_id=f"req-{index}"
            )
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        barrier.wait()
        for _ in range(60):
            store.dataset_approval_requests()
            store.dataset_approval_audit()

    threads = [Thread(target=worker, args=(index,)) for index in range(worker_count)]
    threads.append(Thread(target=reader))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    audit_by_request: dict[str, list[str]] = {}
    for entry in store.dataset_approval_audit():
        audit_by_request.setdefault(entry.request_id, []).append(entry.action)
    assert len(audit_by_request) == worker_count
    for actions in audit_by_request.values():
        assert sorted(actions) == ["consumed", "decided"]

    replica = cosmos_workspace.CosmosWorkspaceStore("https://cosmos.example.test", "research", FakeCredential())
    persisted = {
        document.get("requesterPrincipalId")
        for document in fake_client.database.containers["runs"].documents.values()
        if document["documentType"] == "dataset_approval"
    }
    assert persisted == {f"req-{index}" for index in range(worker_count)}
    assert len(replica.dataset_approval_requests()) == worker_count


def test_consume_rereads_authoritative_state_not_a_validate_time_snapshot(monkeypatch: Any) -> None:
    """INTENT: 're-verify at consume' is satisfiable in letter by re-running the
    checks against a record SNAPSHOT captured at validate time, which re-opens
    the exact TOCTOU window the split was meant to close.

    Drives it directly: validate succeeds, then the AUTHORITATIVE store is
    mutated out of band (another replica consumes), and the consume attempt must
    observe that new state and deny.
    """
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    approval_id = _seed_and_decide(store)

    # Validation passes against current durable state.
    validated = store.validate_dataset_approval_request(
        approval_id, plan_fingerprint="fp-abc", consumed_by_principal_id="requester-1"
    )
    assert validated.state.value == "approved"

    # A DIFFERENT replica consumes it out of band. The first store never sees
    # this through any in-process path -- only by re-reading Cosmos.
    other_replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    other_replica.consume_dataset_approval_request(
        approval_id,
        plan_fingerprint="fp-abc",
        invocation_id="other-replica",
        consumed_by_principal_id="requester-1",
    )

    # The original store's stale validation must grant nothing.
    with pytest.raises(DatasetApprovalError) as exc:
        store.consume_dataset_approval_request(
            approval_id,
            plan_fingerprint="fp-abc",
            invocation_id="stale",
            consumed_by_principal_id="requester-1",
        )
    assert exc.value.reason == DatasetApprovalDenialReason.ALREADY_CONSUMED
    reread = store.dataset_approval_request(approval_id)
    assert reread is not None
    assert reread.consumed_invocation_id == "other-replica"


def test_consume_queries_cosmos_on_every_attempt(monkeypatch: Any) -> None:
    """Structural companion to the above: the consume path must actually hit the
    container, not serve from whatever the validate call left cached."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    approval_id = _seed_and_decide(store)

    container = fake_client.database.containers["runs"]
    original_query = container.query_items
    queries: list[str] = []

    def counting_query(**kwargs: Any) -> Any:
        values = {item["name"]: item["value"] for item in kwargs["parameters"]}
        queries.append(str(values.get("@documentType")))
        return original_query(**kwargs)

    monkeypatch.setattr(container, "query_items", counting_query)

    store.validate_dataset_approval_request(
        approval_id, plan_fingerprint="fp-abc", consumed_by_principal_id="requester-1"
    )
    after_validate = queries.count("dataset_approval")
    store.consume_dataset_approval_request(
        approval_id,
        plan_fingerprint="fp-abc",
        invocation_id="inv-1",
        consumed_by_principal_id="requester-1",
    )
    after_consume = queries.count("dataset_approval")

    assert after_validate >= 1, "validate did not read durable state"
    assert after_consume > after_validate, "consume served from a snapshot instead of re-reading Cosmos"


def test_legacy_cosmos_document_without_requester_is_denied_at_consume(monkeypatch: Any) -> None:
    """The population Finding 3 is actually ABOUT: records already APPROVED
    before requesterPrincipalId existed. They will never pass through `decide`
    again -- their only remaining transition is CONSUME -- so gating decide
    alone would protect none of them.

    Exercises the real load path: _reload_dataset_state builds
    _dataset_requester_principals only from documents where requesterPrincipalId
    is not None, so a legacy document lands with NO entry and no signal that
    anything is absent. Something must explicitly refuse it.
    """
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    approval_id = _seed_and_decide(store)

    # Strip the field to make the stored document indistinguishable from one
    # written before requesterPrincipalId existed.
    container = fake_client.database.containers["runs"]
    document = container.documents[approval_id]
    document.pop("requesterPrincipalId", None)
    assert "requesterPrincipalId" not in container.documents[approval_id]

    # A cold replica loads it: APPROVED, unconsumed, requester unknowable.
    replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    loaded = replica.dataset_approval_request(approval_id)
    assert loaded is not None
    assert loaded.state.value == "approved"
    assert replica.dataset_requester_principal(approval_id) is None

    # CONSUME must deny -- this is the transition that matters for this population.
    with pytest.raises(DatasetApprovalError) as consume_exc:
        replica.consume_dataset_approval_request(
            approval_id,
            plan_fingerprint="fp-abc",
            invocation_id="legacy-consume",
            consumed_by_principal_id="requester-1",
        )
    assert consume_exc.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER

    # The early validation denies for the same reason, and neither spends it.
    with pytest.raises(DatasetApprovalError) as validate_exc:
        replica.validate_dataset_approval_request(
            approval_id, plan_fingerprint="fp-abc", consumed_by_principal_id="requester-1"
        )
    assert validate_exc.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER

    still = replica.dataset_approval_request(approval_id)
    assert still is not None
    assert still.state.value == "approved", "a denied legacy record must not be spent"


def test_absence_denies_regardless_of_the_consuming_principal(monkeypatch: Any) -> None:
    """Absence must be a denial IN ITS OWN RIGHT, raised before the equality
    comparison -- not a comparison that merely happens not to match. Proven by
    varying the consuming principal, including values chosen to collide with a
    plausible sentinel: every one denies with the SAME absence reason, never
    PRINCIPAL_MISMATCH and never success."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    approval_id = _seed_and_decide(store)
    fake_client.database.containers["runs"].documents[approval_id].pop("requesterPrincipalId", None)
    replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )

    for principal in ("requester-1", "someone-else", "<unknown>", "<unknown-requester>", "None", ""):
        with pytest.raises(DatasetApprovalError) as exc:
            replica.validate_dataset_approval_request(
                approval_id, plan_fingerprint="fp-abc", consumed_by_principal_id=principal
            )
        assert exc.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER, principal


def test_legacy_documents_omit_the_key_so_null_comparison_finds_nothing(monkeypatch: Any) -> None:
    """The enumeration query must use NOT IS_DEFINED, never
    `c.requesterPrincipalId = null`.

    _dataset_approval_document sets requesterPrincipalId ONLY when a principal is
    present, so legacy documents OMIT THE KEY rather than storing null. An
    equality-to-null predicate therefore matches nothing and reports a clean
    zero-affected population -- false reassurance that no migration is needed,
    which is worse than no query at all. This pins the shape of the stored
    document so that reassurance can never be manufactured by accident.
    """
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    approval_id = _seed_and_decide(store)
    container = fake_client.database.containers["runs"]
    container.documents[approval_id].pop("requesterPrincipalId", None)
    document = container.documents[approval_id]

    # The key is ABSENT, not present-and-null. This is precisely why the
    # `= null` form cannot observe the condition it is asked about.
    assert "requesterPrincipalId" not in document
    assert document.get("requesterPrincipalId") is None  # only because it is missing

    null_equality_matches = [
        item
        for item in container.documents.values()
        if item.get("documentType") == "dataset_approval"
        and "requesterPrincipalId" in item
        and item["requesterPrincipalId"] is None
    ]
    not_is_defined_matches = [
        item
        for item in container.documents.values()
        if item.get("documentType") == "dataset_approval"
        and "requesterPrincipalId" not in item
    ]
    assert null_equality_matches == [], "the `= null` predicate would report zero affected"
    assert [item["id"] for item in not_is_defined_matches] == [approval_id]

    # And the in-process helper observes the real condition on a cold replica.
    replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential()
    )
    affected = replica.dataset_approvals_blocked_by_requester_attribution()
    assert [record.id for record in affected] == [approval_id]


def test_enumeration_helper_is_scoped_to_one_project_and_under_reports(monkeypatch: Any) -> None:
    """SCOPE WARNING made executable: _query pins @tenantId/@projectId, so the
    in-process helper reports THIS project only. An operator who runs it instead
    of the cross-partition query will under-report the fleet."""
    fake_client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_workspace, "CosmosClient", lambda _endpoint, credential: fake_client)
    project_a = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential(),
        tenant_id="demo", project_id="project-a",
    )
    project_b = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test", "research", FakeCredential(),
        tenant_id="demo", project_id="project-b",
    )
    id_a = _seed_and_decide(project_a)
    id_b = _seed_and_decide(project_b)
    container = fake_client.database.containers["runs"]
    for approval_id in (id_a, id_b):
        container.documents[approval_id].pop("requesterPrincipalId", None)

    # Each store sees only its own partition: 1, not the fleet-wide 2.
    assert [r.id for r in project_a.dataset_approvals_blocked_by_requester_attribution()] == [id_a]
    assert [r.id for r in project_b.dataset_approvals_blocked_by_requester_attribution()] == [id_b]

    fleet_wide = [
        item["id"]
        for item in container.documents.values()
        if item.get("documentType") == "dataset_approval"
        and "requesterPrincipalId" not in item
    ]
    assert sorted(fleet_wide) == sorted([id_a, id_b])
    assert len(fleet_wide) > len(project_a.dataset_approvals_blocked_by_requester_attribution())


def _identity(
    user_id: str = "researcher-1",
    display_name: str = "Researcher One",
    tenant_id: str = "demo",
    groups: tuple[str, ...] = ("researchers",),
) -> IdentityContext:
    return IdentityContext(
        user_id=user_id,
        display_name=display_name,
        tenant_id=tenant_id,
        groups=groups,
        source="test",
    )


def _ingest_record(source_id: str = "source-abc123abc123") -> LibraryIngestRecord:
    return LibraryIngestRecord(
        source_id=source_id,
        title="Persisted protocol",
        kind="Policy",
        source="Workspace upload",
        access="internal",
        license="Project supplied",
        description="A runtime ingestion record.",
    )


def _approval_payload(run_id: str) -> dict[str, str]:
    return {
        "run_id": run_id,
        "title": "Release artifact",
        "gated_action": "Export reviewed package",
        "destination": "SharePoint research site",
        "requested_by": "grant-agent",
        "evidence_summary": "All facts were checked.",
        "risk": "Medium",
    }


def _install_fake_cosmos(
    monkeypatch: Any,
    database: FakeDatabase | None = None,
) -> FakeCosmosClient:
    fake_client = FakeCosmosClient(database)
    monkeypatch.setattr(
        cosmos_workspace,
        "CosmosClient",
        lambda _endpoint, credential: fake_client,
    )
    return fake_client


def _make_store(
    monkeypatch: Any,
    database: FakeDatabase | None = None,
    *,
    tenant_id: str = "demo",
    project_id: str = "demo-project",
) -> tuple[FakeCosmosClient, cosmos_workspace.CosmosWorkspaceStore]:
    fake_client = _install_fake_cosmos(monkeypatch, database)
    store = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return fake_client, store


def _run_record(run_id: str) -> RunSummary:
    now = workspace.utc_now()
    return RunSummary(
        id=run_id,
        durable_instance_id=f"research-{run_id}",
        project_id="demo-project",
        capability=Capability.GRANT,
        title="Approval run",
        status=RunStatus.WAITING_FOR_APPROVAL,
        progress=80,
        current_stage="Reviewer approval",
        owner="Researcher",
        started_at=now,
        artifact_count=1,
        stages=[
            RunStage(
                id="review",
                label="Review",
                status="waiting_for_approval",
                owner="grant-agent",
            )
        ],
    )


def _add_run_from_record(store: WorkspaceStore, record: RunSummary) -> RunSummary:
    return store.add_run(
        run_id=record.id,
        capability=record.capability,
        title=record.title,
        owner=record.owner,
        status=record.status,
        progress=record.progress,
        current_stage=record.current_stage,
        stages=record.stages,
        artifact_count=record.artifact_count,
    )


def test_workspace_helper_functions_update_and_preserve_stage_state() -> None:
    completed_run = _run_record("run-helper")
    completed_run.stages[0].status = "completed"

    workspace._fail_active_stage(completed_run)

    assert completed_run.stages[0].completed_at is None
    pending_run = _run_record("run-complete")

    workspace._complete_stages(pending_run)

    assert all(stage.status == "completed" for stage in pending_run.stages)
    assert pending_run.stages[0].started_at == pending_run.started_at
    assert pending_run.stages[0].completed_at is not None


def test_workspace_complete_ingestion_updates_item_and_run() -> None:
    store = WorkspaceStore()
    ingested = store.ingest(_ingest_record(), _identity())

    completed = store.complete_ingestion(
        ingested.item.id,
        ingested.run.id,
        evidence_count=7,
        needs_review=False,
    )

    assert completed is not None
    assert completed.item.status == workspace.LibraryStatus.READY
    assert completed.item.evidence_count == 7
    assert completed.item.version == "1.0"
    assert completed.run.status == RunStatus.COMPLETED
    assert completed.run.progress == 100
    assert completed.run.current_stage == "Indexed and ready"
    assert completed.run.completed_at is not None
    assert all(stage.status == "completed" for stage in completed.run.stages)
    assert store.complete_ingestion(
        "missing-item",
        ingested.run.id,
        evidence_count=0,
        needs_review=True,
    ) is None


def test_workspace_failures_update_runs_items_and_pending_approvals() -> None:
    store = WorkspaceStore()
    ingested = store.ingest(_ingest_record(), _identity())

    failed_ingestion = store.fail_ingestion(
        ingested.item.id,
        ingested.run.id,
        "malware detected",
    )

    assert failed_ingestion is not None
    assert failed_ingestion.item.status == workspace.LibraryStatus.BLOCKED
    assert "malware detected" in failed_ingestion.item.description
    assert failed_ingestion.run.status == RunStatus.FAILED
    assert failed_ingestion.run.scheduling_state == "not_managed"
    assert failed_ingestion.run.current_stage == "Ingestion failed"
    assert failed_ingestion.run.completed_at is not None
    assert failed_ingestion.run.stages[1].status == "failed"
    assert store.fail_ingestion("missing-item", ingested.run.id, "ignored") is None


def test_workspace_approval_paths_cover_idempotency_and_terminal_run_states() -> None:
    store = WorkspaceStore()
    with pytest.raises(ValueError, match="Decision must be approved or rejected"):
        ApprovalDecision(decision=ApprovalState.CANCELLED, rationale="No-op")

    approve = ApprovalDecision(
        decision=ApprovalState.APPROVED,
        rationale="Looks correct.",
    )
    reject = ApprovalDecision(
        decision=ApprovalState.REJECTED,
        rationale="Evidence is incomplete.",
    )

    assert store.decide_approval("missing", approve, _identity()) is None

    local_run = _add_run_from_record(store, _run_record("run-local-approval"))
    local_approval = store.add_approval(**_approval_payload(local_run.id))
    approved = store.decide_approval(
        local_approval.id,
        approve,
        _identity("reviewer-1", "Reviewer One"),
    )

    assert approved is not None
    assert approved.state == ApprovalState.APPROVED
    assert approved.approver_id == "reviewer-1"
    assert approved.approver_name == "Reviewer One"
    assert approved.rationale == "Looks correct."
    assert approved.event_delivery == "pending"
    assert approved.decision_event_id == f"decision::{local_approval.id}"
    stored_local_run = store.run(local_run.id)
    assert stored_local_run is not None
    assert stored_local_run.status == RunStatus.COMPLETED
    assert stored_local_run.progress == 100
    assert stored_local_run.current_stage == "Complete"
    assert stored_local_run.completed_at is not None
    assert all(stage.status == "completed" for stage in stored_local_run.stages)

    repeated = store.decide_approval(
        local_approval.id,
        ApprovalDecision(decision=ApprovalState.APPROVED, rationale="Retry"),
        _identity("reviewer-2", "Reviewer Two"),
    )
    assert repeated is not None
    assert repeated.rationale == "Looks correct."
    assert repeated.approver_id == "reviewer-1"
    with pytest.raises(ValueError, match="already been decided differently"):
        store.decide_approval(local_approval.id, reject, _identity("reviewer-3", "Reviewer Three"))

    local_reject_run = _add_run_from_record(store, _run_record("run-local-reject"))
    local_reject_approval = store.add_approval(**_approval_payload(local_reject_run.id))
    rejected_local = store.decide_approval(local_reject_approval.id, reject, _identity("reviewer-6", "Reviewer Six"))
    assert rejected_local is not None
    blocked_local = store.run(local_reject_run.id)
    assert blocked_local is not None
    assert blocked_local.status == RunStatus.BLOCKED
    assert blocked_local.current_stage == "Approval rejected"
    assert blocked_local.completed_at is not None
    assert blocked_local.stages[0].status == "failed"

    orphan_approval = store.add_approval(**_approval_payload("missing-run"))
    orphan_decision = store.decide_approval(orphan_approval.id, reject, _identity("reviewer-7", "Reviewer Seven"))
    assert orphan_decision is not None
    assert orphan_decision.state == ApprovalState.REJECTED
    assert store.run("missing-run") is None


def test_workspace_connector_settings_and_run_replacement_paths() -> None:
    store = WorkspaceStore()
    with pytest.raises(ValueError, match="Unsupported approval event delivery state"):
        store.mark_approval_delivery("approval-grant-export", "queued")
    assert store.mark_approval_delivery("missing-approval", "delivered") is None
    delivered = store.mark_approval_delivery("approval-grant-export", "delivered")
    assert delivered is not None
    assert delivered.event_delivery == "delivered"

    with pytest.raises(ValueError, match="unknown specialist"):
        store.update_connector(
            "openalex",
            ConnectorUpdate(enabled=True, assigned_agents=["unknown"]),
        )
    with pytest.raises(ValueError, match="cannot be disabled"):
        store.update_connector(
            "pubmed",
            ConnectorUpdate(enabled=False, assigned_agents=["literature"]),
        )
    assert store.update_connector("missing", ConnectorUpdate(enabled=True, assigned_agents=[])) is None
    updated_connector = store.update_connector(
        "openalex",
        ConnectorUpdate(enabled=False, assigned_agents=["matching", "dataset"]),
    )
    assert updated_connector is not None
    assert updated_connector.enabled is False
    assert updated_connector.assigned_agents == ["matching", "dataset"]

    assert store.record_connector_test("missing", "failed") is None
    tested_connector = store.record_connector_test("openalex", "failed")
    assert tested_connector is not None
    assert tested_connector.test_status == "failed"
    assert tested_connector.last_tested_at is not None

    assert store.agents()

    settings = store.settings()
    with pytest.raises(ValueError, match="project identifier"):
        store.update_settings(settings.model_copy(update={"project_id": "other-project"}))
    with pytest.raises(ValueError, match="opt-in per run"):
        store.update_settings(settings.model_copy(update={"online_research_default": True}))
    updated_settings = store.update_settings(
        settings.model_copy(update={"description": "Updated workspace description"})
    )
    assert updated_settings.description == "Updated workspace description"

    store.add_run(
        run_id="run-replace",
        capability=Capability.LITERATURE,
        title="First title",
        owner="Researcher",
        status=RunStatus.COMPLETED,
    )
    replaced = store.add_run(
        run_id="run-replace",
        capability=Capability.LITERATURE,
        title="Second title",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=25,
        current_stage="Queued",
    )
    assert replaced.title == "Second title"
    stored = store.run("run-replace")
    assert stored is not None
    assert stored.title == "Second title"
    assert stored.status == RunStatus.RUNNING


def test_cosmos_settings_and_persistence_wrappers_cover_missing_and_success_paths(
    monkeypatch: Any,
) -> None:
    fake_client, store = _make_store(monkeypatch)
    settings_id = next(
        key
        for key, document in fake_client.database.containers["projects"].documents.items()
        if document["documentType"] == "settings"
    )
    fake_client.database.containers["projects"].documents.pop(settings_id)

    assert store.settings().project_id == "demo-project"
    assert store.fail_ingestion("missing-item", "missing-run", "ignored") is None

    ingested = store.ingest(_ingest_record(), _identity())
    failed_ingestion = store.fail_ingestion(ingested.item.id, ingested.run.id, "indexer unavailable")

    assert failed_ingestion is not None
    replica = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    replicated_item = next(item for item in replica.library() if item.id == ingested.item.id)
    assert replicated_item.status == workspace.LibraryStatus.BLOCKED
    replicated_run = replica.run(ingested.run.id)
    assert replicated_run is not None
    assert replicated_run.status == RunStatus.FAILED

    run = store.add_run(
        run_id="run-cosmos-direct",
        capability=Capability.LITERATURE,
        title="Cosmos run",
        owner="Researcher",
        status=RunStatus.WAITING_FOR_APPROVAL,
        progress=80,
        current_stage="Reviewer approval",
    )
    approval = store.add_approval(**_approval_payload(run.id))
    orphan_approval = store.add_approval(**_approval_payload("missing-cosmos-run"))
    decided = store.decide_approval(
        approval.id,
        ApprovalDecision(decision=ApprovalState.APPROVED, rationale="Reviewed"),
        _identity(),
    )
    assert decided is not None

    refreshed = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    refreshed_run = refreshed.run(run.id)
    assert refreshed_run is not None
    assert refreshed_run.approval_id == approval.id
    assert refreshed_run.status == RunStatus.COMPLETED
    refreshed_approval = refreshed.approval(approval.id)
    assert refreshed_approval is not None
    assert refreshed_approval.state == ApprovalState.APPROVED
    assert refreshed.approval(orphan_approval.id) is not None
    assert refreshed.run("missing-cosmos-run") is None


def test_cosmos_decide_approval_handles_missing_idempotent_and_conflict_paths(
    monkeypatch: Any,
) -> None:
    _, store = _make_store(monkeypatch)
    decision = ApprovalDecision(
        decision=ApprovalState.APPROVED,
        rationale="Reviewed package",
    )

    assert store.decide_approval("missing-approval", decision, _identity()) is None
    approved = store.decide_approval("approval-grant-export", decision, _identity("reviewer-1", "Reviewer One"))
    assert approved is not None
    assert approved.state == ApprovalState.APPROVED
    repeated = store.decide_approval(
        "approval-grant-export",
        ApprovalDecision(decision=ApprovalState.APPROVED, rationale="Retry"),
        _identity("reviewer-2", "Reviewer Two"),
    )
    assert repeated is not None
    assert repeated.rationale == "Reviewed package"

    fake_client, same_state_store = _make_store(monkeypatch)

    def conflict_with_same_state(
        container: FakeContainer,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        del etag, match_condition
        container.upsert_item(body)
        raise CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]

    fake_client.database.containers["runs"].on_replace = conflict_with_same_state
    same_state = same_state_store.decide_approval(
        "approval-grant-export",
        decision,
        _identity("reviewer-3", "Reviewer Three"),
    )
    assert same_state is not None
    assert same_state.state == ApprovalState.APPROVED

    fake_client, conflicting_store = _make_store(monkeypatch)

    def conflict_with_other_state(
        container: FakeContainer,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        del etag, match_condition
        concurrent = deepcopy(body)
        concurrent["payload"]["state"] = "rejected"
        concurrent["payload"]["rationale"] = "Concurrent rejection"
        concurrent["payload"]["approver_id"] = "other-reviewer"
        concurrent["payload"]["approver_name"] = "Other Reviewer"
        container.upsert_item(concurrent)
        raise CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]

    fake_client.database.containers["runs"].on_replace = conflict_with_other_state
    with pytest.raises(ValueError, match="decided concurrently"):
        conflicting_store.decide_approval(
            "approval-grant-export",
            decision,
            _identity("reviewer-4", "Reviewer Four"),
        )

    fake_client, failing_store = _make_store(monkeypatch)
    fake_client.database.containers["runs"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=500)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(CosmosHttpResponseError, match="Status code: 500"):
        failing_store.decide_approval(
            "approval-grant-export",
            decision,
            _identity("reviewer-5", "Reviewer Five"),
        )

    _, orphan_store = _make_store(monkeypatch)
    orphan_approval = orphan_store.add_approval(**_approval_payload("missing-run-cosmos"))
    orphan_result = orphan_store.decide_approval(
        orphan_approval.id,
        ApprovalDecision(decision=ApprovalState.REJECTED, rationale="No matching run."),
        _identity("reviewer-6", "Reviewer Six"),
    )
    assert orphan_result is not None
    assert orphan_result.state == ApprovalState.REJECTED
    assert orphan_store.run("missing-run-cosmos") is None


def test_cosmos_decide_approval_returns_none_when_base_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(workspace.WorkspaceStore, "decide_approval", lambda self, approval_id, decision, identity: None)
    _, store = _make_store(monkeypatch)

    result = store.decide_approval(
        "approval-grant-export",
        ApprovalDecision(decision=ApprovalState.APPROVED, rationale="Reviewed package"),
        _identity(),
    )

    assert result is None


def test_cosmos_mark_approval_delivery_handles_conflicts_and_missing_paths(
    monkeypatch: Any,
) -> None:
    _, store = _make_store(monkeypatch)
    assert store.mark_approval_delivery("missing-approval", "delivered") is None
    delivered = store.mark_approval_delivery("approval-grant-export", "delivered")
    assert delivered is not None
    assert delivered.event_delivery == "delivered"

    fake_client, conflicting_store = _make_store(monkeypatch)

    def delivery_conflict(
        container: FakeContainer,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        del etag, match_condition
        concurrent = deepcopy(body)
        concurrent["payload"]["event_delivery"] = "failed"
        container.upsert_item(concurrent)
        raise CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]

    fake_client.database.containers["runs"].on_replace = delivery_conflict
    conflicted = conflicting_store.mark_approval_delivery("approval-grant-export", "delivered")
    assert conflicted is not None
    assert conflicted.event_delivery == "failed"

    fake_client, failing_store = _make_store(monkeypatch)
    fake_client.database.containers["runs"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=500)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(CosmosHttpResponseError, match="Status code: 500"):
        failing_store.mark_approval_delivery("approval-grant-export", "delivered")


def test_cosmos_mark_approval_delivery_returns_none_when_base_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(workspace.WorkspaceStore, "mark_approval_delivery", lambda self, approval_id, delivery: None)
    _, store = _make_store(monkeypatch)

    assert store.mark_approval_delivery("approval-grant-export", "delivered") is None


def test_cosmos_connector_and_settings_wrappers_handle_conflicts(monkeypatch: Any) -> None:
    _, store = _make_store(monkeypatch)
    assert store.update_connector("missing", ConnectorUpdate(enabled=True, assigned_agents=[])) is None
    updated = store.update_connector(
        "openalex",
        ConnectorUpdate(enabled=False, assigned_agents=["matching"]),
    )
    assert updated is not None
    assert updated.assigned_agents == ["matching"]

    fake_client, conflicting_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(ValueError, match="Connector configuration changed concurrently"):
        conflicting_store.update_connector(
            "openalex",
            ConnectorUpdate(enabled=False, assigned_agents=["matching"]),
        )

    fake_client, failing_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=500)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(CosmosHttpResponseError, match="Status code: 500"):
        failing_store.update_connector(
            "openalex",
            ConnectorUpdate(enabled=False, assigned_agents=["matching"]),
        )

    assert store.record_connector_test("missing", "failed") is None
    tested = store.record_connector_test("openalex", "failed")
    assert tested is not None
    assert tested.test_status == "failed"

    fake_client, conflicting_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(ValueError, match="Connector test state changed concurrently"):
        conflicting_store.record_connector_test("openalex", "failed")

    fake_client, failing_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=500)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(CosmosHttpResponseError, match="Status code: 500"):
        failing_store.record_connector_test("openalex", "failed")

    fake_client, missing_settings_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].documents = {
        key: value
        for key, value in fake_client.database.containers["projects"].documents.items()
        if value["documentType"] != "settings"
    }
    with pytest.raises(ValueError, match="settings record is missing"):
        missing_settings_store.update_settings(missing_settings_store.settings())

    fake_client, conflicting_settings_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=412)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(ValueError, match="Project settings changed concurrently"):
        conflicting_settings_store.update_settings(
            conflicting_settings_store.settings().model_copy(update={"description": "Changed"})
        )

    fake_client, failing_settings_store = _make_store(monkeypatch)
    fake_client.database.containers["projects"].on_replace = (
        lambda container, item, body, etag, match_condition: (_ for _ in ()).throw(
            CosmosHttpResponseError(status_code=500)  # type: ignore[no-untyped-call]
        )
    )
    with pytest.raises(CosmosHttpResponseError, match="Status code: 500"):
        failing_settings_store.update_settings(
            failing_settings_store.settings().model_copy(update={"description": "Changed"})
        )


def test_cosmos_connector_wrappers_return_none_when_base_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(workspace.WorkspaceStore, "update_connector", lambda self, connector_id, update: None)
    _, store = _make_store(monkeypatch)
    assert store.update_connector("openalex", ConnectorUpdate(enabled=True, assigned_agents=[])) is None


def test_cosmos_record_connector_test_returns_none_when_base_returns_none(monkeypatch: Any) -> None:
    monkeypatch.setattr(workspace.WorkspaceStore, "record_connector_test", lambda self, connector_id, status: None)
    _, store = _make_store(monkeypatch)
    assert store.record_connector_test("openalex", "ready") is None


def test_build_workspace_store_selects_credentials_and_backend(monkeypatch: Any) -> None:
    local_store = cosmos_workspace.build_workspace_store(
        Settings(workspace_tenant_id="tenant-1", workspace_project_id="project-1")
    )
    assert isinstance(local_store, WorkspaceStore)
    assert local_store.tenant_id == "tenant-1"
    assert local_store.project_id == "project-1"

    default_credential = object()
    managed_credential = object()
    monkeypatch.setattr(cosmos_workspace, "DefaultAzureCredential", lambda: default_credential)
    monkeypatch.setattr(
        cosmos_workspace,
        "ManagedIdentityCredential",
        lambda *, client_id: {"client_id": client_id, "credential": managed_credential},
    )
    captured: list[tuple[str, str, object, str, str]] = []

    def fake_cosmos_workspace_store(
        endpoint: str,
        database_name: str,
        credential: object,
        *,
        tenant_id: str,
        project_id: str,
    ) -> str:
        captured.append((endpoint, database_name, credential, tenant_id, project_id))
        return "cosmos-store"

    monkeypatch.setattr(cosmos_workspace, "CosmosWorkspaceStore", fake_cosmos_workspace_store)

    default_store = cosmos_workspace.build_workspace_store(
        Settings(
            cosmos_endpoint="https://cosmos.example.test/",
            cosmos_database="workspace-db",
            workspace_tenant_id="tenant-1",
            workspace_project_id="project-1",
        )
    )
    managed_store = cosmos_workspace.build_workspace_store(
        Settings(
            cosmos_endpoint="https://cosmos.example.test/",
            cosmos_database="workspace-db",
            managed_identity_client_id="managed-client",
            workspace_tenant_id="tenant-2",
            workspace_project_id="project-2",
        )
    )

    assert default_store == "cosmos-store"  # type: ignore[comparison-overlap]
    assert managed_store == "cosmos-store"  # type: ignore[comparison-overlap]
    assert captured == [
        (
            "https://cosmos.example.test",
            "workspace-db",
            default_credential,
            "tenant-1",
            "project-1",
        ),
        (
            "https://cosmos.example.test",
            "workspace-db",
            {"client_id": "managed-client", "credential": managed_credential},
            "tenant-2",
            "project-2",
        ),
    ]
