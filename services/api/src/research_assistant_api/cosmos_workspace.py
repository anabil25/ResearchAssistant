from __future__ import annotations

from hashlib import sha256
from threading import RLock
from typing import Any
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.cosmos import CosmosClient
from azure.cosmos.container import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_core.azure_auth import azure_credential
from research_assistant_core.models import Capability, RunStatus

from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    DEFAULT_PROJECT_DESCRIPTION,
    DEFAULT_PROJECT_NAME,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalState,
    ChatThread,
    ChatThreadConflictError,
    ConnectorSetting,
    ConnectorUpdate,
    DatasetApprovalAuditEntry,
    DatasetApprovalDecisionRequest,
    DatasetApprovalDenialReason,
    DatasetApprovalError,
    DatasetApprovalRequest,
    DatasetApprovalState,
    DatasetSendOutcome,
    LibraryIngestRecord,
    LibraryIngestResponse,
    LibraryItem,
    PersonalProject,
    PersonalProjectCreate,
    PersonalProjectUpdate,
    ProjectLifecycle,
    ProjectSettings,
    RunStage,
    RunSummary,
    WorkspaceStore,
    WorkspaceSummary,
    default_project_settings,
    reconcile_required_connectors,
    utc_now,
)


class WorkspaceProjectUnavailableError(ValueError):
    """The requested project is not an active workspace owned by this caller.

    Callers intentionally receive one non-enumerating error for missing,
    archived, cross-tenant, and foreign project IDs.
    """


class WorkspaceProjectProvider:
    """Project catalog and workspace-store selection boundary."""

    def list_projects(self, identity: IdentityContext) -> tuple[PersonalProject, ...]:
        raise NotImplementedError

    def create_project(self, identity: IdentityContext, payload: PersonalProjectCreate) -> PersonalProject:
        raise NotImplementedError

    def update_project(
        self,
        identity: IdentityContext,
        project_id: str,
        payload: PersonalProjectUpdate,
    ) -> PersonalProject:
        raise NotImplementedError

    def select_project(self, identity: IdentityContext, project_id: str) -> PersonalProject:
        raise NotImplementedError

    def workspace_for(self, identity: IdentityContext, project_id: str | None) -> WorkspaceStore:
        raise NotImplementedError

    def active_project_id(self, identity: IdentityContext) -> str | None:
        raise NotImplementedError

    def stores_for_reconciliation(self) -> tuple[WorkspaceStore, ...]:
        raise NotImplementedError


