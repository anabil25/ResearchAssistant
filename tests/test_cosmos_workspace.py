from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import research_assistant_api.cosmos_workspace as cosmos_workspace
import research_assistant_api.workspace as workspace
from azure.core.credentials import AccessToken, TokenCredential
from azure.cosmos.exceptions import CosmosHttpResponseError
from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ApprovalDecision,
    ApprovalState,
    ConnectorUpdate,
    LibraryIngestRecord,
    RunStage,
    RunSummary,
    WorkspaceStore,
)
from research_assistant_core.models import Capability, RunStatus

ReplaceCallback = Callable[["FakeContainer", str, dict[str, Any], str | None, Any], dict[str, Any]]


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
    def __init__(self, *, on_replace: ReplaceCallback | None = None) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.version = 0
        self.on_replace = on_replace

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
        assert self.documents[item]["_etag"] == etag
        if self.on_replace is not None:
            return self.on_replace(self, item, body, etag, match_condition)
        return self.upsert_item(body)

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, str]],
        enable_cross_partition_query: bool,
    ) -> list[dict[str, Any]]:
        del query, enable_cross_partition_query
        values = {item["name"]: item["value"] for item in parameters}
        return [
            deepcopy(item)
            for item in self.documents.values()
            if item["documentType"] == values["@documentType"]
            and item["tenantId"] == values["@tenantId"]
            and item["projectId"] == values["@projectId"]
        ]


class FakeDatabase:
    def __init__(
        self,
        *,
        projects: FakeContainer | None = None,
        sources: FakeContainer | None = None,
        runs: FakeContainer | None = None,
    ) -> None:
        self.containers = {
            "projects": projects or FakeContainer(),
            "sources": sources or FakeContainer(),
            "runs": runs or FakeContainer(),
        }

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers[name]


class FakeCosmosClient:
    def __init__(self, database: FakeDatabase | None = None) -> None:
        self.database = database or FakeDatabase()

    def get_database_client(self, _name: str) -> FakeDatabase:
        return self.database


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


def _run_record(run_id: str, *, scheduler_managed: bool = False) -> RunSummary:
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
        scheduler_managed=scheduler_managed,
        scheduling_state="pending" if scheduler_managed else "not_managed",
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
        scheduler_managed=record.scheduler_managed,
        orchestration_input=record.orchestration_input,
    )


def test_cosmos_workspace_seeds_and_reloads_operational_state(
    monkeypatch: Any,
) -> None:
    fake_client, first = _make_store(monkeypatch)

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
        FakeCredential(),
    )

    settings = first.settings().model_copy(update={"description": "Persisted project description"})
    first.update_settings(settings)
    first.update_connector(
        "openalex",
        ConnectorUpdate(enabled=True, assigned_agents=["matching"]),
    )
    assert live_replica.settings().description == "Persisted project description"
    assert next(item for item in live_replica.connectors() if item.id == "openalex").assigned_agents == [
        "matching"
    ]
    ingested = first.ingest(
        _ingest_record(),
        _identity(),
    )
    first.decide_approval(
        "approval-grant-export",
        ApprovalDecision(
            decision=ApprovalState.APPROVED,
            rationale="The exact package and destination were reviewed.",
        ),
        _identity(
            user_id="reviewer-1",
            display_name="Reviewer One",
            groups=("grant-reviewers",),
        ),
    )

    second = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
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
    _, store = _make_store(monkeypatch)
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
    document = store._runs_container.documents[run.id]  # type: ignore[attr-defined]
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


def test_workspace_failures_update_runs_items_and_pending_approvals() -> None:
    store = WorkspaceStore()
    ingested = store.ingest(_ingest_record(), _identity(), scheduler_managed=True)

    failed_ingestion = store.fail_ingestion(
        ingested.item.id,
        ingested.run.id,
        "malware detected",
    )

    assert failed_ingestion is not None
    assert failed_ingestion.item.status == workspace.LibraryStatus.BLOCKED
    assert "malware detected" in failed_ingestion.item.description
    assert failed_ingestion.run.status == RunStatus.FAILED
    assert failed_ingestion.run.scheduling_state == "failed"
    assert failed_ingestion.run.current_stage == "Scheduling failed"
    assert failed_ingestion.run.completed_at is not None
    assert failed_ingestion.run.stages[1].status == "failed"
    assert store.fail_ingestion("missing-item", ingested.run.id, "ignored") is None

    failed_run = store.fail_run("run-grant-001", "scheduler offline")

    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    cancelled = store.approval("approval-grant-export")
    assert cancelled is not None
    assert cancelled.state == ApprovalState.CANCELLED
    assert cancelled.rationale == "scheduler offline"
    assert cancelled.decided_at is not None
    assert store.fail_run("missing-run", "ignored") is None

