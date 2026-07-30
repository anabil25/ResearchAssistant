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

``ReleaseGateReport`` documents are partitioned exactly like every other
project-scoped document (``/scope_key``), keyed by an opaque
``gate_report::{report_id}`` document id within that partition -- there is
no shared/sentinel partition for gate reports; a report can only be read
back within the scope that created it.

Follows the same pattern as ``cosmos_workspace.CosmosWorkspaceStore``:
subclass the in-memory store, override each method to read-through/
write-through the container, and use optimistic concurrency
(``MatchConditions.IfNotModified`` + etag) for updates that mutate an
existing document (approval decisions, deployment updates, builder
proposal decisions).
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalConsumptionRecord,
    ApprovalRevocation,
    BuilderProposal,
    BuilderProposalState,
    DeploymentEnvironment,
    DeploymentRecord,
    EvaluationRun,
    EvaluationSuite,
    IdempotencyClaim,
    IdempotencyClaimDisposition,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    PlaygroundTestRun,
    ReleaseGateReport,
    StudioApprovalRecord,
    ToolRegistrationSpec,
    utc_now,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import (
    AgentStudioStore,
    AgentStudioStoreError,
    DraftConflictError,
    IdempotencyConcurrencyError,
    IdempotencyNotFoundError,
    ReleaseSuccessorConflictError,
    hash_idempotency_token,
    is_idempotency_lease_expired,
    validate_idempotency_lease_seconds,
)
from research_assistant_api.config import Settings


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

    def save_draft(self, scope: ScopeContext, draft: AgentDraft, *, expected_etag: str | None = None) -> AgentDraft:
        """Optimistic-concurrency draft save.

        When ``expected_etag`` is supplied, this reads the *current* Cosmos
        document first and (a) fails fast if its stored ``AgentDraft.etag``
        no longer matches, then (b) performs the actual write with
        ``MatchConditions.IfNotModified`` pinned to Cosmos's own ``_etag``,
        so a concurrent write racing between our read and our write is still
        caught by Cosmos itself (authoritative across processes/instances),
        not just our in-process check.
        """
        document_id = self._draft_id(draft.logical_agent_id)
        document = self._read(scope.scope_key, document_id)
        if expected_etag is not None:
            current = AgentDraft.model_validate(document["payload"]) if document is not None else None
            if current is None or current.etag != expected_etag:
                raise DraftConflictError(
                    f"Draft '{draft.logical_agent_id}' was modified concurrently; the supplied "
                    "etag no longer matches the stored draft. Re-fetch and retry."
                )
        body = {
            "id": document_id,
            "documentType": "draft",
            "scope_key": scope.scope_key,
            "payload": draft.model_dump(mode="json"),
        }
        if document is not None:
            try:
                self._container.replace_item(
                    item=document_id,
                    body=body,
                    etag=document.get("_etag"),
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosHttpResponseError as exc:
                if exc.status_code != 412:
                    raise
                raise DraftConflictError(
                    f"Draft '{draft.logical_agent_id}' was modified concurrently; re-fetch and retry."
                ) from exc
        else:
            self._container.upsert_item(body)
        AgentStudioStore.save_draft(self, scope, draft)
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
    # Scoped exactly like every other document (see module docstring);
    # partitioned by ``scope.scope_key``, a true point read/write.

    @staticmethod
    def _gate_report_id(report_id: str) -> str:
        return f"gate_report::{report_id}"

    def save_gate_report(self, scope: ScopeContext, report: ReleaseGateReport) -> ReleaseGateReport:
        super().save_gate_report(scope, report)
        self._upsert(
            scope.scope_key, self._gate_report_id(report.id), "gate_report", report.model_dump(mode="json")
        )
        return report

    def get_gate_report(self, scope: ScopeContext, report_id: str) -> ReleaseGateReport | None:
        cached = super().get_gate_report(scope, report_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._gate_report_id(report_id))
        if document is None:
            return None
        report = ReleaseGateReport.model_validate(document["payload"])
        if report.tenant_id != scope.tenant_id or report.project_id != scope.project_id:
            return None
        AgentStudioStore.save_gate_report(self, scope, report)
        return report

    # -- Releases (append-only lifecycle for an immutable version) ---------

    @staticmethod
    def _release_id(release_id: str) -> str:
        return f"release::{release_id}"

    def _query_releases_for_version(self, scope_key: str, version_id: str) -> list[dict[str, Any]]:
        """Server-side filtered single-partition query for one version's releases.

        Filters by ``payload.version_id`` directly in the Cosmos query
        (``partition_key=`` still pins it to exactly one logical partition)
        so a lookup for one version's release history never loads every
        release ever created across the whole scope and filters
        client-side (finding #9) -- a logical agent with a long release
        history must not make every ``get_release``/``latest_release_for_version``
        call proportional to that history's total size.
        """
        return list(
            self._container.query_items(
                query=("SELECT * FROM c WHERE c.documentType = @documentType AND c.payload.version_id = @versionId"),
                parameters=[
                    {"name": "@documentType", "value": "release"},
                    {"name": "@versionId", "value": version_id},
                ],
                partition_key=scope_key,
            )
        )

    def create_release(self, scope: ScopeContext, release: AgentRelease) -> AgentRelease:
        """Append a new lifecycle transition, guarded by a Cosmos-native
        create-if-absent successor document -- never a client-side
        read-then-write race.

        A dedicated ``release_successor::{version_id}::{predecessor}``
        document (predecessor is ``previous_release_id``, or the literal
        string ``root`` for a version's first release) is written via
        ``create_item`` *before* the release document itself. Cosmos
        rejects a duplicate ``create_item`` with a 409 Conflict, so two
        concurrent callers racing to promote/activate/re-gate the exact same
        predecessor release -- even across separate processes/instances --
        can never both "win": the loser's guard write fails atomically
        server-side and it raises :class:`ReleaseSuccessorConflictError`
        naming the winning release, instead of silently creating a second
        sibling successor (the promotion double-release race). The base
        class's own local in-memory duplicate check still runs afterwards
        (via ``super().create_release(...)``) as defense in depth.
        """
        self._require_scope_match(scope, release.tenant_id, release.project_id)
        successor_id = self._release_successor_id(release.version_id, release.previous_release_id)
        try:
            self._container.create_item(
                {
                    "id": successor_id,
                    "documentType": "release_successor",
                    "scope_key": scope.scope_key,
                    "payload": {"release_id": release.id},
                }
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            document = self._read(scope.scope_key, successor_id)
            winning_release_id = document["payload"]["release_id"] if document is not None else "<unknown>"
            raise ReleaseSuccessorConflictError(
                f"Release '{release.id}' lost a concurrent lifecycle-transition race for version "
                f"'{release.version_id}': release '{winning_release_id}' already succeeded predecessor "
                f"'{release.previous_release_id or 'none'}'."
            ) from exc
        super().create_release(scope, release)
        self._upsert(scope.scope_key, self._release_id(release.id), "release", release.model_dump(mode="json"))
        return release

    @staticmethod
    def _release_successor_id(version_id: str, previous_release_id: str | None) -> str:
        predecessor = previous_release_id or "root"
        return f"release_successor::{version_id}::{predecessor}"

    def _sync_releases(self, scope: ScopeContext, version_id: str) -> None:
        documents = self._query_releases_for_version(scope.scope_key, version_id)
        for document in documents:
            release = AgentRelease.model_validate(document["payload"])
            if release.id not in self._releases:
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

    @staticmethod
    def _approval_dedup_id(dedup_key: str) -> str:
        return f"approval_dedup::{dedup_key}"

    def _sync_approvals(self, scope: ScopeContext) -> None:
        documents = self._query_partition(scope.scope_key, "approval")
        for document in documents:
            approval = StudioApprovalRecord.model_validate(document["payload"])
            self._approvals[approval.id] = approval

    def create_approval(self, scope: ScopeContext, record: StudioApprovalRecord) -> StudioApprovalRecord:
        """Create a pending approval, deduplicating by idempotency key using a
        Cosmos-native create-if-absent guard document -- never a client-side
        scan-then-write.

        A dedicated ``approval_dedup::{hash}`` document (id derived from
        ``AgentStudioStore._approval_dedup_key``, keyed on scope + kind +
        idempotency key) is written via ``create_item`` and carries the
        *full* winning approval payload. Cosmos itself rejects a duplicate
        ``create_item`` with a 409 Conflict, so two concurrent callers --
        even across separate processes/instances -- can never both "win":
        the loser's ``create_item`` fails atomically server-side and it
        decodes the exact payload the winner just committed directly from
        the conflicting document, with no read-after-write race window
        (unlike a query-then-write scan, which both callers could pass
        before either had written anything). ``save_approval_decision``
        deletes the guard document once the approval is decided, so a fresh
        request for the same idempotency key after a prior decision is free
        to create a new pending approval rather than being blocked forever.
        """
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        dedup_key = self._approval_dedup_key(scope, record.kind, record.idempotency_key)
        dedup_id = self._approval_dedup_id(dedup_key)
        payload = record.model_dump(mode="json")
        try:
            self._container.create_item(
                {
                    "id": dedup_id,
                    "documentType": "approval_dedup",
                    "scope_key": scope.scope_key,
                    "payload": payload,
                }
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            document = self._read(scope.scope_key, dedup_id)
            if document is None:
                raise AgentStudioStoreError(
                    f"Approval dedup guard for idempotency key '{record.idempotency_key}' conflicted "
                    "but the winning record could not be read back."
                ) from exc
            winner = StudioApprovalRecord.model_validate(document["payload"])
            self._approvals[winner.id] = winner
            return winner
        AgentStudioStore.create_approval(self, scope, record)
        self._upsert(scope.scope_key, self._approval_id(record.id), "approval", payload)
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
        dedup_key = self._approval_dedup_key(scope, record.kind, record.idempotency_key)
        with contextlib.suppress(CosmosResourceNotFoundError):
            self._container.delete_item(item=self._approval_dedup_id(dedup_key), partition_key=scope.scope_key)
        self._approval_dedup.pop(dedup_key, None)
        return record

    # -- Approval revocations ---------------------------------------------

    @staticmethod
    def _revocation_id(revocation_id: str) -> str:
        return f"approval_revocation::{revocation_id}"

    @staticmethod
    def _revocation_dedup_id(dedup_key: str) -> str:
        return f"approval_revocation_dedup::{dedup_key}"

    def _query_revocations_for_approval(self, scope_key: str, approval_id: str) -> list[dict[str, Any]]:
        """Server-side filtered single-partition query for one approval's
        revocation history -- never a client-side scan across every
        revocation ever appended in the scope (mirrors
        ``_query_releases_for_version``)."""
        return list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.documentType = @documentType AND c.payload.approval_id = @approvalId"
                ),
                parameters=[
                    {"name": "@documentType", "value": "approval_revocation"},
                    {"name": "@approvalId", "value": approval_id},
                ],
                partition_key=scope_key,
            )
        )

    def _sync_revocations(self, scope: ScopeContext, approval_id: str) -> None:
        documents = self._query_revocations_for_approval(scope.scope_key, approval_id)
        for document in documents:
            revocation = ApprovalRevocation.model_validate(document["payload"])
            if revocation.id not in self._revocations:
                AgentStudioStore.create_revocation(self, scope, revocation)

    def create_revocation(self, scope: ScopeContext, revocation: ApprovalRevocation) -> ApprovalRevocation:
        """Append-only revocation write via a Cosmos-native create-if-absent
        guard document -- identical shape to ``create_approval``'s guard,
        except the guard is never deleted: a revocation is a permanent fact,
        so a retried request for the same ``(approval, actor, reason)`` must
        forever resolve to the original revocation rather than ever being
        eligible to create a fresh one.
        """
        self._require_scope_match(scope, revocation.tenant_id, revocation.project_id)
        dedup_key = self._revocation_dedup_key(scope, revocation.approval_id, revocation.idempotency_key)
        dedup_id = self._revocation_dedup_id(dedup_key)
        payload = revocation.model_dump(mode="json")
        try:
            self._container.create_item(
                {
                    "id": dedup_id,
                    "documentType": "approval_revocation_dedup",
                    "scope_key": scope.scope_key,
                    "payload": payload,
                }
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            document = self._read(scope.scope_key, dedup_id)
            if document is None:
                raise AgentStudioStoreError(
                    f"Approval revocation dedup guard for idempotency key "
                    f"'{revocation.idempotency_key}' conflicted but the winning record could not be "
                    "read back."
                ) from exc
            winner = ApprovalRevocation.model_validate(document["payload"])
            self._revocations[winner.id] = winner
            self._revocations_by_approval.setdefault((scope.scope_key, winner.approval_id), [])
            if winner.id not in self._revocations_by_approval[(scope.scope_key, winner.approval_id)]:
                self._revocations_by_approval[(scope.scope_key, winner.approval_id)].append(winner.id)
            return winner
        AgentStudioStore.create_revocation(self, scope, revocation)
        self._upsert(scope.scope_key, self._revocation_id(revocation.id), "approval_revocation", payload)
        return revocation

    def list_revocations(self, scope: ScopeContext, approval_id: str) -> tuple[ApprovalRevocation, ...]:
        self._sync_revocations(scope, approval_id)
        return super().list_revocations(scope, approval_id)

    # -- Approval consumption ----------------------------------------------

    @staticmethod
    def _consumption_id(approval_id: str) -> str:
        """Deterministic Cosmos document id for an approval's single-use
        consumption guard -- keyed by ``approval_id`` alone (not a random
        record id), so ``create_item`` itself is the atomic create-if-absent
        primitive: a second concurrent writer for the same approval always
        gets Cosmos's own 409 conflict rather than a client-side race."""
        return f"approval_consumption::{approval_id}"

    def create_approval_consumption(
        self, scope: ScopeContext, record: ApprovalConsumptionRecord
    ) -> ApprovalConsumptionRecord:
        """Append-only, single-use consumption write via a Cosmos-native
        create-if-absent guard document, identical in spirit to
        ``create_revocation``/``create_approval`` -- except the guard is
        keyed by ``approval_id`` alone (never released, never superseded):
        whichever consumption attempt's ``create_item`` call wins durably
        owns this approval's single use forever.
        """
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        document_id = self._consumption_id(record.approval_id)
        payload = record.model_dump(mode="json")
        try:
            self._container.create_item(
                {
                    "id": document_id,
                    "documentType": "approval_consumption",
                    "scope_key": scope.scope_key,
                    "payload": payload,
                }
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            document = self._read(scope.scope_key, document_id)
            if document is None:
                raise AgentStudioStoreError(
                    f"Approval consumption guard for approval '{record.approval_id}' conflicted but the "
                    "winning record could not be read back."
                ) from exc
            winner = ApprovalConsumptionRecord.model_validate(document["payload"])
            dedup_key = self._consumption_dedup_key(scope, winner.approval_id)
            self._approval_consumptions[winner.id] = winner
            self._approval_consumption_dedup[dedup_key] = winner
            return winner
        AgentStudioStore.create_approval_consumption(self, scope, record)
        return record

    def get_approval_consumption(self, scope: ScopeContext, approval_id: str) -> ApprovalConsumptionRecord | None:
        cached = super().get_approval_consumption(scope, approval_id)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._consumption_id(approval_id))
        if document is None:
            return None
        record = ApprovalConsumptionRecord.model_validate(document["payload"])
        dedup_key = self._consumption_dedup_key(scope, record.approval_id)
        self._approval_consumptions[record.id] = record
        self._approval_consumption_dedup[dedup_key] = record
        return record

    # -- Durable idempotency (cross-instance-safe claim/lease/complete) ----

    @staticmethod
    def _idempotency_id(key: IdempotencyKey) -> str:
        """Deterministic Cosmos document id for an idempotency claim --
        keyed by the key's own canonical digest (never a random record id),
        so ``create_item`` itself is the atomic create-if-absent primitive
        for ``claim_idempotency``, identical in spirit to
        ``_consumption_id``."""
        return key.digest

    @staticmethod
    def _idempotency_result_id(result_hash: str) -> str:
        return f"idempotency-result::{result_hash}"

    def claim_idempotency(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        actor_id: str,
        release_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> IdempotencyClaim:
        """Atomic, cross-instance-safe claim via a Cosmos-native
        create-if-absent guard document -- identical in spirit to
        ``create_approval_consumption``, except keyed by ``key.digest``
        and returning a non-``ACQUIRED`` disposition (never raising) when
        another claim already durably exists for this key."""
        validate_idempotency_lease_seconds(lease_seconds)
        self._require_idempotency_key_scope(scope, key)
        current_time = now if now is not None else utc_now()
        document_id = self._idempotency_id(key)
        claim_token = secrets.token_hex(32)
        record = IdempotencyRecord(
            key=key,
            state=IdempotencyState.CLAIMED,
            version="1",
            claim_token_hash=hash_idempotency_token(claim_token),
            lease_expires_at=current_time + timedelta(seconds=lease_seconds),
            actor_id=actor_id,
            release_id=release_id,
            claimed_at=current_time,
        )
        try:
            self._container.create_item(
                {
                    "id": document_id,
                    "documentType": "idempotency_record",
                    "scope_key": scope.scope_key,
                    "payload": record.model_dump(mode="json"),
                }
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            document = self._read(scope.scope_key, document_id)
            if document is None:
                raise AgentStudioStoreError(
                    f"Idempotency claim for key digest '{key.digest}' conflicted but the current record "
                    "could not be read back."
                ) from exc
            existing = IdempotencyRecord.model_validate(document["payload"])
            self._idempotency_records[(scope.scope_key, key.digest)] = existing
            if existing.state is IdempotencyState.COMPLETED:
                return IdempotencyClaim(disposition=IdempotencyClaimDisposition.COMPLETED, record=existing)
            if existing.state is IdempotencyState.FAILED or is_idempotency_lease_expired(
                existing, now=current_time
            ):
                return IdempotencyClaim(
                    disposition=IdempotencyClaimDisposition.RECONCILIATION_REQUIRED, record=existing
                )
            return IdempotencyClaim(disposition=IdempotencyClaimDisposition.IN_PROGRESS, record=existing)
        self._idempotency_records[(scope.scope_key, key.digest)] = record
        return IdempotencyClaim(
            disposition=IdempotencyClaimDisposition.ACQUIRED, record=record, claim_token=claim_token
        )

    def _transition_idempotency_record(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        build_updates: Callable[[IdempotencyRecord], dict[str, Any]],
    ) -> IdempotencyRecord:
        """Read-then-ETag-guarded-replace transition, mirroring
        ``update_deployment``'s idiom, plus an application-level
        ``version``/``claim_token`` ownership check (mirroring
        ``save_draft``'s ``expected_etag`` check) performed against the
        document *just read* -- never the in-memory cache -- so a stale
        in-process cache can never mask a concurrent transition that a
        different process instance already durably applied."""
        document_id = self._idempotency_id(key)
        document = self._read(scope.scope_key, document_id)
        if document is None:
            raise IdempotencyNotFoundError(f"Idempotency key digest '{key.digest}' has not been claimed.")
        current = IdempotencyRecord.model_validate(document["payload"])
        if current.version != expected_version or current.claim_token_hash != hash_idempotency_token(claim_token):
            raise IdempotencyConcurrencyError(
                f"Idempotency key digest '{key.digest}' was modified concurrently, or claim_token/"
                "expected_version no longer matches the current claim. Re-fetch and retry."
            )
        updated = current.model_copy(update={**build_updates(current), "version": str(int(current.version) + 1)})
        body = {
            "id": document_id,
            "documentType": "idempotency_record",
            "scope_key": scope.scope_key,
            "payload": updated.model_dump(mode="json"),
        }
        try:
            self._container.replace_item(
                item=document_id,
                body=body,
                etag=document.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412:
                raise
            raise IdempotencyConcurrencyError(
                f"Idempotency key digest '{key.digest}' was modified concurrently; re-fetch and retry."
            ) from exc
        self._idempotency_records[(scope.scope_key, key.digest)] = updated
        return updated

    def mark_idempotency_in_progress(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        irreversible: bool,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        self._require_idempotency_key_scope(scope, key)
        current_time = now if now is not None else utc_now()
        return self._transition_idempotency_record(
            scope,
            key,
            claim_token=claim_token,
            expected_version=expected_version,
            build_updates=lambda current: {
                "state": IdempotencyState.IN_PROGRESS,
                "started_at": current.started_at or current_time,
                "irreversible_started": current.irreversible_started or irreversible,
            },
        )

    def complete_idempotency(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        result: dict[str, Any],
        result_hash: str,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        """Durably record completion via a single atomic Cosmos transactional
        batch covering both the record replace and the result document
        write.

        Unlike a read-then-``replace_item``-then-separate-``upsert_item``
        sequence (which leaves a window where a crash between the two calls
        durably strands a ``COMPLETED`` record whose ``result_ref`` points
        at nothing), ``execute_item_batch`` applies every operation in the
        batch as a single all-or-nothing unit scoped to one partition key
        (both documents already share ``scope.scope_key``): either both the
        updated record *and* the result document are durably written, or
        neither is -- "COMPLETED without a readable result" is structurally
        impossible, not just conventionally avoided.
        """
        self._require_idempotency_key_scope(scope, key)
        current_time = now if now is not None else utc_now()
        result_ref = self._idempotency_result_id(result_hash)
        document_id = self._idempotency_id(key)
        document = self._read(scope.scope_key, document_id)
        if document is None:
            raise IdempotencyNotFoundError(f"Idempotency key digest '{key.digest}' has not been claimed.")
        current = IdempotencyRecord.model_validate(document["payload"])
        if current.version != expected_version or current.claim_token_hash != hash_idempotency_token(claim_token):
            raise IdempotencyConcurrencyError(
                f"Idempotency key digest '{key.digest}' was modified concurrently, or claim_token/"
                "expected_version no longer matches the current claim. Re-fetch and retry."
            )
        updated = current.model_copy(
            update={
                "state": IdempotencyState.COMPLETED,
                "completed_at": current_time,
                "result_hash": result_hash,
                "result_ref": result_ref,
                "version": str(int(current.version) + 1),
            }
        )
        record_body = {
            "id": document_id,
            "documentType": "idempotency_record",
            "scope_key": scope.scope_key,
            "payload": updated.model_dump(mode="json"),
        }
        result_body = {
            "id": result_ref,
            "documentType": "idempotency_result",
            "scope_key": scope.scope_key,
            "payload": result,
        }
        batch_operations: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
            ("replace", (document_id, record_body), {"if_match_etag": document.get("_etag")}),
            ("upsert", (result_body,), {}),
        ]
        try:
            self._container.execute_item_batch(batch_operations, partition_key=scope.scope_key)
        except CosmosBatchOperationError as exc:
            if exc.status_code != 412:
                raise
            raise IdempotencyConcurrencyError(
                f"Idempotency key digest '{key.digest}' was modified concurrently; re-fetch and retry."
            ) from exc
        self._idempotency_records[(scope.scope_key, key.digest)] = updated
        self._idempotency_results[(scope.scope_key, result_ref)] = result
        return updated

    def fail_idempotency(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        self._require_idempotency_key_scope(scope, key)
        return self._transition_idempotency_record(
            scope,
            key,
            claim_token=claim_token,
            expected_version=expected_version,
            build_updates=lambda current: {
                "state": IdempotencyState.FAILED,
                "failure_code": failure_code,
                "reconciliation_required": True,
            },
        )

    def load_idempotency_result(self, scope: ScopeContext, result_ref: str) -> dict[str, Any] | None:
        cached = super().load_idempotency_result(scope, result_ref)
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, result_ref)
        if document is None:
            return None
        result: dict[str, Any] = document["payload"]
        self._idempotency_results[(scope.scope_key, result_ref)] = result
        return result

    def get_idempotency_record(self, scope: ScopeContext, key: IdempotencyKey) -> IdempotencyRecord | None:
        self._require_idempotency_key_scope(scope, key)
        cached = self._idempotency_records.get((scope.scope_key, key.digest))
        if cached is not None:
            return cached
        document = self._read(scope.scope_key, self._idempotency_id(key))
        if document is None:
            return None
        record = IdempotencyRecord.model_validate(document["payload"])
        self._idempotency_records[(scope.scope_key, key.digest)] = record
        return record

    # -- Deployments ------------------------------------------------------

    @staticmethod
    def _deployment_id(deployment_id: str) -> str:
        return f"deployment::{deployment_id}"

    def create_deployment(self, scope: ScopeContext, record: DeploymentRecord) -> DeploymentRecord:
        super().create_deployment(scope, record)
        self._upsert(scope.scope_key, self._deployment_id(record.id), "deployment", record.model_dump(mode="json"))
        return record

    def _cache_deployment(self, scope: ScopeContext, deployment: DeploymentRecord) -> None:
        """Refresh the local process cache from a freshly-read Cosmos document.

        Unlike the initial-population helpers used for write-once record
        types, this always overwrites ``self._deployments[deployment.id]``
        (never just "add if missing") so a status/health change written by
        another process/replica is reflected here as soon as it is read,
        instead of being masked by a stale value cached from an earlier read.
        """
        self._deployments[deployment.id] = deployment
        ids = self._deployments_by_agent.setdefault((scope.scope_key, deployment.logical_agent_id), [])
        if deployment.id not in ids:
            ids.append(deployment.id)

    def list_deployments(self, scope: ScopeContext, logical_agent_id: str) -> tuple[DeploymentRecord, ...]:
        documents = self._query_partition(scope.scope_key, "deployment")
        for document in documents:
            deployment = DeploymentRecord.model_validate(document["payload"])
            if deployment.logical_agent_id == logical_agent_id:
                self._cache_deployment(scope, deployment)
        return super().list_deployments(scope, logical_agent_id)

    def get_deployment(self, scope: ScopeContext, deployment_id: str) -> DeploymentRecord | None:
        """Always a fresh Cosmos point read -- never a cache-first return.

        ``DeploymentRecord`` is mutated in place by ``update_deployment``
        (status/health/rollback), and that write may come from a different
        process or replica than the one serving this read. Returning a
        locally cached copy without re-checking Cosmos would let
        activation/health/rollback decisions act on state that is stale
        relative to what is actually persisted -- e.g. satisfying an
        ``ACTIVE`` check against a ``HEALTHY`` reading that another replica
        has since downgraded. The local cache is refreshed as a side effect
        of this read; it is never consulted in place of Cosmos.
        """
        document = self._read(scope.scope_key, self._deployment_id(deployment_id))
        if document is None:
            return None
        deployment = DeploymentRecord.model_validate(document["payload"])
        if deployment.tenant_id != scope.tenant_id or deployment.project_id != scope.project_id:
            return None
        self._cache_deployment(scope, deployment)
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
        """Always a fresh Cosmos point read -- never a cache-first return.

        Binding resolution decides which released version actually serves a
        logical agent in this environment. ``set_binding`` create-or-replaces
        the same document, potentially from a different replica (e.g. a
        rollback re-pointing the binding); a cache-first read here would let
        request routing/resolve keep serving a version that Cosmos no longer
        reflects. The local cache is refreshed as a side effect of this
        read only.
        """
        document = self._read(scope.scope_key, self._binding_id(logical_agent_id, environment))
        if document is None:
            return None
        binding = LogicalAgentBinding.model_validate(document["payload"])
        if binding.tenant_id != scope.tenant_id or binding.project_id != scope.project_id:
            return None
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
            if registration.logical_agent_id == logical_agent_id:
                self._cache_tool_registration(scope, registration)
        return super().list_tool_registrations(scope, logical_agent_id)

    def _cache_tool_registration(self, scope: ScopeContext, registration: ToolRegistrationSpec) -> None:
        """Refresh the local cache for a tool registration from a fresh read.

        Tool registrations have no in-place update path today, but the
        cache is refreshed unconditionally (not just populated on first
        sight) for the same reason as deployments/bindings/proposals: this
        must never rely on "already cached implies still correct".
        """
        self._tool_registrations[registration.id] = registration
        ids = self._tool_registrations_by_agent.setdefault((scope.scope_key, registration.logical_agent_id), [])
        if registration.id not in ids:
            ids.append(registration.id)

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

    def _cache_builder_proposal(self, scope: ScopeContext, proposal: BuilderProposal) -> None:
        """Refresh (never merely populate) the builder-proposal cache.

        ``save_builder_proposal_decision`` mutates a proposal's state in
        place (PENDING -> APPROVED/REJECTED); a stale cached copy must not
        be handed back to a caller inspecting current proposal status.
        """
        self._builder_proposals[proposal.id] = proposal
        ids = self._builder_proposals_by_agent.setdefault((scope.scope_key, proposal.logical_agent_id), [])
        if proposal.id not in ids:
            ids.append(proposal.id)

    def list_builder_proposals(self, scope: ScopeContext, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        documents = self._query_partition(scope.scope_key, "builder_proposal")
        for document in documents:
            proposal = BuilderProposal.model_validate(document["payload"])
            if proposal.logical_agent_id == logical_agent_id:
                self._cache_builder_proposal(scope, proposal)
        return super().list_builder_proposals(scope, logical_agent_id)

    def get_builder_proposal(self, scope: ScopeContext, proposal_id: str) -> BuilderProposal | None:
        """Always a fresh Cosmos point read -- never a cache-first return.

        See ``get_deployment``/``get_binding`` for the rationale: a proposal
        is mutable via ``save_builder_proposal_decision`` and a cache-first
        read could show a caller a PENDING proposal that another replica has
        already decided.
        """
        document = self._read(scope.scope_key, self._builder_proposal_id(proposal_id))
        if document is None:
            return None
        proposal = BuilderProposal.model_validate(document["payload"])
        if proposal.tenant_id != scope.tenant_id or proposal.project_id != scope.project_id:
            return None
        self._cache_builder_proposal(scope, proposal)
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

    # -- Advisory evaluations ------------------------------------------------

    @staticmethod
    def _evaluation_suite_id(suite_id: str) -> str:
        return f"evaluation_suite::{suite_id}"

    @staticmethod
    def _evaluation_run_id(run_id: str) -> str:
        return f"evaluation_run::{run_id}"

    def create_evaluation_suite(self, scope: ScopeContext, suite: EvaluationSuite) -> EvaluationSuite:
        super().create_evaluation_suite(scope, suite)
        self._upsert(
            scope.scope_key,
            self._evaluation_suite_id(suite.id),
            "evaluation_suite",
            suite.model_dump(mode="json"),
        )
        return suite

    def _cache_evaluation_suite(self, scope: ScopeContext, suite: EvaluationSuite) -> None:
        self._evaluation_suites[suite.id] = suite
        ids = self._evaluation_suites_by_agent.setdefault((scope.scope_key, suite.logical_agent_id), [])
        if suite.id not in ids:
            ids.append(suite.id)

    def get_evaluation_suite(self, scope: ScopeContext, suite_id: str) -> EvaluationSuite | None:
        document = self._read(scope.scope_key, self._evaluation_suite_id(suite_id))
        if document is None:
            return None
        suite = EvaluationSuite.model_validate(document["payload"])
        if suite.tenant_id != scope.tenant_id or suite.project_id != scope.project_id:
            return None
        self._cache_evaluation_suite(scope, suite)
        return suite

    def list_evaluation_suites(self, scope: ScopeContext, logical_agent_id: str) -> tuple[EvaluationSuite, ...]:
        documents = self._query_partition(scope.scope_key, "evaluation_suite")
        for document in documents:
            suite = EvaluationSuite.model_validate(document["payload"])
            if suite.logical_agent_id == logical_agent_id:
                self._cache_evaluation_suite(scope, suite)
        return super().list_evaluation_suites(scope, logical_agent_id)

    def create_evaluation_run(self, scope: ScopeContext, run: EvaluationRun) -> EvaluationRun:
        super().create_evaluation_run(scope, run)
        self._upsert(
            scope.scope_key,
            self._evaluation_run_id(run.id),
            "evaluation_run",
            run.model_dump(mode="json"),
        )
        return run

    def _cache_evaluation_run(self, scope: ScopeContext, run: EvaluationRun) -> None:
        self._evaluation_runs[run.id] = run
        agent_ids = self._evaluation_runs_by_agent.setdefault((scope.scope_key, run.logical_agent_id), [])
        if run.id not in agent_ids:
            agent_ids.append(run.id)
        suite_ids = self._evaluation_runs_by_suite.setdefault((scope.scope_key, run.suite_id), [])
        if run.id not in suite_ids:
            suite_ids.append(run.id)

    def get_evaluation_run(self, scope: ScopeContext, run_id: str) -> EvaluationRun | None:
        document = self._read(scope.scope_key, self._evaluation_run_id(run_id))
        if document is None:
            return None
        run = EvaluationRun.model_validate(document["payload"])
        if run.tenant_id != scope.tenant_id or run.project_id != scope.project_id:
            return None
        self._cache_evaluation_run(scope, run)
        return run

    def list_evaluation_runs(
        self, scope: ScopeContext, logical_agent_id: str, *, suite_id: str | None = None
    ) -> tuple[EvaluationRun, ...]:
        documents = self._query_partition(scope.scope_key, "evaluation_run")
        for document in documents:
            run = EvaluationRun.model_validate(document["payload"])
            if run.logical_agent_id == logical_agent_id:
                self._cache_evaluation_run(scope, run)
        return super().list_evaluation_runs(scope, logical_agent_id, suite_id=suite_id)

    # -- Test/playground runs --------------------------------------------

    @staticmethod
    def _test_run_id(run_id: str) -> str:
        return f"test_run::{run_id}"

    def create_test_run(self, scope: ScopeContext, run: PlaygroundTestRun) -> PlaygroundTestRun:
        super().create_test_run(scope, run)
        self._upsert(
            scope.scope_key,
            self._test_run_id(run.id),
            "test_run",
            run.model_dump(mode="json"),
        )
        return run

    def _cache_test_run(self, scope: ScopeContext, run: PlaygroundTestRun) -> None:
        self._test_runs[run.id] = run
        agent_ids = self._test_runs_by_agent.setdefault((scope.scope_key, run.logical_agent_id), [])
        if run.id not in agent_ids:
            agent_ids.append(run.id)
        version_ids = self._test_runs_by_version.setdefault((scope.scope_key, run.version_id), [])
        if run.id not in version_ids:
            version_ids.append(run.id)

    def get_test_run(self, scope: ScopeContext, run_id: str) -> PlaygroundTestRun | None:
        document = self._read(scope.scope_key, self._test_run_id(run_id))
        if document is None:
            return None
        run = PlaygroundTestRun.model_validate(document["payload"])
        if run.tenant_id != scope.tenant_id or run.project_id != scope.project_id:
            return None
        self._cache_test_run(scope, run)
        return run

    def list_test_runs(
        self, scope: ScopeContext, logical_agent_id: str, *, version_id: str | None = None
    ) -> tuple[PlaygroundTestRun, ...]:
        documents = self._query_partition(scope.scope_key, "test_run")
        for document in documents:
            run = PlaygroundTestRun.model_validate(document["payload"])
            if run.logical_agent_id == logical_agent_id:
                self._cache_test_run(scope, run)
        return super().list_test_runs(scope, logical_agent_id, version_id=version_id)


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
        azure_credential(settings.managed_identity_client_id),
        settings.agent_studio_metadata_container,
    )


__all__ = [
    "CosmosAgentStudioStore",
    "build_agent_studio_store",
]