class CosmosWorkspaceStore(WorkspaceStore):
    persistence = "Azure Cosmos DB"

    def __init__(
        self,
        endpoint: str,
        database_name: str,
        credential: TokenCredential,
        tenant_id: str,
        project_id: str,
        *,
        initial_settings: ProjectSettings | None = None,
    ) -> None:
        super().__init__(
            tenant_id=tenant_id,
            project_id=project_id,
            project_name=initial_settings.name if initial_settings is not None else DEFAULT_PROJECT_NAME,
            project_description=(
                initial_settings.description
                if initial_settings is not None
                else DEFAULT_PROJECT_DESCRIPTION
            ),
        )
        if initial_settings is not None:
            self._settings = initial_settings
        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._projects_container = database.get_container_client("projects")
        self._sources_container = database.get_container_client("sources")
        self._runs_container = database.get_container_client("runs")
        self._load_or_initialize()

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

    def _load_or_initialize(self) -> None:
        settings_documents = self._query(self._projects_container, "settings")
        if settings_documents:
            self._settings = ProjectSettings.model_validate(settings_documents[0]["payload"])
        else:
            self._persist_settings(self._settings)

        connector_documents = self._query(self._projects_container, "connector")
        if connector_documents:
            loaded_connectors = [
                ConnectorSetting.model_validate(document["payload"]) for document in connector_documents
            ]
            self._connectors, changed_connectors = reconcile_required_connectors(
                loaded_connectors
            )
            for connector in changed_connectors:
                self._persist_connector(connector)
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

        dataset_approval_documents = self._query(self._runs_container, "dataset_approval")
        if dataset_approval_documents:
            self._reload_dataset_state(dataset_approval_documents)

        for document in self._query(self._runs_container, "chat_thread"):
            thread = ChatThread.model_validate(document["payload"])
            self._chat_threads[thread.id] = thread

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

    def _chat_thread_document(self, thread: ChatThread) -> dict[str, Any]:
        return {
            "id": f"chat-thread::{thread.id}",
            "documentType": "chat_thread",
            "tenantId": self.tenant_id,
            "projectId": thread.project_id,
            "tenantRunKey": f"{self.tenant_id}|{thread.id}",
            "payload": thread.model_dump(mode="json"),
        }

    def _read_chat_thread_document(self, thread_id: str) -> dict[str, Any] | None:
        try:
            document = self._runs_container.read_item(
                item=f"chat-thread::{thread_id}",
                partition_key=f"{self.tenant_id}|{thread_id}",
            )
        except CosmosResourceNotFoundError:
            return None
        if (
            document.get("documentType") != "chat_thread"
            or document.get("tenantId") != self.tenant_id
            or document.get("projectId") != self.project_id
        ):
            return None
        return document

    def chat_thread(self, thread_id: str, *, owner_principal_id: str) -> ChatThread | None:
        """Point-read the durable thread so every replica sees the latest turn."""
        with self._lock:
            document = self._read_chat_thread_document(thread_id)
            if document is None:
                self._chat_threads.pop(thread_id, None)
                return None
            record = ChatThread.model_validate(document["payload"]).model_copy(
                update={"storage_etag": document.get("_etag")}
            )
            self._chat_threads[thread_id] = record
            if record.owner_principal_id != owner_principal_id:
                return None
            return record.model_copy(deep=True)

    def save_chat_thread(self, thread: ChatThread) -> ChatThread:
        if thread.tenant_id != self.tenant_id or thread.project_id != self.project_id:
            raise ValueError("A chat thread cannot move between workspaces.")
        with self._lock:
            existing = self._read_chat_thread_document(thread.id)
            if existing is not None:
                current = ChatThread.model_validate(existing["payload"])
                if current.owner_principal_id != thread.owner_principal_id:
                    raise ValueError("A chat thread cannot change owner.")
                if not thread.storage_etag or existing.get("_etag") != thread.storage_etag:
                    raise ChatThreadConflictError(
                        "The chat thread changed while this turn was running."
                    )
            record = thread.model_copy(deep=True, update={"updated_at": utc_now()})
            document = self._chat_thread_document(record)
            try:
                if existing is None:
                    persisted = self._runs_container.create_item(document)
                else:
                    persisted = self._runs_container.replace_item(
                        item=existing["id"],
                        body=document,
                        etag=existing.get("_etag"),
                        match_condition=MatchConditions.IfNotModified,
                    )
            except CosmosHttpResponseError as exc:
                if exc.status_code not in {404, 409, 412}:
                    raise
                raise ChatThreadConflictError(
                    "The chat thread changed while this turn was running."
                ) from exc
            if isinstance(persisted, dict):
                record = record.model_copy(update={"storage_etag": persisted.get("_etag")})
            self._chat_threads[record.id] = record
            return record.model_copy(deep=True)

    def _persist_dataset_approval(
        self,
        record: DatasetApprovalRequest,
        *,
        requester_principal_id: str | None,
    ) -> None:
        self._runs_container.upsert_item(
            self._dataset_approval_document(
                record,
                requester_principal_id=requester_principal_id,
                audit_entries=[],
            )
        )

    def _dataset_approval_document(
        self,
        record: DatasetApprovalRequest,
        *,
        requester_principal_id: str | None,
        audit_entries: list[DatasetApprovalAuditEntry],
    ) -> dict[str, Any]:
        """Serialize an approval, its requester principal, and its audit trail
        into a single document.

        The audit/outbox entries live *inside* the same document as the state
        payload so a reviewer decision or a consumption and its audit intent are
        written in one atomic ETag-guarded write -- there is no separate audit
        record that a crash could drop after the state mutation landed. The
        requester principal id is stored top-level (outside ``payload``) so it
        durably backs separation-of-duties without widening the public read
        model returned to project members. Both the requester principal and the
        audit entries are passed as immutable local snapshots (never read back
        from the shared instance caches, which a concurrent reload could clobber
        between a mutation and its persistence).
        """
        document: dict[str, Any] = {
            "id": record.id,
            "documentType": "dataset_approval",
            "tenantId": self.tenant_id,
            "projectId": self.project_id,
            "payload": record.model_dump(mode="json"),
            "auditTrail": [entry.model_dump(mode="json") for entry in audit_entries],
        }
        if requester_principal_id is not None:
            document["requesterPrincipalId"] = requester_principal_id
        return document

    def _reload_dataset_state(self, documents: list[dict[str, Any]]) -> None:
        """Rebuild the approval records, requester principals, and audit trail
        from the authoritative Cosmos documents, discarding any un-persisted
        in-memory state (e.g. an optimistic mutation whose ETag CAS write lost
        the race). Callers hold ``self._lock`` for the whole query-mutate-persist
        cycle so a reload here cannot interleave with another operation's
        in-flight mutation on the shared caches."""
        self._dataset_approvals = [
            DatasetApprovalRequest.model_validate(document["payload"]) for document in documents
        ]
        self._dataset_requester_principals = {
            document["id"]: document["requesterPrincipalId"]
            for document in documents
            if document.get("requesterPrincipalId") is not None
        }
        self._dataset_audit = [
            DatasetApprovalAuditEntry.model_validate(entry)
            for document in documents
            for entry in document.get("auditTrail", [])
        ]
        # Continue the monotonic counter past everything already persisted, so a
        # replica that appends after a cold load cannot reuse a sequence and
        # invert causal order within a trail.
        self._dataset_audit_sequence = max(
            (entry.sequence for entry in self._dataset_audit), default=0
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

    def dataset_approval_requests(self) -> list[DatasetApprovalRequest]:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().dataset_approval_requests()

    def dataset_approval_request(self, request_id: str) -> DatasetApprovalRequest | None:
        with self._lock:
            self.dataset_approval_requests()
            return super().dataset_approval_request(request_id)

    def create_dataset_approval_request(
        self,
        *,
        plan_fingerprint: str,
        filename: str,
        objective: str,
        requested_by: str,
        ttl_minutes: int,
        requested_by_principal_id: str | None = None,
    ) -> DatasetApprovalRequest:
        with self._lock:
            record = super().create_dataset_approval_request(
                plan_fingerprint=plan_fingerprint,
                filename=filename,
                objective=objective,
                requested_by=requested_by,
                ttl_minutes=ttl_minutes,
                requested_by_principal_id=requested_by_principal_id,
            )
            # Persist the requester principal from the local create-time value,
            # never from the shared cache, so a concurrent reload cannot erase it
            # (which would silently disable separation-of-duties for this record).
            self._persist_dataset_approval(record, requester_principal_id=requested_by_principal_id)
            return record

    def decide_dataset_approval_request(
        self,
        request_id: str,
        decision: DatasetApprovalDecisionRequest,
        identity: IdentityContext,
    ) -> DatasetApprovalRequest | None:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            document = next((item for item in documents if item["id"] == request_id), None)
            if document is None:
                return None
            self._reload_dataset_state(documents)
            record = super().decide_dataset_approval_request(request_id, decision, identity)
            if record is None:
                return None
            document["payload"] = record.model_dump(mode="json")
            document["auditTrail"] = self._audit_documents_for(request_id)
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
                self.dataset_approval_requests()
                current = super().dataset_approval_request(request_id)
                if current is not None and current.state.value == decision.decision:
                    return current
                raise DatasetApprovalError(
                    DatasetApprovalDenialReason.CONCURRENT_CONFLICT,
                    "This dataset approval request was decided concurrently.",
                ) from exc
            return record

    def validate_dataset_approval_request(
        self,
        request_id: str,
        *,
        plan_fingerprint: str,
        consumed_by_principal_id: str | None = None,
    ) -> DatasetApprovalRequest:
        """Fresh-read, non-mutating fail-fast. Writes nothing, so a losing race
        here costs only a rejected request, never a spent approval."""
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().validate_dataset_approval_request(
                request_id,
                plan_fingerprint=plan_fingerprint,
                consumed_by_principal_id=consumed_by_principal_id,
            )

    def consume_dataset_approval_request(
        self,
        request_id: str,
        *,
        plan_fingerprint: str,
        invocation_id: str,
        consumed_by_principal_id: str | None = None,
    ) -> DatasetApprovalRequest:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            document = next((item for item in documents if item["id"] == request_id), None)
            if document is None:
                raise DatasetApprovalError(
                    DatasetApprovalDenialReason.NOT_FOUND,
                    "Dataset approval request was not found.",
                )
            self._reload_dataset_state(documents)
            record = super().consume_dataset_approval_request(
                request_id,
                plan_fingerprint=plan_fingerprint,
                invocation_id=invocation_id,
                consumed_by_principal_id=consumed_by_principal_id,
            )
            document["payload"] = record.model_dump(mode="json")
            document["auditTrail"] = self._audit_documents_for(request_id)
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
                # Consumption is strictly single-use: a concurrent winner already
                # mutated this exact record between our fresh read and this
                # write, so this attempt must fail closed rather than risk
                # granting a second invocation for the same decided approval.
                raise DatasetApprovalError(
                    DatasetApprovalDenialReason.CONCURRENT_CONFLICT,
                    "Dataset approval request was concurrently consumed or modified; "
                    "refusing to grant a second invocation.",
                ) from exc
            except (ServiceRequestError, ServiceResponseError) as exc:
                # Transport-level ambiguity: the write may or may not have landed.
                # Never blindly retry (that risks a second invocation) and never
                # assume success. Reconcile against the authoritative store: only
                # treat it as consumed if this exact invocation id is what durably
                # won; otherwise fail closed.
                return self._reconcile_consumption(request_id, invocation_id, exc)
            return record

    def _audit_documents_for(self, request_id: str) -> list[dict[str, Any]]:
        return [
            entry.model_dump(mode="json")
            for entry in self._dataset_audit
            if entry.request_id == request_id
        ]

    def _reconcile_consumption(
        self,
        request_id: str,
        invocation_id: str,
        exc: Exception,
    ) -> DatasetApprovalRequest:
        with self._lock:
            self.dataset_approval_requests()
            current = super().dataset_approval_request(request_id)
            if (
                current is not None
                and current.state == DatasetApprovalState.CONSUMED
                and current.consumed_invocation_id == invocation_id
            ):
                return current
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.CONCURRENT_CONFLICT,
                "Dataset approval consumption outcome was unknown and did not durably "
                "record this invocation; refusing to grant an unverified invocation.",
            ) from exc

    def record_dataset_send_outcome(
        self,
        request_id: str,
        *,
        invocation_id: str,
        plan_fingerprint: str,
        delivered: bool,
        actor_principal_id: str,
    ) -> DatasetApprovalAuditEntry:
        """Durably append the send-outcome entry to the approval document.

        Uses a bounded ETag-CAS retry rather than a blind write or a silent
        drop: by this point the approval is already CONSUMED, so contention on
        this document is effectively nil, and surfacing an exhausted retry is
        preferable to losing the entry that distinguishes
        attempted-and-delivered from attempted-and-failed.
        """
        attempts = 3
        for attempt in range(attempts):
            with self._lock:
                documents = self._query(self._runs_container, "dataset_approval")
                document = next((item for item in documents if item["id"] == request_id), None)
                self._reload_dataset_state(documents)
                entry = super().record_dataset_send_outcome(
                    request_id,
                    invocation_id=invocation_id,
                    plan_fingerprint=plan_fingerprint,
                    delivered=delivered,
                    actor_principal_id=actor_principal_id,
                )
                if document is None:
                    return entry
                document["auditTrail"] = self._audit_documents_for(request_id)
                try:
                    self._runs_container.replace_item(
                        item=document["id"],
                        body=document,
                        etag=document.get("_etag"),
                        match_condition=MatchConditions.IfNotModified,
                    )
                except CosmosHttpResponseError as exc:
                    if exc.status_code != 412 or attempt == attempts - 1:
                        raise
                    continue
                return entry
        raise AssertionError("unreachable: bounded retry always returns or raises")

    def dataset_send_outcome(
        self,
        request_id: str,
        *,
        invocation_id: str | None = None,
    ) -> DatasetSendOutcome:
        """Fresh-read, fail-closed. A document that cannot be read back yields
        ``UNKNOWN`` rather than an optimistic ``DELIVERED``."""
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().dataset_send_outcome(request_id, invocation_id=invocation_id)

    def dataset_approvals_blocked_by_requester_attribution(self) -> list[DatasetApprovalRequest]:
        """Fresh-read enumeration of the migration surface.

        SCOPE WARNING: bound to THIS (tenant, project) pair by ``_query``, so it
        under-reports the fleet. See the base implementation for the
        cross-partition query an operator must run to size the real population.
        """
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().dataset_approvals_blocked_by_requester_attribution()

    def dataset_approvals_invalidated_by_fingerprint_version(self) -> list[DatasetApprovalRequest]:
        """Fresh-read enumeration. SCOPE WARNING: single (tenant, project) only;
        see the base implementation for the cross-partition query."""
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().dataset_approvals_invalidated_by_fingerprint_version()

    def dataset_approval_audit(self) -> list[DatasetApprovalAuditEntry]:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().dataset_approval_audit()

    def pending_dataset_approval_audit(self) -> list[DatasetApprovalAuditEntry]:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            return super().pending_dataset_approval_audit()

    def mark_dataset_approval_audit_delivered(self, entry_id: str) -> DatasetApprovalAuditEntry | None:
        with self._lock:
            documents = self._query(self._runs_container, "dataset_approval")
            self._reload_dataset_state(documents)
            entry = super().mark_dataset_approval_audit_delivered(entry_id)
            if entry is None:
                return None
            document = next((item for item in documents if item["id"] == entry.request_id), None)
            if document is None:
                return entry
            document["auditTrail"] = self._audit_documents_for(entry.request_id)
            self._runs_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
            return entry

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
    ) -> LibraryIngestResponse:
        response = super().ingest(payload, identity)
        self._persist_library_item(response.item)
        return response

    def complete_ingestion(
        self,
        item_id: str,
        run_id: str,
        *,
        evidence_count: int,
        needs_review: bool,
    ) -> LibraryIngestResponse | None:
        response = super().complete_ingestion(
            item_id,
            run_id,
            evidence_count=evidence_count,
            needs_review=needs_review,
        )
        if response:
            self._persist_library_item(response.item)
            self._persist_run(response.run)
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


