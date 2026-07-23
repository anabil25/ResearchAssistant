"""Cosmos DB-backed persistence for the Agent Studio metadata store.

Follows the same pattern as ``cosmos_workspace.CosmosWorkspaceStore``:
subclass the in-memory store, override each method to read-through/
write-through named containers, and use optimistic concurrency
(``MatchConditions.IfNotModified`` + etag) for updates that mutate an
existing document (version status transitions, approval decisions,
deployment health/rollback).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.cosmos.container import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentRelease,
    AgentRole,
    AgentVersion,
    BuilderProposal,
    BuilderProposalState,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    StudioApprovalRecord,
    ToolRegistrationSpec,
)
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError
from research_assistant_api.config import Settings


class CosmosAgentStudioStore(AgentStudioStore):
    persistence = "Azure Cosmos DB"

    def __init__(self, endpoint: str, database_name: str, credential: TokenCredential) -> None:
        super().__init__()
        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._manifests_container = database.get_container_client("manifests")
        self._versions_container = database.get_container_client("versions")
        self._governance_container = database.get_container_client("governance")

    def _query(self, container: ContainerProxy, document_type: str, tenant_id: str) -> list[dict[str, Any]]:
        return list(
            container.query_items(
                query="SELECT * FROM c WHERE c.documentType = @documentType AND c.tenantId = @tenantId",
                parameters=[
                    {"name": "@documentType", "value": document_type},
                    {"name": "@tenantId", "value": tenant_id},
                ],
                enable_cross_partition_query=True,
            )
        )

    # -- Drafts -----------------------------------------------------------

    def save_draft(self, draft: AgentDraft) -> AgentDraft:
        super().save_draft(draft)
        self._manifests_container.upsert_item(
            {
                "id": f"draft::{draft.tenant_id}::{draft.logical_agent_id}",
                "documentType": "draft",
                "tenantId": draft.tenant_id,
                "logicalAgentId": draft.logical_agent_id,
                "payload": draft.model_dump(mode="json"),
            }
        )
        return draft

    def get_draft(self, tenant_id: str, logical_agent_id: str) -> AgentDraft | None:
        documents = self._query(self._manifests_container, "draft", tenant_id)
        for document in documents:
            AgentStudioStore.save_draft(self, AgentDraft.model_validate(document["payload"]))
        return super().get_draft(tenant_id, logical_agent_id)

    def list_drafts(self, tenant_id: str) -> tuple[AgentDraft, ...]:
        documents = self._query(self._manifests_container, "draft", tenant_id)
        for document in documents:
            AgentStudioStore.save_draft(self, AgentDraft.model_validate(document["payload"]))
        return super().list_drafts(tenant_id)

    # -- Ownership ----------------------------------------------------------

    def grant_ownership(self, grant: OwnershipGrant) -> OwnershipGrant:
        super().grant_ownership(grant)
        document_id = (
            f"ownership::{grant.tenant_id}::{grant.logical_agent_id}::{grant.principal_id}::{grant.role.value}"
        )
        self._manifests_container.upsert_item(
            {
                "id": document_id,
                "documentType": "ownership",
                "tenantId": grant.tenant_id,
                "logicalAgentId": grant.logical_agent_id,
                "payload": grant.model_dump(mode="json"),
            }
        )
        return grant

    def list_ownership(self, tenant_id: str, logical_agent_id: str) -> tuple[OwnershipGrant, ...]:
        documents = self._query(self._manifests_container, "ownership", tenant_id)
        for document in documents:
            grant = OwnershipGrant.model_validate(document["payload"])
            if grant not in self._ownership.get((grant.tenant_id, grant.logical_agent_id), []):
                AgentStudioStore.grant_ownership(self, grant)
        return super().list_ownership(tenant_id, logical_agent_id)

    def role_for(
        self,
        tenant_id: str,
        logical_agent_id: str,
        principal_id: str,
        *,
        project_id: str | None = None,
    ) -> AgentRole | None:
        self.list_ownership(tenant_id, logical_agent_id)
        return super().role_for(tenant_id, logical_agent_id, principal_id, project_id=project_id)

    # -- Versions -------------------------------------------------------

    def create_version(self, version: AgentVersion) -> AgentVersion:
        super().create_version(version)
        self._versions_container.upsert_item(
            {
                "id": version.id,
                "documentType": "version",
                "tenantId": version.tenant_id,
                "logicalAgentId": version.logical_agent_id,
                "payload": version.model_dump(mode="json"),
            }
        )
        return version

    def allocate_version(
        self,
        tenant_id: str,
        logical_agent_id: str,
        builder: Callable[[int], AgentVersion],
    ) -> AgentVersion:
        """Atomic sequence allocation, then write-through to Cosmos.

        Reuses the in-memory lock-guarded allocation for the actual sequence
        reservation (single-process atomicity), then persists the resulting
        version the same way ``create_version`` does. Multi-process
        allocation races are out of scope for this pass — see the Phase 1
        coordination note on atomic sequence allocation.
        """
        version = super().allocate_version(tenant_id, logical_agent_id, builder)
        self._versions_container.upsert_item(
            {
                "id": version.id,
                "documentType": "version",
                "tenantId": version.tenant_id,
                "logicalAgentId": version.logical_agent_id,
                "payload": version.model_dump(mode="json"),
            }
        )
        return version

    def list_versions(self, tenant_id: str, logical_agent_id: str) -> tuple[AgentVersion, ...]:
        documents = self._query(self._versions_container, "version", tenant_id)
        for document in documents:
            version = AgentVersion.model_validate(document["payload"])
            if version.id not in self._versions:
                AgentStudioStore.create_version(self, version)
        return super().list_versions(tenant_id, logical_agent_id)

    def get_version(self, tenant_id: str, version_id: str) -> AgentVersion | None:
        cached = super().get_version(tenant_id, version_id)
        if cached is not None:
            return cached
        documents = self._query(self._versions_container, "version", tenant_id)
        document = next((item for item in documents if item["id"] == version_id), None)
        if document is None:
            return None
        version = AgentVersion.model_validate(document["payload"])
        AgentStudioStore.create_version(self, version)
        return version

    # -- Lineage --------------------------------------------------------

    def add_lineage_edge(self, edge: LineageEdge) -> LineageEdge:
        super().add_lineage_edge(edge)
        self._versions_container.upsert_item(
            {
                "id": f"lineage::{edge.tenant_id}::{edge.child_version_id}::{edge.parent_version_id}",
                "documentType": "lineage",
                "tenantId": edge.tenant_id,
                "payload": edge.model_dump(mode="json"),
            }
        )
        return edge

    def list_lineage(self, tenant_id: str, logical_agent_id: str) -> tuple[LineageEdge, ...]:
        documents = self._query(self._versions_container, "lineage", tenant_id)
        for document in documents:
            edge = LineageEdge.model_validate(document["payload"])
            if edge not in self._lineage:
                AgentStudioStore.add_lineage_edge(self, edge)
        return super().list_lineage(tenant_id, logical_agent_id)

    # -- Gate reports -----------------------------------------------------

    def save_gate_report(self, report: ReleaseGateReport) -> ReleaseGateReport:
        super().save_gate_report(report)
        self._versions_container.upsert_item(
            {
                "id": f"gate::{report.id}",
                "documentType": "gate_report",
                "tenantId": "_shared",
                "payload": report.model_dump(mode="json"),
            }
        )
        return report

    def get_gate_report(self, report_id: str) -> ReleaseGateReport | None:
        cached = super().get_gate_report(report_id)
        if cached is not None:
            return cached
        documents = list(
            self._versions_container.query_items(
                query="SELECT * FROM c WHERE c.documentType = @documentType AND c.id = @id",
                parameters=[
                    {"name": "@documentType", "value": "gate_report"},
                    {"name": "@id", "value": f"gate::{report_id}"},
                ],
                enable_cross_partition_query=True,
            )
        )
        if not documents:
            return None
        report = ReleaseGateReport.model_validate(documents[0]["payload"])
        AgentStudioStore.save_gate_report(self, report)
        return report

    # -- Releases (append-only lifecycle for an immutable version) ---------

    def create_release(self, release: AgentRelease) -> AgentRelease:
        super().create_release(release)
        self._versions_container.upsert_item(
            {
                "id": f"release::{release.id}",
                "documentType": "release",
                "tenantId": release.tenant_id,
                "versionId": release.version_id,
                "payload": release.model_dump(mode="json"),
            }
        )
        return release

    def _sync_releases(self, tenant_id: str, version_id: str) -> None:
        documents = self._query(self._versions_container, "release", tenant_id)
        for document in documents:
            release = AgentRelease.model_validate(document["payload"])
            if release.version_id != version_id:
                continue
            if release.id not in self._releases:
                AgentStudioStore.create_release(self, release)

    def get_release(self, tenant_id: str, release_id: str) -> AgentRelease | None:
        cached = super().get_release(tenant_id, release_id)
        if cached is not None:
            return cached
        documents = list(
            self._versions_container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.documentType = @documentType "
                    "AND c.id = @id AND c.tenantId = @tenantId"
                ),
                parameters=[
                    {"name": "@documentType", "value": "release"},
                    {"name": "@id", "value": f"release::{release_id}"},
                    {"name": "@tenantId", "value": tenant_id},
                ],
                enable_cross_partition_query=True,
            )
        )
        if not documents:
            return None
        release = AgentRelease.model_validate(documents[0]["payload"])
        AgentStudioStore.create_release(self, release)
        return release

    def list_releases_for_version(self, tenant_id: str, version_id: str) -> tuple[AgentRelease, ...]:
        self._sync_releases(tenant_id, version_id)
        return super().list_releases_for_version(tenant_id, version_id)

    def latest_release_for_version(self, tenant_id: str, version_id: str) -> AgentRelease | None:
        self._sync_releases(tenant_id, version_id)
        return super().latest_release_for_version(tenant_id, version_id)

    # -- Approvals ------------------------------------------------------

    def create_approval(self, record: StudioApprovalRecord) -> StudioApprovalRecord:
        self.list_approvals(record.tenant_id)
        existing = self.find_pending_approval(record.tenant_id, record.idempotency_key)
        if existing is not None:
            return existing
        AgentStudioStore.create_approval(self, record)
        self._governance_container.upsert_item(
            {
                "id": record.id,
                "documentType": "approval",
                "tenantId": record.tenant_id,
                "versionId": record.version_id,
                "payload": record.model_dump(mode="json"),
            }
        )
        return record

    def list_approvals(self, tenant_id: str, version_id: str | None = None) -> tuple[StudioApprovalRecord, ...]:
        documents = self._query(self._governance_container, "approval", tenant_id)
        for document in documents:
            approval = StudioApprovalRecord.model_validate(document["payload"])
            self._approvals[approval.id] = approval
        return super().list_approvals(tenant_id, version_id)

    def get_approval(self, tenant_id: str, approval_id: str) -> StudioApprovalRecord | None:
        self.list_approvals(tenant_id)
        return super().get_approval(tenant_id, approval_id)

    def save_approval_decision(self, record: StudioApprovalRecord) -> StudioApprovalRecord:
        documents = self._query(self._governance_container, "approval", record.tenant_id)
        document = next((item for item in documents if item["id"] == record.id), None)
        if document is None:
            raise AgentStudioStoreError(f"Approval '{record.id}' not found.")
        current = StudioApprovalRecord.model_validate(document["payload"])
        if current.state.value != "pending":
            raise AgentStudioStoreError(f"Approval '{record.id}' has already been decided.")
        document["payload"] = record.model_dump(mode="json")
        try:
            self._governance_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise AgentStudioStoreError(f"Approval '{record.id}' was decided concurrently.") from exc
        self._approvals[record.id] = record
        return record

    # -- Deployments ------------------------------------------------------

    def create_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        super().create_deployment(record)
        self._governance_container.upsert_item(
            {
                "id": record.id,
                "documentType": "deployment",
                "tenantId": record.tenant_id,
                "logicalAgentId": record.logical_agent_id,
                "payload": record.model_dump(mode="json"),
            }
        )
        return record

    def list_deployments(self, tenant_id: str, logical_agent_id: str) -> tuple[DeploymentRecord, ...]:
        documents = self._query(self._governance_container, "deployment", tenant_id)
        for document in documents:
            deployment = DeploymentRecord.model_validate(document["payload"])
            if deployment.id not in self._deployments:
                AgentStudioStore.create_deployment(self, deployment)
        return super().list_deployments(tenant_id, logical_agent_id)

    def get_deployment(self, tenant_id: str, deployment_id: str) -> DeploymentRecord | None:
        cached = super().get_deployment(tenant_id, deployment_id)
        if cached is not None:
            return cached
        documents = self._query(self._governance_container, "deployment", tenant_id)
        document = next((item for item in documents if item["id"] == deployment_id), None)
        if document is None:
            return None
        deployment = DeploymentRecord.model_validate(document["payload"])
        AgentStudioStore.create_deployment(self, deployment)
        return deployment

    def update_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        documents = self._query(self._governance_container, "deployment", record.tenant_id)
        document = next((item for item in documents if item["id"] == record.id), None)
        if document is None:
            raise AgentStudioStoreError(f"Deployment '{record.id}' not found.")
        document["payload"] = record.model_dump(mode="json")
        try:
            self._governance_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise AgentStudioStoreError(f"Deployment '{record.id}' changed concurrently.") from exc
        self._deployments[record.id] = record
        return record

    # -- Logical agent bindings ----------------------------------------

    def set_binding(self, binding: LogicalAgentBinding) -> LogicalAgentBinding:
        super().set_binding(binding)
        self._governance_container.upsert_item(
            {
                "id": f"binding::{binding.tenant_id}::{binding.logical_agent_id}::{binding.environment.value}",
                "documentType": "binding",
                "tenantId": binding.tenant_id,
                "logicalAgentId": binding.logical_agent_id,
                "payload": binding.model_dump(mode="json"),
            }
        )
        return binding

    def get_binding(
        self,
        tenant_id: str,
        logical_agent_id: str,
        environment: DeploymentEnvironment,
    ) -> LogicalAgentBinding | None:
        cached = super().get_binding(tenant_id, logical_agent_id, environment)
        if cached is not None:
            return cached
        documents = self._query(self._governance_container, "binding", tenant_id)
        document = next(
            (
                item
                for item in documents
                if item["id"] == f"binding::{tenant_id}::{logical_agent_id}::{environment.value}"
            ),
            None,
        )
        if document is None:
            return None
        binding = LogicalAgentBinding.model_validate(document["payload"])
        AgentStudioStore.set_binding(self, binding)
        return binding

    # -- Tool registrations (runtime handler wiring) -----------------------

    def create_tool_registration(self, registration: ToolRegistrationSpec) -> ToolRegistrationSpec:
        super().create_tool_registration(registration)
        self._governance_container.upsert_item(
            {
                "id": registration.id,
                "documentType": "tool_registration",
                "tenantId": registration.tenant_id,
                "logicalAgentId": registration.logical_agent_id,
                "payload": registration.model_dump(mode="json"),
            }
        )
        return registration

    def list_tool_registrations(self, tenant_id: str, logical_agent_id: str) -> tuple[ToolRegistrationSpec, ...]:
        documents = self._query(self._governance_container, "tool_registration", tenant_id)
        for document in documents:
            registration = ToolRegistrationSpec.model_validate(document["payload"])
            if registration.id not in self._tool_registrations:
                AgentStudioStore.create_tool_registration(self, registration)
        return super().list_tool_registrations(tenant_id, logical_agent_id)

    # -- Builder proposals --------------------------------------------------

    def create_builder_proposal(self, proposal: BuilderProposal) -> BuilderProposal:
        super().create_builder_proposal(proposal)
        self._governance_container.upsert_item(
            {
                "id": proposal.id,
                "documentType": "builder_proposal",
                "tenantId": proposal.tenant_id,
                "logicalAgentId": proposal.logical_agent_id,
                "payload": proposal.model_dump(mode="json"),
            }
        )
        return proposal

    def _reload_builder_proposals(self, tenant_id: str) -> None:
        documents = self._query(self._governance_container, "builder_proposal", tenant_id)
        for document in documents:
            proposal = BuilderProposal.model_validate(document["payload"])
            if proposal.id not in self._builder_proposals:
                AgentStudioStore.create_builder_proposal(self, proposal)

    def list_builder_proposals(self, tenant_id: str, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        self._reload_builder_proposals(tenant_id)
        return super().list_builder_proposals(tenant_id, logical_agent_id)

    def get_builder_proposal(self, tenant_id: str, proposal_id: str) -> BuilderProposal | None:
        self._reload_builder_proposals(tenant_id)
        return super().get_builder_proposal(tenant_id, proposal_id)

    def save_builder_proposal_decision(self, proposal: BuilderProposal) -> BuilderProposal:
        documents = self._query(self._governance_container, "builder_proposal", proposal.tenant_id)
        document = next((item for item in documents if item["id"] == proposal.id), None)
        if document is None:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' not found.")
        current = BuilderProposal.model_validate(document["payload"])
        if current.state != BuilderProposalState.PENDING:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' has already been decided.")
        document["payload"] = proposal.model_dump(mode="json")
        try:
            self._governance_container.replace_item(
                item=document["id"],
                body=document,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' was decided concurrently.") from exc
        self._builder_proposals[proposal.id] = proposal
        return proposal


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_agent_studio_store(settings: Settings) -> AgentStudioStore:
    """Production factory.

    Returns a Cosmos-backed store when ``cosmos_endpoint`` is configured.
    When it is not configured, metadata persistence is explicitly
    unavailable in production: callers must not silently fall back to
    ``AgentStudioStore`` (in-memory) outside of tests.
    """
    if not settings.cosmos_endpoint:
        raise AgentStudioStoreError(
            "No Azure Cosmos DB endpoint is configured; Agent Studio metadata persistence is unavailable."
        )
    return CosmosAgentStudioStore(
        settings.cosmos_endpoint,
        settings.agent_studio_cosmos_database,
        _credential(settings.managed_identity_client_id),
    )
