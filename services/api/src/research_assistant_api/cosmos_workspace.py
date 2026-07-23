from __future__ import annotations

from typing import Any

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.cosmos.container import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from research_assistant_core.models import Capability, RunStatus

from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalState,
    ConnectorSetting,
    ConnectorUpdate,
    LibraryIngestRecord,
    LibraryIngestResponse,
    LibraryItem,
    ProjectSettings,
    RunStage,
    RunSummary,
    WorkspaceStore,
    WorkspaceSummary,
)


class CosmosWorkspaceStore(WorkspaceStore):
    persistence = "Azure Cosmos DB"

    def __init__(
        self,
        endpoint: str,
        database_name: str,
        credential: TokenCredential,
        tenant_id: str = "demo",
        project_id: str = "demo-project",
    ) -> None:
        super().__init__(tenant_id=tenant_id, project_id=project_id)
        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._projects_container = database.get_container_client("projects")
        self._sources_container = database.get_container_client("sources")
        self._runs_container = database.get_container_client("runs")
        self._load_or_seed()

    def _query(
        self,
        container: ContainerProxy,
        document_type: str,
    ) -> list[dict[str, Any]]:
        return list(
            container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.documentType = @documentType "
                    "AND c.tenantId = @tenantId AND c.projectId = @projectId"
                ),
                parameters=[
                    {"name": "@documentType", "value": document_type},
                    {"name": "@tenantId", "value": self.tenant_id},
                    {"name": "@projectId", "value": self.project_id},
                ],
                enable_cross_partition_query=True,
            )
        )

    def _load_or_seed(self) -> None:
        settings_documents = self._query(self._projects_container, "settings")
        if settings_documents:
            self._settings = ProjectSettings.model_validate(settings_documents[0]["payload"])
        else:
            self._persist_settings(self._settings)

        connector_documents = self._query(self._projects_container, "connector")
        if connector_documents:
            self._connectors = [
                ConnectorSetting.model_validate(document["payload"]) for document in connector_documents
            ]
        else:
            for connector in self._connectors:
                self._persist_connector(connector)

        library_documents = self._query(self._sources_container, "libraryItem")
        if library_documents:
            self._library = [LibraryItem.model_validate(document["payload"]) for document in library_documents]
        else:
            for item in self._library:
                self._persist_library_item(item)

        run_documents = self._query(self._runs_container, "run")
        if run_documents:
            self._runs = [RunSummary.model_validate(document["payload"]) for document in run_documents]
        else:
            for run in self._runs:
                self._persist_run(run)

        approval_documents = self._query(self._runs_container, "approval")
        if approval_documents:
            self._approvals = [ApprovalRecord.model_validate(document["payload"]) for document in approval_documents]
        else:
            for approval in self._approvals:
                self._persist_approval(approval)

    def _persist_settings(self, settings: ProjectSettings) -> None:
        self._projects_container.upsert_item(
            {
                "id": f"settings::{settings.project_id}",
                "documentType": "settings",
                "tenantId": self.tenant_id,
                "projectId": settings.project_id,
                "payload": settings.model_dump(mode="json"),
            }
        )

    def _persist_connector(self, connector: ConnectorSetting) -> None:
        self._projects_container.upsert_item(
            {
                "id": f"connector::{connector.id}",
                "documentType": "connector",
                "tenantId": self.tenant_id,
                "projectId": self.project_id,
                "payload": connector.model_dump(mode="json"),
            }
        )

    def _persist_library_item(self, item: LibraryItem) -> None:
        self._sources_container.upsert_item(
            {
                "id": item.id,
                "documentType": "libraryItem",
                "tenantId": self.tenant_id,
                "projectId": self.project_id,
                "tenantProjectKey": f"{self.tenant_id}|{self.project_id}",
                "payload": item.model_dump(mode="json"),
            }
        )

    def _persist_run(self, run: RunSummary) -> None:
        self._runs_container.upsert_item(
            {
                "id": run.id,
                "documentType": "run",
                "tenantId": self.tenant_id,
                "projectId": run.project_id,
                "tenantRunKey": f"{self.tenant_id}|{run.id}",
                "payload": run.model_dump(mode="json"),
            }
        )

    def _persist_approval(self, approval: ApprovalRecord) -> None:
        self._runs_container.upsert_item(
            {
                "id": approval.id,
                "documentType": "approval",
                "tenantId": self.tenant_id,
                "projectId": self.project_id,
                "runId": approval.run_id,
                "tenantRunKey": f"{self.tenant_id}|{approval.run_id}",
                "payload": approval.model_dump(mode="json"),
            }
        )

    def summary(self) -> WorkspaceSummary:
        self.library()
        self.runs()
        self.approvals()
        return super().summary()

    def library(self) -> list[LibraryItem]:
        documents = self._query(self._sources_container, "libraryItem")
        self._library = [LibraryItem.model_validate(document["payload"]) for document in documents]
        return super().library()

    def runs(self) -> list[RunSummary]:
        documents = self._query(self._runs_container, "run")
        self._runs = [RunSummary.model_validate(document["payload"]) for document in documents]
        return super().runs()

    def run(self, run_id: str) -> RunSummary | None:
        self.runs()
        return super().run(run_id)

    def approvals(self) -> list[ApprovalRecord]:
        documents = self._query(self._runs_container, "approval")
        self._approvals = [ApprovalRecord.model_validate(document["payload"]) for document in documents]
        return super().approvals()

    def approval(self, approval_id: str) -> ApprovalRecord | None:
        self.approvals()
        return super().approval(approval_id)

    def connectors(self) -> list[ConnectorSetting]:
        documents = self._query(self._projects_container, "connector")
        self._connectors = [ConnectorSetting.model_validate(document["payload"]) for document in documents]
        return super().connectors()

    def settings(self) -> ProjectSettings:
        documents = self._query(self._projects_container, "settings")
        if documents:
            self._settings = ProjectSettings.model_validate(documents[0]["payload"])
        return super().settings()

    def ingest(
        self,
        payload: LibraryIngestRecord,
        identity: IdentityContext,
        *,
        scheduler_managed: bool = False,
    ) -> LibraryIngestResponse:
        response = super().ingest(
            payload,
            identity,
            scheduler_managed=scheduler_managed,
        )
        self._persist_library_item(response.item)
        return response

    def fail_ingestion(
        self,
        item_id: str,
        run_id: str,
        reason: str,
    ) -> LibraryIngestResponse | None:
        response = super().fail_ingestion(item_id, run_id, reason)
        if response:
            self._persist_library_item(response.item)
            self._persist_run(response.run)
        return response

    def fail_run(self, run_id: str, reason: str) -> RunSummary | None:
        run = super().fail_run(run_id, reason)
        if run:
            self._persist_run(run)
            for approval in super().approvals():
                if approval.run_id == run_id:
                    self._persist_approval(approval)
        return run

    def set_run_orchestration(
        self,
        run_id: str,
        orchestration_input: dict[str, Any],
    ) -> RunSummary | None:
        run = super().set_run_orchestration(run_id, orchestration_input)
        if run:
            self._persist_run(run)
        return run

    def mark_run_scheduling(
        self,
        run_id: str,
        state: str,
    ) -> RunSummary | None:
        run = super().mark_run_scheduling(run_id, state)
        if run:
            self._persist_run(run)
        return run

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        identity: IdentityContext,
    ) -> ApprovalRecord | None:
        documents = self._query(self._runs_container, "approval")
        document = next(
            (item for item in documents if item["id"] == approval_id),
            None,
        )
        if document is None:
            return None
        self._approvals = [ApprovalRecord.model_validate(item["payload"]) for item in documents]
        before = super().approval(approval_id)
        approval = super().decide_approval(approval_id, decision, identity)
        if approval is None or before is None:
            return approval
        if before.state != ApprovalState.PENDING:
            return approval
        document["payload"] = approval.model_dump(mode="json")
        try:
            self._runs_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            self.approvals()
            current = super().approval(approval_id)
            if current and current.state == decision.decision:
                return current
            raise ValueError("This approval was decided concurrently.") from exc
        run = super().run(approval.run_id)
        if run:
            self._persist_run(run)
        return approval

    def mark_approval_delivery(
        self,
        approval_id: str,
        delivery: str,
    ) -> ApprovalRecord | None:
        documents = self._query(self._runs_container, "approval")
        document = next(
            (item for item in documents if item["id"] == approval_id),
            None,
        )
        if document is None:
            return None
        self._approvals = [ApprovalRecord.model_validate(item["payload"]) for item in documents]
        approval = super().mark_approval_delivery(approval_id, delivery)
        if approval is None:
            return None
        document["payload"] = approval.model_dump(mode="json")
        try:
            self._runs_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            self.approvals()
            return super().approval(approval_id)
        return approval

    def update_connector(
        self,
        connector_id: str,
        update: ConnectorUpdate,
    ) -> ConnectorSetting | None:
        documents = self._query(self._projects_container, "connector")
        document = next(
            (item for item in documents if item["id"] == f"connector::{connector_id}"),
            None,
        )
        if document is None:
            return None
        self._connectors = [ConnectorSetting.model_validate(item["payload"]) for item in documents]
        connector = super().update_connector(connector_id, update)
        if connector:
            document["payload"] = connector.model_dump(mode="json")
            try:
                self._projects_container.replace_item(
                    item=document["id"],
                    body=document,
                    etag=document.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosHttpResponseError as exc:
                if exc.status_code != 412:
                    raise
                raise ValueError("Connector configuration changed concurrently.") from exc
        return connector

    def record_connector_test(
        self,
        connector_id: str,
        status: str,
    ) -> ConnectorSetting | None:
        documents = self._query(self._projects_container, "connector")
        document = next(
            (item for item in documents if item["id"] == f"connector::{connector_id}"),
            None,
        )
        if document is None:
            return None
        self._connectors = [ConnectorSetting.model_validate(item["payload"]) for item in documents]
        connector = super().record_connector_test(connector_id, status)
        if connector:
            document["payload"] = connector.model_dump(mode="json")
            try:
                self._projects_container.replace_item(
                    item=document["id"],
                    body=document,
                    etag=document.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosHttpResponseError as exc:
                if exc.status_code != 412:
                    raise
                raise ValueError("Connector test state changed concurrently.") from exc
        return connector

    def update_settings(self, update: ProjectSettings) -> ProjectSettings:
        documents = self._query(self._projects_container, "settings")
        if not documents:
            raise ValueError("Project settings record is missing.")
        document = documents[0]
        self._settings = ProjectSettings.model_validate(document["payload"])
        settings = super().update_settings(update)
        document["payload"] = settings.model_dump(mode="json")
        try:
            self._projects_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise ValueError("Project settings changed concurrently.") from exc
        return settings

    def add_run(
        self,
        *,
        run_id: str,
        capability: Capability,
        title: str,
        owner: str,
        status: RunStatus = RunStatus.COMPLETED,
        progress: int = 100,
        current_stage: str = "Complete",
        stages: list[RunStage] | None = None,
        artifact_count: int = 1,
        scheduler_managed: bool = False,
        orchestration_input: dict[str, Any] | None = None,
    ) -> RunSummary:
        run = super().add_run(
            run_id=run_id,
            capability=capability,
            title=title,
            owner=owner,
            status=status,
            progress=progress,
            current_stage=current_stage,
            stages=stages,
            artifact_count=artifact_count,
            scheduler_managed=scheduler_managed,
            orchestration_input=orchestration_input,
        )
        self._persist_run(run)
        return run

    def add_approval(
        self,
        *,
        run_id: str,
        title: str,
        gated_action: str,
        destination: str,
        requested_by: str,
        evidence_summary: str,
        risk: str,
    ) -> ApprovalRecord:
        approval = super().add_approval(
            run_id=run_id,
            title=title,
            gated_action=gated_action,
            destination=destination,
            requested_by=requested_by,
            evidence_summary=evidence_summary,
            risk=risk,
        )
        self._persist_approval(approval)
        run = super().run(run_id)
        if run:
            self._persist_run(run)
        return approval


def build_workspace_store(settings: Settings) -> WorkspaceStore:
    if not settings.cosmos_endpoint:
        return WorkspaceStore(
            tenant_id=settings.workspace_tenant_id,
            project_id=settings.workspace_project_id,
        )
    credential: TokenCredential
    if settings.managed_identity_client_id:
        credential = ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    else:
        credential = DefaultAzureCredential()
    return CosmosWorkspaceStore(
        settings.cosmos_endpoint,
        settings.cosmos_database,
        credential,
        tenant_id=settings.workspace_tenant_id,
        project_id=settings.workspace_project_id,
    )