def test_workspace_scheduling_and_orchestration_validate_inputs() -> None:
    store = WorkspaceStore()
    local_run = store.add_run(
        run_id="run-local",
        capability=Capability.LITERATURE,
        title="Local run",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=5,
        current_stage="Plan",
        scheduler_managed=False,
    )
    updated_local = store.set_run_orchestration(local_run.id, {"ui_status": "running"})
    assert updated_local is not None
    assert updated_local.scheduling_state == "not_managed"

    managed_run = store.add_run(
        run_id="run-managed",
        capability=Capability.LITERATURE,
        title="Managed run",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=35,
        current_stage="Extract",
        scheduler_managed=True,
        orchestration_input={
            "ui_status": 1,
            "ui_current_stage": ["Extract"],
            "ui_progress": "35",
        },
        stages=[
            RunStage(
                id="extract",
                label="Extract evidence",
                status="running",
                owner="worker",
            )
        ],
    )
    updated_managed = store.set_run_orchestration(
        managed_run.id,
        {
            "ui_status": 1,
            "ui_current_stage": ["Extract"],
            "ui_progress": "35",
        },
    )
    assert updated_managed is not None
    assert updated_managed.scheduling_state == "pending"

    uncertain = store.mark_run_scheduling(managed_run.id, "uncertain")
    assert uncertain is not None
    assert uncertain.status == RunStatus.PLANNED
    assert uncertain.current_stage == "Scheduling reconciliation required"

    scheduled = store.mark_run_scheduling(managed_run.id, "scheduled")
    assert scheduled is not None
    assert scheduled.scheduling_state == "scheduled"
    assert scheduled.status == RunStatus.PLANNED
    assert scheduled.current_stage == "Scheduling reconciliation required"
    assert scheduled.progress == 35

    failed = store.mark_run_scheduling(managed_run.id, "failed")
    assert failed is not None
    assert failed.status == RunStatus.FAILED
    assert failed.current_stage == "Scheduling failed"
    assert failed.completed_at is not None
    assert failed.stages[0].status == "failed"

    reconciled_run = store.add_run(
        run_id="run-reconciled",
        capability=Capability.LITERATURE,
        title="Reconciled run",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=22,
        current_stage="Queue",
        scheduler_managed=True,
        orchestration_input={
            "ui_status": "running",
            "ui_current_stage": "Queue",
            "ui_progress": 22,
        },
    )
    store.mark_run_scheduling(reconciled_run.id, "uncertain")
    restored = store.mark_run_scheduling(reconciled_run.id, "scheduled")
    assert restored is not None
    assert restored.status == RunStatus.RUNNING
    assert restored.current_stage == "Queue"
    assert restored.progress == 22

    assert store.set_run_orchestration("missing-run", {"ui_status": "running"}) is None
    assert store.mark_run_scheduling("missing-run", "scheduled") is None
    with pytest.raises(ValueError, match="Unsupported run scheduling state"):
        store.mark_run_scheduling(managed_run.id, "queued")


def test_workspace_approval_paths_cover_idempotency_local_and_scheduler_managed_runs() -> None:
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

    managed_run = _add_run_from_record(store, _run_record("run-managed-approval", scheduler_managed=True))
    managed_approval = store.add_approval(**_approval_payload(managed_run.id))
    managed_result = store.decide_approval(managed_approval.id, approve, _identity("reviewer-4", "Reviewer Four"))
    assert managed_result is not None
    managed_stored = store.run(managed_run.id)
    assert managed_stored is not None
    assert managed_stored.status == RunStatus.RUNNING
    assert managed_stored.current_stage == "Approved action queued"
    assert managed_stored.completed_at is None

    managed_reject_run = _add_run_from_record(store, _run_record("run-managed-reject", scheduler_managed=True))
    managed_reject_approval = store.add_approval(**_approval_payload(managed_reject_run.id))
    store.decide_approval(managed_reject_approval.id, reject, _identity("reviewer-5", "Reviewer Five"))
    rejected_managed = store.run(managed_reject_run.id)
    assert rejected_managed is not None
    assert rejected_managed.status == RunStatus.BLOCKED
    assert rejected_managed.current_stage == "Approval rejected"

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
        run_id="run-cosmos-orchestration",
        capability=Capability.LITERATURE,
        title="Cosmos run",
        owner="Researcher",
        status=RunStatus.RUNNING,
        progress=15,
        current_stage="Extract",
        scheduler_managed=True,
    )
    assert store.set_run_orchestration("missing-run", {"ui_status": "running"}) is None
    updated = store.set_run_orchestration(run.id, {"ui_status": "running"})
    assert updated is not None
    assert updated.orchestration_input == {"ui_status": "running"}
    assert store.mark_run_scheduling("missing-run", "scheduled") is None
    scheduled = store.mark_run_scheduling(run.id, "scheduled")
    assert scheduled is not None
    assert scheduled.scheduling_state == "scheduled"

    approval = store.add_approval(**_approval_payload(run.id))
    orphan_approval = store.add_approval(**_approval_payload("missing-cosmos-run"))
    failed_run = store.fail_run(run.id, "scheduler down")
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED

    refreshed = cosmos_workspace.CosmosWorkspaceStore(
        "https://cosmos.example.test",
        "research",
        FakeCredential(),
    )
    refreshed_run = refreshed.run(run.id)
    assert refreshed_run is not None
    assert refreshed_run.approval_id == approval.id
    refreshed_approval = refreshed.approval(approval.id)
    assert refreshed_approval is not None
    assert refreshed_approval.state == ApprovalState.CANCELLED
    assert refreshed.approval(orphan_approval.id) is not None
    assert refreshed.run("missing-cosmos-run") is None
    assert store.fail_run("missing-run", "ignored") is None

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
