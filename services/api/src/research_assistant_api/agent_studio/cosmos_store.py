"""Cosmos DB-backed persistence for the Agent Studio metadata store.

Phase 2 partitioning: all project-scoped documents live in a single
dedicated container (``Settings.agent_studio_metadata_container``, default
``agentStudioMetadataV1``) whose partition key path is ``/scope_key`` —
the same synthetic, collision-resistant key computed by
``ScopeContext.scope_key`` for every ``(tenant_id, project_id)`` pair. This
is a brand-new container for a brand-new feature: there is no dual-read
fallback to the legacy ``manifests``/``versions``/``governance`` containers
used before Phase 2, and no cross-partition query is ever issued for
project-scoped data — every query and read is scoped to exactly one
partition (``partition_key=scope.scope_key``), and single-document lookups
prefer a true point ``read_item`` over a query wherever the document id is
deterministic.

``ReleaseGateReport`` is the one document type with no owning
``ScopeContext`` (mirroring ``AgentStudioStore``, which does not scope-check
it either — the caller already validated the owning version's scope before
saving/loading a report). It is stored under a fixed sentinel partition
key so it still fits the single-container/single-partition-key design
without ever being treated as tenant/project data in its own right.

Follows the same pattern as ``cosmos_workspace.CosmosWorkspaceStore``:
subclass the in-memory store, override each method to read-through/
write-through the container, and use optimistic concurrency
(``MatchConditions.IfNotModified`` + etag) for updates that mutate an
existing document (approval decisions, deployment updates, builder
proposal decisions).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
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
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError
from research_assistant_api.config import Settings

#: Fixed sentinel partition for ``ReleaseGateReport`` documents, which have
#: no owning tenant/project of their own (see module docstring). This is a
#: single, well-known partition — reading/writing it is still a point
#: operation, never a cross-partition fan-out.
_GATE_REPORT_SCOPE_KEY = "sk_shared_gate_reports"


class CosmosAgentStudioStore(AgentStudioStore):
    persistence = "Azure Cosmos DB"

    def __init__(
        self,
        endpoint: str,
        database_name: str,
        credential: TokenCredential,
        metadata_container_name: str = "agentStudioMetadataV1",
    ) -> None:
        super().__init__()
        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._container = database.get_container_client(metadata_container_name)

    # -- Low-level helpers ---------------------------------------------

    def _read(self, scope_key: str, document_id: str) -> dict[str, Any] | None:
        """True point read: a single document by id within a single
        partition. Never a query, never cross-partition."""
        try:
            return dict(self._container.read_item(item=document_id, partition_key=scope_key))
        except CosmosResourceNotFoundError:
            return None

    def _query_partition(self, scope_key: str, document_type: str) -> list[dict[str, Any]]:
        """Single-partition query (``partition_key=`` pins it to exactly one
        logical partition) filtered by document type. This is a point query
        within a known partition, not a cross-partition scan."""
        return list(
            self._container.query_items(
                query="SELECT * FROM c WHERE c.documentType = @documentType",
                parameters=[{"name": "@documentType", "value": document_type}],
                partition_key=scope_key,
            )
        )

    def _upsert(self, scope_key: str, document_id: str, document_type: str, payload: dict[str, Any]) -> None:
        self._container.upsert_item(
            {
                "id": document_id,
                "documentType": document_type,
                "scope_key": scope_key,
                "payload": payload,
            }
        )

    # -- Drafts -----------------------------------------------------------

    @staticmethod
    def _draft_id(logical_agent_id: str) -> str:
        return f"draft::{logical_agent_id}"

    def save_draft(self, scope: ScopeContext, draft: AgentDraft) -> AgentDraft:
        super().save_draft(scope, draft)
        self._upsert(scope.scope_key, self._draft_id(draft.logical_agent_id), "draft", draft.model_dump(mode="json"))
        return draft

    def get_draft(self, scope: ScopeContext, logical_agent_id: str) -> AgentDraft | None:
        document = self._read(scope.scope_key, self._draft_id(logical_agent_id))
        if document is None:
            return None
        draft = AgentDraft.model_validate(document["payload"])
        AgentStudioStore.save_draft(self, scope, draft)
        return draft

    def list_drafts(self, scope: ScopeContext) -> tuple[AgentDraft, ...]:
        documents = self._query_partition(scope.scope_key, "draft")
        for document in documents:
            AgentStudioStore.save_draft(self, scope, AgentDraft.model_validate(document["payload"]))
        return super().list_drafts(scope)

    # -- Ownership ----------------------------------------------------------

    @staticmethod
    def _ownership_id(grant: OwnershipGrant) -> str:
        return f"ownership::{grant.logical_agent_id}::{grant.principal_id}::{grant.role.value}"

    def grant_ownership(self, scope: ScopeContext, grant: OwnershipGrant) -> OwnershipGrant:
        super().grant_ownership(scope, grant)
        self._upsert(scope.scope_key, self._ownership_id(grant), "ownership", grant.model_dump(mode="json"))
        return grant

    def list_ownership(self, scope: ScopeContext, logical_agent_id: str) -> tuple[OwnershipGrant, ...]:
        documents = self._query_partition(scope.scope_key, "ownership")
        existing = super().list_ownership(scope, logical_agent_id)
        for document in documents:
            grant = OwnershipGrant.model_validate(document["payload"])
            if grant.logical_agent_id == logical_agent_id and grant not in existing:
                AgentStudioStore.grant_ownership(self, scope, grant)
        return super().list_ownership(scope, logical_agent_id)

    def role_for(self, scope: ScopeContext, logical_agent_id: str, principal_id: str) -> AgentRole | None:
        self.list_ownership(scope, logical_agent_id)
        return super().role_for(scope, logical_agent_id, principal_id)

    # -- Versions -------------------------------------------------------

    #: Bounded retry budget for the sequence-counter compare-and-swap loop.
    #: A 412 (etag mismatch on ``replace_item``) or 409 (conflict on
    #: ``create_item``) means another process/instance won the race for this
    #: increment; we simply re-read the latest counter value and try again.
    #: Sequence *gaps* (a reserved number whose version write never lands,
    #: e.g. the process crashes) are acceptable; sequence *duplicates* are
    #: not, and this loop never returns the same value twice.
    _MAX_SEQUENCE_CAS_RETRIES = 10

    @staticmethod
    def _version_id(version_id: str) -> str:
        return f"version::{version_id}"

    @staticmethod
    def _sequence_counter_id(logical_agent_id: str) -> str:
        return f"sequence-counter::{logical_agent_id}"

    def _sync_versions(self, scope: ScopeContext, logical_agent_id: str) -> None:
        for document in self._query_partition(scope.scope_key, "version"):
            version = AgentVersion.model_validate(document["payload"])
            if version.logical_agent_id != logical_agent_id or version.id in self._versions:
                continue
            try:
                AgentStudioStore.create_version(self, scope, version)
            except AgentStudioStoreError:
                # A concurrent caller on this same instance already synced
                # this exact document into the local cache between our
                # membership check and this call (e.g. two threads racing
                # inside allocate_version) -- the cache ends up consistent
                # either way, so this is a harmless benign race, not a real
                # duplicate-version conflict.
                continue

    def _highest_persisted_sequence(self, scope: ScopeContext, logical_agent_id: str) -> int:
        """Highest ``sequence`` among version documents already persisted for
        this agent, read directly from Cosmos (not the local cache) so a
        freshly-created counter seeds itself correctly even when versions
        were written by ``create_version`` (tests, migration/backfill)
        before the counter document ever existed."""
        highest = 0
        for document in self._query_partition(scope.scope_key, "version"):
            version = AgentVersion.model_validate(document["payload"])
            if version.logical_agent_id == logical_agent_id and version.sequence > highest:
                highest = version.sequence
        return highest

    def _allocate_sequence_cas(self, scope: ScopeContext, logical_agent_id: str) -> int:
        """Atomically reserve the next version sequence number for
        ``(scope.scope_key, logical_agent_id)`` using a dedicated Cosmos
        counter document and an ETag compare-and-swap increment.

        This is the actual source of cross-process/cross-instance
        atomicity: a single counter document per agent, incremented via
        create-if-absent (first sequence) or a ``replace_item`` guarded by
        ``MatchConditions.IfNotModified`` (subsequent sequences). Losing a
        race surfaces as a 409 (someone else just created the counter) or a
        412 (someone else just replaced it); both are retried with a
        bounded budget instead of ever silently reusing a value. The
        in-process ``AgentStudioStore._version_lock`` plays no role in this
        guarantee — it only keeps this instance's local read-through cache
        internally consistent.

        On first creation the counter seeds itself from the highest
        ``sequence`` already persisted for this agent (see
        ``_highest_persisted_sequence``), so it produces a correct next
        value even for an agent whose earlier versions were written via
        ``create_version`` (e.g. migrated/backfilled data) before any
        counter document existed for it.
        """
        counter_id = self._sequence_counter_id(logical_agent_id)
        last_error: CosmosHttpResponseError | None = None
        for _ in range(self._MAX_SEQUENCE_CAS_RETRIES):
            try:
                document = self._container.read_item(item=counter_id, partition_key=scope.scope_key)
            except CosmosResourceNotFoundError:
                seed = self._highest_persisted_sequence(scope, logical_agent_id) + 1
                try:
                    created = self._container.create_item(
                        {
                            "id": counter_id,
                            "documentType": "sequence_counter",
                            "scope_key": scope.scope_key,
                            "logical_agent_id": logical_agent_id,
                            "value": seed,
                        }
                    )
                except CosmosHttpResponseError as exc:
                    if exc.status_code != 409:
                        raise
                    last_error = exc
                    continue
                return int(created["value"])
            else:
                next_value = int(document["value"]) + 1
                document["value"] = next_value
                try:
                    self._container.replace_item(
                        item=counter_id,
                        body=document,
                        etag=document.get("_etag"),
                        match_condition=MatchConditions.IfNotModified,
                    )
                except CosmosHttpResponseError as exc:
                    if exc.status_code != 412:
                        raise
                    last_error = exc
                    continue
                return next_value
        raise AgentStudioStoreError(
            f"Exceeded {self._MAX_SEQUENCE_CAS_RETRIES} retries allocating a version sequence "
            f"for '{logical_agent_id}'."
        ) from last_error

    def allocate_version(
        self,
        scope: ScopeContext,
        logical_agent_id: str,
        builder: Callable[[int], AgentVersion],
    ) -> AgentVersion:
        """Reserve the next sequence via the Cosmos-native CAS counter
        (:meth:`_allocate_sequence_cas`, safe across processes/instances),
        build the immutable version with that exact sequence, then persist
        it locally and write it through to Cosmos.

        The Cosmos counter (not this process's local cache) is the sole
        source of atomicity for the sequence number itself, so no lock is
        held here. ``_sync_versions`` is still called first purely so this
        instance's local cache -- and therefore ``list_versions`` ordering
        -- reflects any versions another process/instance already wrote for
        this agent before appending the newly allocated one.
        """
        self._sync_versions(scope, logical_agent_id)
        sequence = self._allocate_sequence_cas(scope, logical_agent_id)
        version = builder(sequence)
        self._require_scope_match(scope, version.tenant_id, version.project_id)
        if version.sequence != sequence:
            raise AgentStudioStoreError(
                f"Builder returned sequence {version.sequence}, expected atomically-reserved {sequence}."
            )
        AgentStudioStore.create_version(self, scope, version)
        self._upsert(scope.scope_key, self._version_id(version.id), "version", version.model_dump(mode="json"))
        return version

    def create_version(self, scope: ScopeContext, version: AgentVersion) -> AgentVersion:
        super().create_version(scope, version)
        self._upsert(scope.scope_key, self._version_id(version.id), "version", version.model_dump(mode="json"))
        return version

    def get_version(self, scope: ScopeContext, version_id: str) -> AgentVersion | None:
        cached = super().get_version(scope, version_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._version_id(version_id))
        if document is None:
            return None
        version = AgentVersion.model_validate(document["payload"])
        if version.tenant_id != scope.tenant_id or version.project_id != scope.project_id:
            return None
        AgentStudioStore.create_version(self, scope, version)
        return version

    def list_versions(self, scope: ScopeContext, logical_agent_id: str) -> tuple[AgentVersion, ...]:
        self._sync_versions(scope, logical_agent_id)
        return super().list_versions(scope, logical_agent_id)

    # -- Lineage --------------------------------------------------------

    @staticmethod
    def _lineage_id(edge: LineageEdge) -> str:
        return f"lineage::{edge.child_version_id}::{edge.parent_version_id}"

    def add_lineage_edge(self, scope: ScopeContext, edge: LineageEdge) -> LineageEdge:
        super().add_lineage_edge(scope, edge)
        self._upsert(scope.scope_key, self._lineage_id(edge), "lineage", edge.model_dump(mode="json"))
        return edge

    def list_lineage(self, scope: ScopeContext, logical_agent_id: str) -> tuple[LineageEdge, ...]:
        documents = self._query_partition(scope.scope_key, "lineage")
        for document in documents:
            edge = LineageEdge.model_validate(document["payload"])
            if edge not in self._lineage:
                AgentStudioStore.add_lineage_edge(self, scope, edge)
        return super().list_lineage(scope, logical_agent_id)

    # -- Gate reports -----------------------------------------------------
    # No owning ScopeContext (see module docstring) — stored under a fixed
    # sentinel partition key, still a true point read/write.

    @staticmethod
    def _gate_report_id(report_id: str) -> str:
        return f"gate_report::{report_id}"

    def save_gate_report(self, report: ReleaseGateReport) -> ReleaseGateReport:
        super().save_gate_report(report)
        self._upsert(
            _GATE_REPORT_SCOPE_KEY, self._gate_report_id(report.id), "gate_report", report.model_dump(mode="json")
        )
        return report

    def get_gate_report(self, report_id: str) -> ReleaseGateReport | None:
        cached = super().get_gate_report(report_id)
        if cached is not None:
            return cached
        document = self._read(_GATE_REPORT_SCOPE_KEY, self._gate_report_id(report_id))
        if document is None:
            return None
        report = ReleaseGateReport.model_validate(document["payload"])
        AgentStudioStore.save_gate_report(self, report)
        return report

    # -- Releases (append-only lifecycle for an immutable version) ---------

    @staticmethod
    def _release_id(release_id: str) -> str:
        return f"release::{release_id}"

    def create_release(self, scope: ScopeContext, release: AgentRelease) -> AgentRelease:
        super().create_release(scope, release)
        self._upsert(scope.scope_key, self._release_id(release.id), "release", release.model_dump(mode="json"))
        return release

    def _sync_releases(self, scope: ScopeContext, version_id: str) -> None:
        documents = self._query_partition(scope.scope_key, "release")
        for document in documents:
            release = AgentRelease.model_validate(document["payload"])
            if release.version_id == version_id and release.id not in self._releases:
                AgentStudioStore.create_release(self, scope, release)

    def get_release(self, scope: ScopeContext, release_id: str) -> AgentRelease | None:
        cached = super().get_release(scope, release_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._release_id(release_id))
        if document is None:
            return None
        release = AgentRelease.model_validate(document["payload"])
        if release.tenant_id != scope.tenant_id or release.project_id != scope.project_id:
            return None
        AgentStudioStore.create_release(self, scope, release)
        return release

    def list_releases_for_version(self, scope: ScopeContext, version_id: str) -> tuple[AgentRelease, ...]:
        self._sync_releases(scope, version_id)
        return super().list_releases_for_version(scope, version_id)

    def latest_release_for_version(self, scope: ScopeContext, version_id: str) -> AgentRelease | None:
        self._sync_releases(scope, version_id)
        return super().latest_release_for_version(scope, version_id)

    # -- Approvals ------------------------------------------------------

    @staticmethod
    def _approval_id(approval_id: str) -> str:
        return f"approval::{approval_id}"

    def _sync_approvals(self, scope: ScopeContext) -> None:
        documents = self._query_partition(scope.scope_key, "approval")
        for document in documents:
            approval = StudioApprovalRecord.model_validate(document["payload"])
            self._approvals[approval.id] = approval

    def create_approval(self, scope: ScopeContext, record: StudioApprovalRecord) -> StudioApprovalRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        self._sync_approvals(scope)
        existing = self.find_pending_approval(scope, record.idempotency_key)
        if existing is not None:
            return existing
        AgentStudioStore.create_approval(self, scope, record)
        self._upsert(scope.scope_key, self._approval_id(record.id), "approval", record.model_dump(mode="json"))
        return record

    def list_approvals(self, scope: ScopeContext, version_id: str | None = None) -> tuple[StudioApprovalRecord, ...]:
        self._sync_approvals(scope)
        return super().list_approvals(scope, version_id)

    def get_approval(self, scope: ScopeContext, approval_id: str) -> StudioApprovalRecord | None:
        self._sync_approvals(scope)
        return super().get_approval(scope, approval_id)

    def save_approval_decision(self, scope: ScopeContext, record: StudioApprovalRecord) -> StudioApprovalRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        document = self._read(scope.scope_key, self._approval_id(record.id))
        if document is None:
            raise AgentStudioStoreError(f"Approval '{record.id}' not found.")
        current = StudioApprovalRecord.model_validate(document["payload"])
        if current.state.value != "pending":
            raise AgentStudioStoreError(f"Approval '{record.id}' has already been decided.")
        document["payload"] = record.model_dump(mode="json")
        try:
            self._container.replace_item(
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

    @staticmethod
    def _deployment_id(deployment_id: str) -> str:
        return f"deployment::{deployment_id}"

    def create_deployment(self, scope: ScopeContext, record: DeploymentRecord) -> DeploymentRecord:
        super().create_deployment(scope, record)
        self._upsert(scope.scope_key, self._deployment_id(record.id), "deployment", record.model_dump(mode="json"))
        return record

    def list_deployments(self, scope: ScopeContext, logical_agent_id: str) -> tuple[DeploymentRecord, ...]:
        documents = self._query_partition(scope.scope_key, "deployment")
        for document in documents:
            deployment = DeploymentRecord.model_validate(document["payload"])
            if deployment.logical_agent_id == logical_agent_id and deployment.id not in self._deployments:
                AgentStudioStore.create_deployment(self, scope, deployment)
        return super().list_deployments(scope, logical_agent_id)

    def get_deployment(self, scope: ScopeContext, deployment_id: str) -> DeploymentRecord | None:
        cached = super().get_deployment(scope, deployment_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._deployment_id(deployment_id))
        if document is None:
            return None
        deployment = DeploymentRecord.model_validate(document["payload"])
        if deployment.tenant_id != scope.tenant_id or deployment.project_id != scope.project_id:
            return None
        AgentStudioStore.create_deployment(self, scope, deployment)
        return deployment

    def update_deployment(self, scope: ScopeContext, record: DeploymentRecord) -> DeploymentRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        document = self._read(scope.scope_key, self._deployment_id(record.id))
        if document is None:
            raise AgentStudioStoreError(f"Deployment '{record.id}' not found.")
        document["payload"] = record.model_dump(mode="json")
        try:
            self._container.replace_item(
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

    @staticmethod
    def _binding_id(logical_agent_id: str, environment: DeploymentEnvironment) -> str:
        return f"binding::{logical_agent_id}::{environment.value}"

    def set_binding(self, scope: ScopeContext, binding: LogicalAgentBinding) -> LogicalAgentBinding:
        super().set_binding(scope, binding)
        self._upsert(
            scope.scope_key,
            self._binding_id(binding.logical_agent_id, binding.environment),
            "binding",
            binding.model_dump(mode="json"),
        )
        return binding

    def get_binding(
        self,
        scope: ScopeContext,
        logical_agent_id: str,
        environment: DeploymentEnvironment,
    ) -> LogicalAgentBinding | None:
        cached = super().get_binding(scope, logical_agent_id, environment)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._binding_id(logical_agent_id, environment))
        if document is None:
            return None
        binding = LogicalAgentBinding.model_validate(document["payload"])
        AgentStudioStore.set_binding(self, scope, binding)
        return binding

    # -- Tool registrations (runtime handler wiring) -----------------------

    @staticmethod
    def _tool_registration_id(registration_id: str) -> str:
        return f"tool_registration::{registration_id}"

    def create_tool_registration(
        self, scope: ScopeContext, registration: ToolRegistrationSpec
    ) -> ToolRegistrationSpec:
        super().create_tool_registration(scope, registration)
        self._upsert(
            scope.scope_key,
            self._tool_registration_id(registration.id),
            "tool_registration",
            registration.model_dump(mode="json"),
        )
        return registration

    def list_tool_registrations(self, scope: ScopeContext, logical_agent_id: str) -> tuple[ToolRegistrationSpec, ...]:
        documents = self._query_partition(scope.scope_key, "tool_registration")
        for document in documents:
            registration = ToolRegistrationSpec.model_validate(document["payload"])
            if registration.logical_agent_id == logical_agent_id and registration.id not in self._tool_registrations:
                AgentStudioStore.create_tool_registration(self, scope, registration)
        return super().list_tool_registrations(scope, logical_agent_id)

    # -- Builder proposals --------------------------------------------------

    @staticmethod
    def _builder_proposal_id(proposal_id: str) -> str:
        return f"builder_proposal::{proposal_id}"

    def create_builder_proposal(self, scope: ScopeContext, proposal: BuilderProposal) -> BuilderProposal:
        super().create_builder_proposal(scope, proposal)
        self._upsert(
            scope.scope_key,
            self._builder_proposal_id(proposal.id),
            "builder_proposal",
            proposal.model_dump(mode="json"),
        )
        return proposal

    def list_builder_proposals(self, scope: ScopeContext, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        documents = self._query_partition(scope.scope_key, "builder_proposal")
        for document in documents:
            proposal = BuilderProposal.model_validate(document["payload"])
            if proposal.logical_agent_id == logical_agent_id and proposal.id not in self._builder_proposals:
                AgentStudioStore.create_builder_proposal(self, scope, proposal)
        return super().list_builder_proposals(scope, logical_agent_id)

    def get_builder_proposal(self, scope: ScopeContext, proposal_id: str) -> BuilderProposal | None:
        cached = super().get_builder_proposal(scope, proposal_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._builder_proposal_id(proposal_id))
        if document is None:
            return None
        proposal = BuilderProposal.model_validate(document["payload"])
        if proposal.tenant_id != scope.tenant_id or proposal.project_id != scope.project_id:
            return None
        AgentStudioStore.create_builder_proposal(self, scope, proposal)
        return proposal

    def save_builder_proposal_decision(self, scope: ScopeContext, proposal: BuilderProposal) -> BuilderProposal:
        self._require_scope_match(scope, proposal.tenant_id, proposal.project_id)
        document = self._read(scope.scope_key, self._builder_proposal_id(proposal.id))
        if document is None:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' not found.")
        current = BuilderProposal.model_validate(document["payload"])
        if current.state != BuilderProposalState.PENDING:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' has already been decided.")
        document["payload"] = proposal.model_dump(mode="json")
        try:
            self._container.replace_item(
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
        settings.agent_studio_metadata_container,
    )


__all__ = [
    "CosmosAgentStudioStore",
    "build_agent_studio_store",
]