class CosmosWorkspaceProjectProvider(WorkspaceProjectProvider):
    """Cosmos-backed catalog for personal workspaces.

    Catalog, preference, and legacy-template documents all reside in the
    already-deployed ``research/projects`` container. Source, run, approval,
    and Blob records keep their existing project-specific locations.
    """

    _PROJECT_DOCUMENT_TYPE = "personalProject"
    _PREFERENCE_DOCUMENT_TYPE = "personalProjectPreference"
    _TEMPLATE_DOCUMENT_TYPE = "projectTemplate"

    def __init__(
        self,
        endpoint: str,
        database_name: str,
        credential: TokenCredential,
        *,
        tenant_id: str,
        template_project_id: str,
    ) -> None:
        self._endpoint = endpoint
        self._database_name = database_name
        self._credential = credential
        self._tenant_id = tenant_id
        self._template_project_id = template_project_id
        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._projects_container = database.get_container_client("projects")
        self._stores: dict[tuple[str, str], WorkspaceStore] = {}
        self._lock = RLock()

    def _require_tenant(self, identity: IdentityContext) -> None:
        if identity.tenant_id != self._tenant_id:
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")

    @staticmethod
    def _project_document_id(project_id: str) -> str:
        return f"project::{project_id}"

    @staticmethod
    def _preference_document_id(user_id: str) -> str:
        return f"preference::{sha256(user_id.encode('utf-8')).hexdigest()}"

    def _read_document(self, document_id: str, tenant_id: str) -> dict[str, Any] | None:
        try:
            return dict(self._projects_container.read_item(item=document_id, partition_key=tenant_id))
        except CosmosResourceNotFoundError:
            return None

    def _project_document(self, project: PersonalProject) -> dict[str, Any]:
        return {
            "id": self._project_document_id(project.project_id),
            "documentType": self._PROJECT_DOCUMENT_TYPE,
            "tenantId": self._tenant_id,
            "projectId": project.project_id,
            "ownerUserId": project.owner_user_id,
            "lifecycle": project.lifecycle.value,
            "payload": project.model_dump(mode="json"),
        }

    def _get_owned_document(
        self,
        identity: IdentityContext,
        project_id: str,
    ) -> tuple[PersonalProject, dict[str, Any]]:
        self._require_tenant(identity)
        document = self._read_document(self._project_document_id(project_id), identity.tenant_id)
        if document is None or document.get("documentType") != self._PROJECT_DOCUMENT_TYPE:
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")
        project = PersonalProject.model_validate(document["payload"])
        if (
            project.owner_user_id != identity.user_id
            or project.lifecycle is not ProjectLifecycle.ACTIVE
            or project.project_id != project_id
        ):
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")
        return project, document

    def _template_settings(self) -> ProjectSettings:
        """Bootstrap an unowned template without changing legacy data."""
        template_id = f"template::{self._template_project_id}"
        template = self._read_document(template_id, self._tenant_id)
        if template is not None and template.get("documentType") == self._TEMPLATE_DOCUMENT_TYPE:
            return ProjectSettings.model_validate(template["payload"]["settings"])

        legacy_settings = self._read_document(
            f"settings::{self._template_project_id}",
            self._tenant_id,
        )
        settings = (
            ProjectSettings.model_validate(legacy_settings["payload"])
            if legacy_settings is not None and legacy_settings.get("documentType") == "settings"
            else default_project_settings(self._template_project_id)
        )
        self._projects_container.upsert_item(
            {
                "id": template_id,
                "documentType": self._TEMPLATE_DOCUMENT_TYPE,
                "tenantId": self._tenant_id,
                "templateProjectId": self._template_project_id,
                "payload": {
                    "project_id": self._template_project_id,
                    "name": settings.name,
                    "description": settings.description,
                    "settings": settings.model_dump(mode="json"),
                },
            }
        )
        return settings

    def _preference(self, identity: IdentityContext) -> str | None:
        document = self._read_document(self._preference_document_id(identity.user_id), identity.tenant_id)
        if document is None or document.get("documentType") != self._PREFERENCE_DOCUMENT_TYPE:
            return None
        if document.get("userId") != identity.user_id:
            return None
        active_project_id = document.get("activeProjectId")
        return active_project_id if isinstance(active_project_id, str) else None

    def _set_preference(self, identity: IdentityContext, project_id: str | None) -> None:
        self._projects_container.upsert_item(
            {
                "id": self._preference_document_id(identity.user_id),
                "documentType": self._PREFERENCE_DOCUMENT_TYPE,
                "tenantId": identity.tenant_id,
                "userId": identity.user_id,
                "activeProjectId": project_id,
                "updatedAt": utc_now().isoformat(),
            }
        )

    def _store_for_project(
        self,
        project: PersonalProject,
        *,
        initial_settings: ProjectSettings | None = None,
    ) -> WorkspaceStore:
        key = (self._tenant_id, project.project_id)
        with self._lock:
            store = self._stores.get(key)
            if store is None:
                store = CosmosWorkspaceStore(
                    self._endpoint,
                    self._database_name,
                    self._credential,
                    tenant_id=self._tenant_id,
                    project_id=project.project_id,
                    initial_settings=initial_settings,
                )
                self._stores[key] = store
            return store

    def list_projects(self, identity: IdentityContext) -> tuple[PersonalProject, ...]:
        self._require_tenant(identity)
        documents = self._projects_container.query_items(
            query=(
                "SELECT * FROM c WHERE c.documentType = @documentType "
                "AND c.ownerUserId = @ownerUserId AND c.lifecycle = @lifecycle"
            ),
            parameters=[
                {"name": "@documentType", "value": self._PROJECT_DOCUMENT_TYPE},
                {"name": "@ownerUserId", "value": identity.user_id},
                {"name": "@lifecycle", "value": ProjectLifecycle.ACTIVE.value},
            ],
            partition_key=identity.tenant_id,
        )
        projects = [PersonalProject.model_validate(document["payload"]) for document in documents]
        return tuple(sorted(projects, key=lambda project: (project.updated_at, project.project_id), reverse=True))

    def create_project(self, identity: IdentityContext, payload: PersonalProjectCreate) -> PersonalProject:
        self._require_tenant(identity)
        template_settings = self._template_settings()
        now = utc_now()
        project = PersonalProject(
            project_id=f"project-{uuid4().hex}",
            owner_user_id=identity.user_id,
            name=payload.name,
            description=payload.description,
            created_at=now,
            updated_at=now,
            template_project_id=self._template_project_id,
        )
        self._projects_container.create_item(self._project_document(project))
        settings = template_settings.model_copy(
            update={
                "project_id": project.project_id,
                "name": project.name,
                "description": project.description,
            }
        )
        self._store_for_project(project, initial_settings=settings)
        self._set_preference(identity, project.project_id)
        return project

    def update_project(
        self,
        identity: IdentityContext,
        project_id: str,
        payload: PersonalProjectUpdate,
    ) -> PersonalProject:
        project, document = self._get_owned_document(identity, project_id)
        updated = project.model_copy(
            update={
                "name": payload.name if payload.name is not None else project.name,
                "description": payload.description if payload.description is not None else project.description,
                "lifecycle": ProjectLifecycle.ARCHIVED if payload.archive else project.lifecycle,
                "updated_at": utc_now(),
            }
        )
        self._projects_container.replace_item(
            item=document["id"],
            body=self._project_document(updated),
            etag=document.get("_etag"),
            match_condition=MatchConditions.IfNotModified,
        )
        if payload.name is not None or payload.description is not None:
            workspace_store = self._store_for_project(project)
            workspace_store.update_settings(
                workspace_store.settings().model_copy(
                    update={"name": updated.name, "description": updated.description}
                )
            )
        if payload.archive and self._preference(identity) == project_id:
            self._set_preference(identity, None)
        return updated

    def select_project(self, identity: IdentityContext, project_id: str) -> PersonalProject:
        project, _ = self._get_owned_document(identity, project_id)
        self._set_preference(identity, project_id)
        return project

    def active_project_id(self, identity: IdentityContext) -> str | None:
        self._require_tenant(identity)
        active_project_id = self._preference(identity)
        if active_project_id is None:
            return None
        try:
            self._get_owned_document(identity, active_project_id)
        except WorkspaceProjectUnavailableError:
            return None
        return active_project_id

    def workspace_for(self, identity: IdentityContext, project_id: str | None) -> WorkspaceStore:
        self._require_tenant(identity)
        selected_project_id = project_id or self.active_project_id(identity)
        if selected_project_id is None:
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")
        project, _ = self._get_owned_document(identity, selected_project_id)
        return self._store_for_project(project)

    def stores_for_reconciliation(self) -> tuple[WorkspaceStore, ...]:
        documents = self._projects_container.query_items(
            query="SELECT * FROM c WHERE c.documentType = @documentType AND c.lifecycle = @lifecycle",
            parameters=[
                {"name": "@documentType", "value": self._PROJECT_DOCUMENT_TYPE},
                {"name": "@lifecycle", "value": ProjectLifecycle.ACTIVE.value},
            ],
            partition_key=self._tenant_id,
        )
        return tuple(
            self._store_for_project(PersonalProject.model_validate(document["payload"]))
            for document in documents
        )


def build_workspace_project_provider(settings: Settings) -> WorkspaceProjectProvider:
    return CosmosWorkspaceProjectProvider(
        settings.cosmos_endpoint,
        settings.cosmos_database,
        azure_credential(settings.managed_identity_client_id),
        tenant_id=settings.workspace_tenant_id,
        template_project_id=settings.workspace_project_id,
    )
