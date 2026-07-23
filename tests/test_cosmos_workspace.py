from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import research_assistant_api.cosmos_workspace as cosmos_workspace
from azure.core.credentials import AccessToken, TokenCredential
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ApprovalDecision,
    ApprovalState,
    ConnectorUpdate,
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
