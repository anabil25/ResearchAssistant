from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import research_assistant_api.cosmos_workspace as cosmos_workspace
from azure.core.credentials import AccessToken, TokenCredential
from azure.cosmos.exceptions import CosmosHttpResponseError
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ApprovalDecision,
    ApprovalState,
    ConnectorUpdate,
    DatasetApprovalDecisionRequest,
    LibraryIngestRecord,
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
    def __init__(self) -> None:
        self.database = FakeDatabase()

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
    grant_run = second.run("run-grant-001")
    assert grant_run is not None
    assert grant_run.status.value == "completed"
    assert grant_run.progress == 100
    assert grant_run.current_stage == "Complete"
    assert grant_run.completed_at is not None


def test_scheduling_reconciliation_preserves_worker_terminal_state(
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
    run = store.add_run(
        run_id="run-reconcile",
        capability=Capability.LITERATURE,
        title="Reconciliation run",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=10,
        current_stage="Extract",
        scheduler_managed=True,
        orchestration_input={
            "ui_status": "running",
            "ui_progress": 10,
            "ui_current_stage": "Extract",
        },
    )
    store.mark_run_scheduling(run.id, "uncertain")
    document = fake_client.database.containers["runs"].documents[run.id]
    document["payload"].update(
        {
            "status": "completed",
            "progress": 100,
            "current_stage": "Complete",
        }
    )
    store.runs()

    reconciled = store.mark_run_scheduling(run.id, "scheduled")

    assert reconciled is not None
    assert reconciled.status == RunStatus.COMPLETED
    assert reconciled.progress == 100
    assert reconciled.current_stage == "Complete"


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
