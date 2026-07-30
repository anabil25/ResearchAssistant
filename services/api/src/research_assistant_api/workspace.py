from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from research_assistant_core.connector_catalog import ConnectorDefinition, connector_definitions
from research_assistant_core.models import Capability, RunStatus

from research_assistant_api.identity import LOCAL_DEVELOPMENT_SOURCE, IdentityContext


def utc_now() -> datetime:
    return datetime.now(UTC)


def _complete_stages(run: RunSummary) -> None:
    completed_at = utc_now()
    for stage in run.stages:
        stage.status = "completed"
        stage.started_at = stage.started_at or run.started_at
        stage.completed_at = stage.completed_at or completed_at


def _fail_active_stage(run: RunSummary) -> None:
    active = next(
        (
            stage
            for stage in run.stages
            if stage.status in {"running", "waiting_for_approval", "planned"}
        ),
        None,
    )
    if active is not None:
        active.status = "failed"
        active.started_at = active.started_at or run.started_at
        active.completed_at = utc_now()


class LibraryStatus(StrEnum):
    READY = "ready"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class LibraryItem(BaseModel):
    id: str
    title: str
    kind: str
    source: str
    status: LibraryStatus
    access: str
    version: str
    checksum: str
    license: str
    added_at: datetime
    evidence_count: int
    connector: str
    provider: str
    publication_year: int | None = None
    description: str
    tags: list[str] = Field(default_factory=list)
    blob_uri: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None


class LibraryIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=3, max_length=240)
    kind: str = Field(min_length=2, max_length=80)
    source: str = Field(min_length=2, max_length=120)
    publication_year: int | None = Field(default=None, ge=1000, le=2100)
    access: Literal["public", "internal", "restricted"] = "internal"
    license: str = Field(min_length=2, max_length=120)
    description: str = Field(min_length=3, max_length=1000)


class LibraryIngestRecord(LibraryIngestRequest):
    source_id: str = Field(pattern=r"^source-[a-f0-9]{12}$")
    blob_uri: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=1, le=20_000_000)
    checksum: str | None = None


class RunStage(BaseModel):
    id: str
    label: str
    status: str
    owner: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


class RunSummary(BaseModel):
    id: str
    durable_instance_id: str
    project_id: str
    capability: Capability
    title: str
    status: RunStatus
    progress: int
    current_stage: str
    owner: str
    started_at: datetime
    completed_at: datetime | None = None
    artifact_count: int = 0
    approval_id: str | None = None
    estimated_cost_usd: float = 0
    scheduler_managed: bool = False
    scheduling_state: str = "not_managed"
    orchestration_input: dict[str, Any] | None = None
    stages: list[RunStage] = Field(default_factory=list)


class LibraryIngestResponse(BaseModel):
    item: LibraryItem
    run: RunSummary


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalRecord(BaseModel):
    id: str
    run_id: str
    title: str
    state: ApprovalState
    risk: str
    gated_action: str
    destination: str
    requested_by: str
    requested_at: datetime
    evidence_summary: str
    idempotency_key: str
    approver_id: str | None = None
    approver_name: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    event_delivery: str = "not_requested"
    decision_event_id: str | None = None


class ApprovalDecision(BaseModel):
    decision: ApprovalState
    rationale: str = Field(min_length=3, max_length=1000)

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: ApprovalState) -> ApprovalState:
        if value not in {ApprovalState.APPROVED, ApprovalState.REJECTED}:
            raise ValueError("Decision must be approved or rejected.")
        return value


class DatasetApprovalState(StrEnum):
    """Lifecycle of a durable, server-issued Dataset Studio analysis approval.

    ``PENDING``/``APPROVED``/``REJECTED`` mirror a human reviewer decision.
    ``CONSUMED`` is a distinct, terminal state reached only once, at the
    moment an ``APPROVED`` request is actually spent to authorize a single
    dataset-analysis invocation -- this makes a decided approval single-use
    and non-replayable, exactly like ``ApprovalConsumptionPort`` in the
    Agent Studio package it is modeled after.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"


class DatasetApprovalDenialReason(StrEnum):
    """Stable, machine-readable reason a dataset approval could not authorize
    an analysis. Every denial path fails closed and maps to exactly one of
    these; there is no reason value that means "allowed"."""

    NOT_FOUND = "not_found"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    ALREADY_CONSUMED = "already_consumed"
    REJECTED = "rejected"
    PENDING = "pending"
    EXPIRED = "expired"
    CONCURRENT_CONFLICT = "concurrent_conflict"
    ALREADY_DECIDED = "already_decided"
    SEPARATION_OF_DUTIES = "separation_of_duties"
    #: The record carries no requester principal, so neither reviewer/requester
    #: distinctness nor consumer entitlement can be verified. Deliberately
    #: distinct from ``SEPARATION_OF_DUTIES``: "we proved the approver is the
    #: requester" and "we cannot prove anything about the requester" are
    #: different facts and must stay separable in the audit trail.
    UNATTRIBUTABLE_REQUESTER = "unattributable_requester"
    #: The principal spending the approval is not the principal that requested
    #: it -- a decided approval is not a bearer token.
    PRINCIPAL_MISMATCH = "principal_mismatch"
    #: A server invariant was violated at the structural send backstop (no
    #: grant, or a grant that does not match the plan/capability being sent).
    GRANT_INVARIANT = "grant_invariant"
    MISSING_APPROVAL_REFERENCE = "missing_approval_reference"


class DatasetApprovalError(ValueError):
    """Typed dataset-approval domain error carrying a stable
    :class:`DatasetApprovalDenialReason`.

    Subclasses ``ValueError`` so existing ``except ValueError`` call sites and
    tests that assert on the human message keep working, while new callers can
    branch on ``.reason`` for a deterministic status/policy mapping.
    """

    def __init__(self, reason: DatasetApprovalDenialReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


#: Domain tag + version baked into every dataset-plan fingerprint. Bumping the
#: version deliberately invalidates every previously-issued approval, so an
#: algorithm change can never let an approval decided under the old encoding
#: silently authorize a plan hashed under the new one.
_DATASET_FINGERPRINT_DOMAIN = "research_assistant.dataset_plan"
_DATASET_FINGERPRINT_VERSION = 3


def compute_dataset_plan_fingerprint(
    *,
    tenant_id: str,
    project_id: str,
    objective: str,
    filename: str,
    csv_text: str,
) -> str:
    """Deterministically bind a dataset approval to the exact plan it may
    authorize.

    A decided approval can never be replayed against a different tenant,
    project, objective, filename, or CSV content: changing any one of those
    facts changes the fingerprint, so ``consume_dataset_approval_request`` will
    reject the mismatch rather than silently accepting a look-alike request.
    This closes the gap where a client-supplied
    ``analysis_approved``/``compute_adapter_configured`` boolean asserted its
    own authority with no binding to what was actually reviewed.

    ``tenant_id`` is bound explicitly rather than inferred from the ambient
    single-tenant store: tenant isolation is otherwise only an environmental
    property (``_workspace_access`` rejecting a mismatched tenant, and Cosmos
    ``_query`` scoping by ``tenantId``), and an approval receipt must not
    depend on those remaining true to be un-replayable across tenants.

    Each field is hashed independently and combined through canonical,
    key-sorted JSON with an explicit domain/version tag -- not a delimiter
    join. Per-field hashing makes cross-field boundary-shift collisions
    impossible: no attacker-controlled character in ``objective``/``filename``/
    ``csv_text`` can be used to move the boundary between two fields (the old
    ``"\u241f".join(...)`` encoding used a printable separator, so an attacker
    could shift bytes across the ``csv_text`` boundary to collide two distinct
    plans onto one fingerprint).
    """

    def _field(value: str) -> str:
        return sha256(value.encode("utf-8")).hexdigest()

    canonical = json.dumps(
        {
            "domain": _DATASET_FINGERPRINT_DOMAIN,
            "version": _DATASET_FINGERPRINT_VERSION,
            "fields": {
                "tenant_id": _field(tenant_id),
                "project_id": _field(project_id),
                "objective": _field(objective),
                "filename": _field(filename),
                "csv_text": _field(csv_text),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class DatasetApprovalRequest(BaseModel):
    id: str
    project_id: str
    plan_fingerprint: str
    filename: str
    objective: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    state: DatasetApprovalState
    approver_id: str | None = None
    approver_name: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    consumed_at: datetime | None = None
    consumed_invocation_id: str | None = None


class DatasetSendOutcome(StrEnum):
    """Whether the send that a consumption authorized actually delivered.

    Deliberately a SEPARATE state machine from
    ``DatasetApprovalAuditEntry.delivery``, which means "has this audit intent
    been emitted to a downstream consumer yet" (the outbox marker driven by
    ``pending_dataset_approval_audit``/``mark_dataset_approval_audit_delivered``)
    and says nothing about whether CSV reached Foundry. Collapsing the two would
    make an entry whose SEND failed indistinguishable from an entry whose AUDIT
    RECORD has not been emitted yet, so recovery would either re-emit delivered
    entries or skip undelivered ones -- and the collision is especially easy to
    miss because both meanings would be spelled "delivered".

    ``UNKNOWN`` is the fail-closed default and is returned whenever no outcome
    entry exists. Absence must never read as success: a crash between the
    consumption and the outcome write is precisely the case this exists to make
    visible.
    """

    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DatasetApprovalAuditEntry(BaseModel):
    """Durable, append-only audit/outbox intent for a dataset-approval state
    transition (a reviewer decision, a single-use consumption) or for the
    outcome of the send that consumption authorized.

    Written atomically with the state mutation it records (in-memory: under the
    same lock; Cosmos: inside the same document under the same ETag CAS), so
    the audit trail can never be lost by a crash *after* the mutation
    succeeded. ``delivery`` is the outbox marker: a pending entry is a durable
    intent that a downstream emitter can recover and (re)deliver, then mark
    ``delivered`` -- there is no post-mutation "best effort" audit write that
    could silently drop.

    On ``action`` semantics, precisely: ``consumed`` means a send was
    **ATTEMPTED**, never that a send happened. The subsequent ``send_succeeded``
    / ``send_failed`` entry is what distinguishes attempted-and-delivered from
    attempted-and-failed, because ``gateway.invoke`` can raise after the
    single-use approval has already been spent.
    """

    id: str
    request_id: str
    project_id: str
    action: Literal["decided", "consumed", "send_succeeded", "send_failed"]
    actor_principal_id: str
    decision: str | None = None
    invocation_id: str | None = None
    plan_fingerprint: str
    recorded_at: datetime
    #: Monotonic append order within a store, persisted alongside the entry.
    #: ``recorded_at`` alone is not a total order (the Windows clock has ~15.6 ms
    #: granularity, so a decision and its consumption can share a timestamp), and
    #: breaking that tie on ``id`` would be deterministic but CAUSALLY WRONG --
    #: it could present "consumed" before "decided". The sequence preserves
    #: append order explicitly instead of relying on Python's sort being stable,
    #: which is an implementation property nobody wrote down in this code.
    sequence: int = 0
    delivery: Literal["pending", "delivered"] = "pending"


class DatasetApprovalRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=4000)
    csv_text: str = Field(min_length=1, max_length=2_000_000)
    ttl_minutes: int = Field(default=60, ge=1, le=1440)


class DatasetApprovalDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=3, max_length=1000)


class ConnectorSetting(BaseModel):
    id: str
    name: str
    category: str
    description: str
    auth_kind: str
    secret_status: str
    enabled: bool
    test_status: str
    last_tested_at: datetime | None = None
    assigned_agents: list[str]
    terms_url: HttpUrl
    data_boundary: str
    capabilities: list[str]
    credential_kind: Literal["none", "api_key"] = "none"
    credential_required: bool = False
    credential_help_url: str | None = None
    operations: list[str] = Field(default_factory=list)


class ConnectorUpdate(BaseModel):
    enabled: bool
    assigned_agents: list[str]


class ConnectorCredentialUpdate(BaseModel):
    #: ``None`` clears the stored key and returns the connector to anonymous quota.
    api_key: str | None = Field(default=None, min_length=1, max_length=500)


class AgentSetting(BaseModel):
    id: str
    name: str
    model_tier: str
    status: str
    web_access: str
    workflow_steps: list[str]
    deployment: str


class ChatAttachment(BaseModel):
    """A file uploaded into the Foundry hosted-agent session filesystem."""

    path: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=160)
    uploaded_at: datetime


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    agent_name: str | None = None
    attachments: list[ChatAttachment] = Field(default_factory=list)


class ChatThread(BaseModel):
    """Server-owned binding between a browser chat and a Foundry conversation.

    ``conversation_id`` and ``session_id`` are never accepted from a client;
    the browser only ever holds ``id``, and the owning principal is re-checked
    on every turn so one project member cannot resume another's session
    sandbox or read the files uploaded into it.
    """

    id: str
    project_id: str
    tenant_id: str
    capability: Capability
    agent_name: str
    owner_principal_id: str
    conversation_id: str
    session_id: str
    isolation_key: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessage] = Field(default_factory=list)
    attachments: list[ChatAttachment] = Field(default_factory=list)


class ProjectSettings(BaseModel):
    project_id: str
    name: str
    description: str
    default_classification: str
    online_research_default: bool = False
    retention_days: int = Field(ge=30, le=3650)
    citation_coverage_threshold: float = Field(ge=0, le=1)
    require_human_approval: bool
    allowed_export_destinations: list[str]
    model_profile: str
    evaluation_policy: str


DEFAULT_PROJECT_NAME = "Research workspace template"
DEFAULT_PROJECT_DESCRIPTION = (
    "A governed template for evidence review, research workflows, "
    "and institutional guidance."
)


def default_project_settings(
    project_id: str,
    *,
    name: str = DEFAULT_PROJECT_NAME,
    description: str = DEFAULT_PROJECT_DESCRIPTION,
) -> ProjectSettings:
    """Return the governed defaults applied to a newly created workspace."""
    return ProjectSettings(
        project_id=project_id,
        name=name,
        description=description,
        default_classification="internal",
        online_research_default=False,
        retention_days=2555,
        citation_coverage_threshold=1.0,
        require_human_approval=True,
        allowed_export_destinations=["Workspace Library", "SharePoint research site"],
        model_profile="Balanced quality",
        evaluation_policy="Block release on unresolved citations or critical policy findings",
    )


class ProjectLifecycle(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class PersonalProject(BaseModel):
    """Catalog metadata for one user-owned workspace.

    Operational data stays in the existing project-scoped stores. This record
    is only the authorization and lifecycle boundary used to select one.
    """

    project_id: str = Field(pattern=r"^(?:project-[a-f0-9]{32}|demo-project)$")
    owner_user_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1000)
    lifecycle: ProjectLifecycle = ProjectLifecycle.ACTIVE
    created_at: datetime
    updated_at: datetime
    template_project_id: str = Field(min_length=1, max_length=200)


class PersonalProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=3, max_length=1000)


class PersonalProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=3, max_length=120)
    description: str | None = Field(default=None, min_length=3, max_length=1000)
    archive: bool = False

    @model_validator(mode="after")
    def has_change(self) -> PersonalProjectUpdate:
        if self.name is None and self.description is None and not self.archive:
            raise ValueError("Provide a name, description, or archive=true.")
        return self


class WorkspaceSummary(BaseModel):
    project: ProjectSettings
    library_items: int
    active_runs: int
    pending_approvals: int
    connector_ready: int
    connector_total: int
    last_activity_at: datetime
    persistence: str


def _dataset_approval_order(record: DatasetApprovalRequest) -> tuple[datetime, str]:
    """Total order for dataset approvals: timestamp, then id as tiebreaker.

    Ordering must not depend on Python's sort being STABLE. ``requested_at``
    alone is not a total order -- on Windows the system clock has ~15.6 ms
    granularity, so two records created in quick succession can share a
    timestamp -- and stability is an implementation property of the sort, not a
    guarantee written down anywhere in this code. Appending the id makes the
    order rest on IDENTITY, so a caller indexing ``[0]``/``[-1]`` gets a
    deterministic answer regardless of insertion order or sort implementation.
    """
    return (record.requested_at, record.id)


def _dataset_audit_order(entry: DatasetApprovalAuditEntry) -> tuple[datetime, int, str]:
    """Total order for dataset audit entries: timestamp, then the monotonic
    append sequence, then the id.

    The sequence is what preserves CAUSAL order when entries share a timestamp
    -- "decided" must never sort after "consumed" -- and the id makes the order
    total even across records that predate sequencing. Correctness therefore
    rests on identity and recorded order, not on Python's sort being stable."""
    return (entry.recorded_at, entry.sequence, entry.id)


class WorkspaceStore:
    persistence = "in-memory demo"

    def __init__(
        self,
        tenant_id: str = "demo",
        project_id: str = "demo-project",
        *,
        project_name: str = DEFAULT_PROJECT_NAME,
        project_description: str = DEFAULT_PROJECT_DESCRIPTION,
        seed_demo_data: bool = True,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self._lock = RLock()
        self._library = _seed_library() if seed_demo_data else []
        self._runs = _seed_runs(project_id) if seed_demo_data else []
        self._approvals = _seed_approvals() if seed_demo_data else []
        self._dataset_approvals: list[DatasetApprovalRequest] = []
        #: Requester principal id (``IdentityContext.user_id``) per approval id,
        #: kept distinct from the public read model (which exposes only the
        #: ``requested_by`` display name) so a reviewer-vs-requester
        #: separation-of-duties check has an authoritative identifier without
        #: broadening what the API returns to project members.
        self._dataset_requester_principals: dict[str, str] = {}
        self._dataset_audit: list[DatasetApprovalAuditEntry] = []
        #: Monotonic counter backing ``DatasetApprovalAuditEntry.sequence``, so
        #: append order survives ties in the coarse system clock.
        self._dataset_audit_sequence = 0
        self._connectors = _seed_connectors() if seed_demo_data else [
            connector.model_copy(
                update={
                    "enabled": False,
                    "secret_status": "not_configured",
                    "test_status": "not_configured",
                    "assigned_agents": [],
                }
            )
            for connector in _seed_connectors()
        ]
        self._settings = default_project_settings(
            project_id,
            name=project_name,
            description=project_description,
        )
        self._agents = _seed_agents()
        self._chat_threads: dict[str, ChatThread] = {}

    def summary(self) -> WorkspaceSummary:
        with self._lock:
            return WorkspaceSummary(
                project=deepcopy(self._settings),
                library_items=len(self._library),
                active_runs=sum(
                    item.status in {RunStatus.RUNNING, RunStatus.WAITING_FOR_APPROVAL} for item in self._runs
                ),
                pending_approvals=sum(item.state == ApprovalState.PENDING for item in self._approvals),
                connector_ready=sum(
                    item.enabled and item.test_status in {"ready", "ready_with_key"} for item in self._connectors
                ),
                connector_total=len(self._connectors),
                last_activity_at=max((item.started_at for item in self._runs), default=utc_now()),
                persistence=self.persistence,
            )

    def library(self) -> list[LibraryItem]:
        with self._lock:
            return deepcopy(self._library)

    def ingest(
        self,
        payload: LibraryIngestRecord,
        identity: IdentityContext,
    ) -> LibraryIngestResponse:
        item_id = payload.source_id
        item = LibraryItem(
            id=item_id,
            title=payload.title,
            kind=payload.kind,
            source=payload.source,
            status=LibraryStatus.PROCESSING,
            access=payload.access,
            version="1.0" if payload.checksum else "pending",
            checksum=payload.checksum or "pending",
            license=payload.license,
            added_at=utc_now(),
            evidence_count=0,
            connector=payload.source,
            provider=payload.source,
            publication_year=payload.publication_year,
            description=payload.description,
            tags=["new-ingestion"],
            blob_uri=payload.blob_uri,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
        )
        run_id = f"run-ingest-{uuid4().hex[:10]}"
        run = self.add_run(
            run_id=run_id,
            capability=Capability.ORCHESTRATION,
            title=f"Ingest {payload.title}",
            owner=identity.display_name,
            status=RunStatus.RUNNING,
            progress=10,
            current_stage="Extract structure",
            artifact_count=0,
            stages=[
                RunStage(
                    id="receive",
                    label="Receive source",
                    status="completed",
                    owner="ingestion-service",
                ),
                RunStage(
                    id="extract",
                    label="Extract structure",
                    status="running",
                    owner="document-intelligence",
                ),
                RunStage(
                    id="govern",
                    label="Checksum, license, version & ACL",
                    status="planned",
                    owner="ingestion-service",
                ),
                RunStage(
                    id="index",
                    label="Chunk, embed & index",
                    status="planned",
                    owner="search-indexer",
                ),
            ],
        )
        with self._lock:
            self._library.insert(0, item)
        return LibraryIngestResponse(item=deepcopy(item), run=run)

    def complete_ingestion(
        self,
        item_id: str,
        run_id: str,
        *,
        evidence_count: int,
        needs_review: bool,
    ) -> LibraryIngestResponse | None:
        with self._lock:
            item = next((row for row in self._library if row.id == item_id), None)
            run = next((row for row in self._runs if row.id == run_id), None)
            if item is None or run is None:
                return None
            item.status = (
                LibraryStatus.NEEDS_REVIEW if needs_review else LibraryStatus.READY
            )
            item.evidence_count = evidence_count
            item.version = "1.0"
            run.status = RunStatus.COMPLETED
            run.progress = 100
            run.current_stage = "Indexed and ready"
            run.completed_at = utc_now()
            _complete_stages(run)
            return LibraryIngestResponse(item=deepcopy(item), run=deepcopy(run))

    def fail_ingestion(
        self,
        item_id: str,
        run_id: str,
        reason: str,
    ) -> LibraryIngestResponse | None:
        with self._lock:
            item = next((row for row in self._library if row.id == item_id), None)
            run = next((row for row in self._runs if row.id == run_id), None)
            if item is None or run is None:
                return None
            item.status = LibraryStatus.BLOCKED
            item.description = f"{item.description} Ingestion blocked: {reason}"
            run.status = RunStatus.FAILED
            run.progress = 100
            run.current_stage = "Ingestion failed"
            run.completed_at = utc_now()
            _fail_active_stage(run)
            return LibraryIngestResponse(item=deepcopy(item), run=deepcopy(run))

    def runs(self) -> list[RunSummary]:
        with self._lock:
            return deepcopy(sorted(self._runs, key=lambda item: item.started_at, reverse=True))

    def run(self, run_id: str) -> RunSummary | None:
        with self._lock:
            return deepcopy(next((item for item in self._runs if item.id == run_id), None))

    def approvals(self) -> list[ApprovalRecord]:
        with self._lock:
            return deepcopy(sorted(self._approvals, key=lambda item: item.requested_at, reverse=True))

    def approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return deepcopy(
                next(
                    (item for item in self._approvals if item.id == approval_id),
                    None,
                )
            )

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        identity: IdentityContext,
    ) -> ApprovalRecord | None:
        with self._lock:
            approval = next(
                (item for item in self._approvals if item.id == approval_id),
                None,
            )
            if approval is None:
                return None
            if approval.state != ApprovalState.PENDING:
                if approval.state == decision.decision:
                    return deepcopy(approval)
                raise ValueError("This approval has already been decided differently.")
            approval.state = decision.decision
            approval.approver_id = identity.user_id
            approval.approver_name = identity.display_name
            approval.decided_at = utc_now()
            approval.rationale = decision.rationale
            approval.event_delivery = "pending"
            approval.decision_event_id = f"decision::{approval.id}"
            run = next((item for item in self._runs if item.id == approval.run_id), None)
            if run:
                run.status = (
                    RunStatus.COMPLETED
                    if decision.decision == ApprovalState.APPROVED
                    else RunStatus.BLOCKED
                )
                run.progress = 100
                run.current_stage = (
                    "Complete"
                    if decision.decision == ApprovalState.APPROVED
                    else "Approval rejected"
                )
                run.completed_at = utc_now()
                if decision.decision == ApprovalState.APPROVED:
                    _complete_stages(run)
                else:
                    _fail_active_stage(run)
            return deepcopy(approval)

    def mark_approval_delivery(
        self,
        approval_id: str,
        delivery: str,
    ) -> ApprovalRecord | None:
        if delivery not in {"delivered", "failed", "not_required"}:
            raise ValueError("Unsupported approval event delivery state.")
        with self._lock:
            approval = next(
                (item for item in self._approvals if item.id == approval_id),
                None,
            )
            if approval is None:
                return None
            approval.event_delivery = delivery
            return deepcopy(approval)

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
        record = ApprovalRecord(
            id=f"approval-{uuid4().hex[:12]}",
            run_id=run_id,
            title=title,
            state=ApprovalState.PENDING,
            risk=risk,
            gated_action=gated_action,
            destination=destination,
            requested_by=requested_by,
            requested_at=utc_now(),
            evidence_summary=evidence_summary,
            idempotency_key=f"{run_id}-{uuid4().hex[:10]}",
        )
        with self._lock:
            self._approvals.append(record)
            run = next((item for item in self._runs if item.id == run_id), None)
            if run:
                run.approval_id = record.id
            return deepcopy(record)

    def dataset_approval_requests(self) -> list[DatasetApprovalRequest]:
        with self._lock:
            return deepcopy(
                sorted(
                    self._dataset_approvals,
                    key=_dataset_approval_order,
                    reverse=True,
                )
            )

    def dataset_approval_request(self, request_id: str) -> DatasetApprovalRequest | None:
        with self._lock:
            return deepcopy(
                next((item for item in self._dataset_approvals if item.id == request_id), None)
            )

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
        now = utc_now()
        record = DatasetApprovalRequest(
            id=f"dsapproval-{uuid4().hex[:12]}",
            project_id=self.project_id,
            plan_fingerprint=plan_fingerprint,
            filename=filename,
            objective=objective,
            requested_by=requested_by,
            requested_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
            state=DatasetApprovalState.PENDING,
        )
        with self._lock:
            self._dataset_approvals.append(record)
            if requested_by_principal_id is not None:
                self._dataset_requester_principals[record.id] = requested_by_principal_id
            return deepcopy(record)

    def dataset_requester_principal(self, request_id: str) -> str | None:
        """Authoritative requester principal id for a dataset approval, held
        separately from the public read model."""
        with self._lock:
            return self._dataset_requester_principals.get(request_id)

    @staticmethod
    def _dataset_sod_exempt(identity: IdentityContext) -> bool:
        """Deterministic separation-of-duties policy exemption.

        The only identity allowed to both request and decide the same dataset
        approval is the local developer identity, which exists solely when no
        authenticating gateway fronts the API. Separation of duties stays
        strictly enforced for every gateway-authenticated identity.

        Deliberately NOT consulted on the consume path: see
        ``_verify_consuming_principal``, where the same predicate would mean
        "anyone may consume".
        """
        return identity.source == LOCAL_DEVELOPMENT_SOURCE

    def _append_dataset_audit(
        self,
        *,
        request_id: str,
        project_id: str,
        action: Literal["decided", "consumed", "send_succeeded", "send_failed"],
        actor_principal_id: str,
        plan_fingerprint: str,
        decision: str | None = None,
        invocation_id: str | None = None,
    ) -> DatasetApprovalAuditEntry:
        """Append a durable audit/outbox intent. Callers already hold
        ``self._lock`` around the state mutation this records; the reentrant
        lock here keeps the append part of that same atomic section."""
        entry = DatasetApprovalAuditEntry(
            id=f"dsaudit-{uuid4().hex[:12]}",
            request_id=request_id,
            project_id=project_id,
            action=action,
            actor_principal_id=actor_principal_id,
            decision=decision,
            invocation_id=invocation_id,
            plan_fingerprint=plan_fingerprint,
            recorded_at=utc_now(),
            sequence=0,
            delivery="pending",
        )
        with self._lock:
            self._dataset_audit_sequence += 1
            entry.sequence = self._dataset_audit_sequence
            self._dataset_audit.append(entry)
        return entry

    def decide_dataset_approval_request(
        self,
        request_id: str,
        decision: DatasetApprovalDecisionRequest,
        identity: IdentityContext,
    ) -> DatasetApprovalRequest | None:
        with self._lock:
            record = next(
                (item for item in self._dataset_approvals if item.id == request_id),
                None,
            )
            if record is None:
                return None
            if record.state != DatasetApprovalState.PENDING:
                # Idempotent same-decision retry: an APPROVED request re-decided
                # "approved" (or REJECTED re-decided "rejected") returns the
                # existing record rather than raising, matching the Cosmos
                # 412-reconcile path. A conflicting decision (or a re-decide of
                # an already-CONSUMED request) fails closed.
                if (
                    record.state == DatasetApprovalState.APPROVED and decision.decision == "approved"
                ) or (record.state == DatasetApprovalState.REJECTED and decision.decision == "rejected"):
                    return deepcopy(record)
                raise DatasetApprovalError(
                    DatasetApprovalDenialReason.ALREADY_DECIDED,
                    "This dataset approval request has already been decided.",
                )
            if not self._dataset_sod_exempt(identity):
                requester_principal = self._dataset_requester_principals.get(request_id)
                if requester_principal is None:
                    # Fail closed rather than skip the control. Absent requester
                    # means separation of duties CANNOT be verified, so deny --
                    # the same shape as an allowlist status check: deny unless
                    # provably distinct. This is deliberately not solved by a
                    # backfill: a backfill can be incomplete and silently so,
                    # and would re-introduce the bypass for anything it missed.
                    # Every approval persisted before requester binding existed
                    # is therefore undecidable; the requester re-submits, which
                    # binds a principal. Backfill remains optional cleanup, never
                    # the control.
                    raise DatasetApprovalError(
                        DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER,
                        "This dataset approval request has no attributable requester "
                        "principal, so reviewer/requester separation of duties cannot "
                        "be verified; refusing to decide it. Re-submit the request to "
                        "bind a requester principal.",
                    )
                if requester_principal == identity.user_id:
                    raise DatasetApprovalError(
                        DatasetApprovalDenialReason.SEPARATION_OF_DUTIES,
                        "The requester of a dataset approval cannot also approve or reject "
                        "it; a different reviewer must decide (separation of duties).",
                    )
            record.state = (
                DatasetApprovalState.APPROVED
                if decision.decision == "approved"
                else DatasetApprovalState.REJECTED
            )
            record.approver_id = identity.user_id
            record.approver_name = identity.display_name
            record.decided_at = utc_now()
            record.rationale = decision.rationale
            self._append_dataset_audit(
                request_id=record.id,
                project_id=record.project_id,
                action="decided",
                actor_principal_id=identity.user_id,
                plan_fingerprint=record.plan_fingerprint,
                decision=decision.decision,
            )
            return deepcopy(record)

    def _check_dataset_approval_usable(
        self,
        request_id: str,
        *,
        plan_fingerprint: str,
        consumed_by_principal_id: str | None,
    ) -> DatasetApprovalRequest:
        """Every fail-closed condition that must hold for an approval to
        authorize one send. Pure: inspects, never mutates. Callers hold
        ``self._lock``.

        Shared verbatim by :meth:`validate_dataset_approval_request` (an early
        fail-fast) and :meth:`consume_dataset_approval_request` (the authority),
        so the authoritative transition re-checks *everything* rather than
        trusting that an earlier validation already did.
        """
        record = next(
            (item for item in self._dataset_approvals if item.id == request_id),
            None,
        )
        if record is None:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.NOT_FOUND,
                "Dataset approval request was not found.",
            )
        if record.plan_fingerprint != plan_fingerprint:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.FINGERPRINT_MISMATCH,
                "Dataset approval request does not match the exact dataset plan submitted.",
            )
        if record.state == DatasetApprovalState.CONSUMED:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.ALREADY_CONSUMED,
                "Dataset approval request has already been consumed.",
            )
        if record.state == DatasetApprovalState.REJECTED:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.REJECTED,
                "Dataset approval request was rejected.",
            )
        if record.state == DatasetApprovalState.PENDING:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.PENDING,
                "Dataset approval request has not been decided yet.",
            )
        self._verify_consuming_principal(request_id, consumed_by_principal_id)
        if record.expires_at <= utc_now():
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.EXPIRED,
                "Dataset approval request has expired.",
            )
        return record

    def validate_dataset_approval_request(
        self,
        request_id: str,
        *,
        plan_fingerprint: str,
        consumed_by_principal_id: str | None = None,
    ) -> DatasetApprovalRequest:
        """Non-mutating fail-fast: would this approval authorize this exact plan
        for this principal *right now*?

        This exists so a route can reject unauthorized dataset content **before**
        any local processing touches it, without spending the single-use
        approval. It is deliberately NOT the authorization gate: the approval can
        be consumed, revoked, or expire between this call and the real
        transition, so :meth:`consume_dataset_approval_request` re-verifies every
        condition and remains the sole authority.
        """
        with self._lock:
            return deepcopy(
                self._check_dataset_approval_usable(
                    request_id,
                    plan_fingerprint=plan_fingerprint,
                    consumed_by_principal_id=consumed_by_principal_id,
                )
            )

    def consume_dataset_approval_request(
        self,
        request_id: str,
        *,
        plan_fingerprint: str,
        invocation_id: str,
        consumed_by_principal_id: str | None = None,
    ) -> DatasetApprovalRequest:
        """Atomically resolve and single-use-consume a decided dataset
        approval, failing closed with a specific reason for every
        non-usable state.

        This is the sole authority. It re-verifies every condition under the
        lock -- never assuming an earlier
        :meth:`validate_dataset_approval_request` result still holds -- and
        either returns a ``CONSUMED`` record or raises
        :class:`DatasetApprovalError`. Never fabricates a context and never
        allows the same decided approval to authorize a second, different, or
        replayed invocation.

        Consumption is bound to the requesting principal so a decided approval
        is not a bearer credential: knowing the request id and holding the exact
        CSV is not sufficient for a *different* project member to spend it.
        """
        with self._lock:
            record = self._check_dataset_approval_usable(
                request_id,
                plan_fingerprint=plan_fingerprint,
                consumed_by_principal_id=consumed_by_principal_id,
            )
            now = utc_now()
            record.state = DatasetApprovalState.CONSUMED
            record.consumed_at = now
            record.consumed_invocation_id = invocation_id
            self._append_dataset_audit(
                request_id=record.id,
                project_id=record.project_id,
                action="consumed",
                actor_principal_id=consumed_by_principal_id or "unknown",
                plan_fingerprint=record.plan_fingerprint,
                invocation_id=invocation_id,
            )
            return deepcopy(record)

    def _verify_consuming_principal(
        self,
        request_id: str,
        consumed_by_principal_id: str | None,
    ) -> None:
        """A decided approval may only be spent by the principal that requested
        it. Deny when either side is unattributable -- an approval whose
        requester was never recorded cannot have its consumer checked, and
        "unknown" must never be treated as "entitled"."""
        requester_principal = self._dataset_requester_principals.get(request_id)
        if requester_principal is None or consumed_by_principal_id is None:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER,
                "Dataset approval request cannot be attributed to a requesting "
                "principal, so entitlement to spend it cannot be verified; "
                "refusing to consume it.",
            )
        if requester_principal != consumed_by_principal_id:
            raise DatasetApprovalError(
                DatasetApprovalDenialReason.PRINCIPAL_MISMATCH,
                "This dataset approval was requested by a different principal; a "
                "decided approval is not transferable and may only be spent by its "
                "requester.",
            )

    def record_dataset_send_outcome(
        self,
        request_id: str,
        *,
        invocation_id: str,
        plan_fingerprint: str,
        delivered: bool,
        actor_principal_id: str,
    ) -> DatasetApprovalAuditEntry:
        """Append the second audit/outbox entry describing what happened to the
        send that a consumption authorized.

        ``consumed`` alone only ever means a send was ATTEMPTED: the hosted
        gateway can raise after the single-use approval has already been spent.
        This entry is what makes the trail unambiguous, distinguishing
        attempted-and-delivered (``send_succeeded``) from attempted-and-failed
        (``send_failed``). It reuses the same append-only outbox machinery as
        the decision and consumption entries, so it is recoverable through
        ``pending_dataset_approval_audit`` on the same terms.
        """
        return self._append_dataset_audit(
            request_id=request_id,
            project_id=self.project_id,
            action="send_succeeded" if delivered else "send_failed",
            actor_principal_id=actor_principal_id,
            plan_fingerprint=plan_fingerprint,
            invocation_id=invocation_id,
        )

    def dataset_send_outcome(
        self,
        request_id: str,
        *,
        invocation_id: str | None = None,
    ) -> DatasetSendOutcome:
        """Read the send outcome for a consumed approval, FAIL-CLOSED.

        Returns :attr:`DatasetSendOutcome.UNKNOWN` whenever no outcome entry
        exists. The absence of an outcome entry must never be read as success --
        a crash between the consumption and the outcome write is exactly the
        case this distinction exists to surface. Callers that need "did this CSV
        actually reach Foundry" must treat ``UNKNOWN`` as "not proven", never as
        delivered.

        This deliberately does NOT consult ``DatasetApprovalAuditEntry.delivery``,
        which tracks an unrelated concern (audit-intent emission to a downstream
        consumer).
        """
        with self._lock:
            for entry in reversed(sorted(self._dataset_audit, key=_dataset_audit_order)):
                if entry.request_id != request_id:
                    continue
                if invocation_id is not None and entry.invocation_id != invocation_id:
                    continue
                if entry.action == "send_succeeded":
                    return DatasetSendOutcome.DELIVERED
                if entry.action == "send_failed":
                    return DatasetSendOutcome.FAILED
            return DatasetSendOutcome.UNKNOWN

    def dataset_approvals_blocked_by_requester_attribution(self) -> list[DatasetApprovalRequest]:
        """Enumerate the approvals that will ACTUALLY begin denying once
        requester attribution is enforced -- the migration surface.

        Narrowed deliberately to the genuinely affected set: ``APPROVED`` and
        not yet expired. A ``CONSUMED`` record cannot be consumed again
        regardless, and ``REJECTED`` or expired records already deny, so
        including them inflates the number without adding a decision. This is
        the difference between reporting "1,200 legacy approvals" and "7
        approved and unexpired" -- the former causes a panic, the latter
        supports a decision.

        SCOPE WARNING: this instance is bound to ONE (tenant, project) pair --
        ``_query`` pins ``@tenantId``/``@projectId`` -- so this returns the
        count for THIS project only and will UNDER-REPORT the fleet-wide
        population. To size the real migration, query the container
        cross-partition::

            SELECT c.id, c.tenantId, c.projectId, c.payload.expires_at FROM c
            WHERE c.documentType = "dataset_approval"
              AND NOT IS_DEFINED(c.requesterPrincipalId)
              AND c.payload.state = "approved"

        ``NOT IS_DEFINED`` is required, not ``c.requesterPrincipalId = null``:
        legacy documents OMIT the key rather than storing null (see
        ``CosmosWorkspaceStore._dataset_approval_document``, which sets it only
        when a principal is present), so the ``= null`` form matches NOTHING and
        reports a clean zero-affected population. A query that cannot observe
        the condition it is asked about is worse than no query, because it
        produces false reassurance rather than silence.
        """
        now = utc_now()
        with self._lock:
            return deepcopy(
                [
                    record
                    for record in sorted(self._dataset_approvals, key=_dataset_approval_order)
                    if record.id not in self._dataset_requester_principals
                    and record.state == DatasetApprovalState.APPROVED
                    and record.expires_at > now
                ]
            )

    def dataset_approvals_invalidated_by_fingerprint_version(self) -> list[DatasetApprovalRequest]:
        """Enumerate approvals invalidated by the plan-fingerprint domain bump.

        Binding ``tenant_id`` into the fingerprint changed the hash for every
        plan, and the domain version moved with it. Stored fingerprints were
        computed under the OLD version, so they can no longer match a
        recomputed one -- and because a fingerprint is an opaque hash, a v2
        record is indistinguishable from a v3 record by inspection. Every
        approval that has not yet been spent is therefore affected, NOT only the
        legacy ones missing a requester principal.

        Narrowed to those that will actually be harmed: not yet ``CONSUMED``
        (a consumed approval cannot be spent again regardless) and not expired
        (an expired one already denies). These fail with ``FINGERPRINT_MISMATCH``
        -- a THIRD, separate monitoring signal from the two requester-attribution
        denials, and one that will also spike on deploy day.

        SCOPE WARNING: bound to THIS (tenant, project) pair by ``_query``, so it
        under-reports the fleet. Cross-partition equivalent::

            SELECT c.id, c.tenantId, c.projectId, c.payload.state,
                   c.payload.expires_at
            FROM c
            WHERE c.documentType = "dataset_approval"
              AND c.payload.state IN ("pending", "approved")
        """
        now = utc_now()
        with self._lock:
            return deepcopy(
                [
                    record
                    for record in sorted(self._dataset_approvals, key=_dataset_approval_order)
                    if record.state
                    in {DatasetApprovalState.PENDING, DatasetApprovalState.APPROVED}
                    and record.expires_at > now
                ]
            )

    def dataset_approval_audit(self) -> list[DatasetApprovalAuditEntry]:
        """Full append-only dataset-approval audit trail, oldest first."""
        with self._lock:
            return deepcopy(sorted(self._dataset_audit, key=_dataset_audit_order))

    def pending_dataset_approval_audit(self) -> list[DatasetApprovalAuditEntry]:
        """Undelivered audit/outbox intents awaiting downstream delivery.

        A recovery process reads these after a restart and re-emits them; each
        one is durable because it was written atomically with the state
        transition that produced it, so nothing is lost by a crash between the
        mutation and delivery.
        """
        with self._lock:
            return deepcopy([entry for entry in self._dataset_audit if entry.delivery == "pending"])

    def mark_dataset_approval_audit_delivered(self, entry_id: str) -> DatasetApprovalAuditEntry | None:
        with self._lock:
            entry = next((item for item in self._dataset_audit if item.id == entry_id), None)
            if entry is None:
                return None
            entry.delivery = "delivered"
            return deepcopy(entry)

    def connectors(self) -> list[ConnectorSetting]:
        with self._lock:
            return deepcopy(self._connectors)

    def update_connector(
        self,
        connector_id: str,
        update: ConnectorUpdate,
    ) -> ConnectorSetting | None:
        allowed_agents = {
            "literature",
            "grant",
            "matching",
            "dataset",
            "institution",
        }
        if not set(update.assigned_agents).issubset(allowed_agents):
            raise ValueError("Connector assignment contains an unknown specialist.")
        if connector_id in {"pubmed", "grants_gov"} and not update.enabled:
            raise ValueError("Required project connectors cannot be disabled.")
        with self._lock:
            connector = next(
                (item for item in self._connectors if item.id == connector_id),
                None,
            )
            if connector is None:
                return None
            connector.enabled = update.enabled
            connector.assigned_agents = update.assigned_agents
            return deepcopy(connector)

    def record_connector_test(
        self,
        connector_id: str,
        status: str,
    ) -> ConnectorSetting | None:
        with self._lock:
            connector = next(
                (item for item in self._connectors if item.id == connector_id),
                None,
            )
            if connector is None:
                return None
            connector.test_status = status
            connector.last_tested_at = utc_now()
            return deepcopy(connector)

    def agents(self) -> list[AgentSetting]:
        with self._lock:
            return deepcopy(self._agents)

    def chat_thread(self, thread_id: str, *, owner_principal_id: str) -> ChatThread | None:
        """Return a thread only to the principal that opened it."""
        with self._lock:
            record = self._chat_threads.get(thread_id)
            if record is None or record.owner_principal_id != owner_principal_id:
                return None
            return deepcopy(record)

    def save_chat_thread(self, thread: ChatThread) -> ChatThread:
        with self._lock:
            existing = self._chat_threads.get(thread.id)
            if existing is not None and existing.owner_principal_id != thread.owner_principal_id:
                raise ValueError("A chat thread cannot change owner.")
            record = thread.model_copy(deep=True, update={"updated_at": utc_now()})
            self._chat_threads[thread.id] = record
            return deepcopy(record)

    def settings(self) -> ProjectSettings:
        with self._lock:
            return deepcopy(self._settings)

    def update_settings(self, update: ProjectSettings) -> ProjectSettings:
        if update.project_id != self._settings.project_id:
            raise ValueError("The project identifier cannot be changed.")
        if update.online_research_default:
            raise ValueError("Online research must remain opt-in per run.")
        with self._lock:
            self._settings = deepcopy(update)
            return deepcopy(self._settings)

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
        now = utc_now()
        record = RunSummary(
            id=run_id,
            durable_instance_id=f"research-{run_id}",
            project_id=self.project_id,
            capability=capability,
            title=title,
            status=status,
            progress=progress,
            current_stage=current_stage,
            owner=owner,
            started_at=now,
            completed_at=now if status == RunStatus.COMPLETED else None,
            artifact_count=artifact_count,
            stages=stages or [],
        )
        with self._lock:
            existing_index = next(
                (index for index, item in enumerate(self._runs) if item.id == run_id),
                None,
            )
            if existing_index is None:
                self._runs.append(record)
            else:
                self._runs[existing_index] = record
            return deepcopy(record)


def _seed_library() -> list[LibraryItem]:
    now = utc_now()
    rows = [
        (
            "paper-rag",
            "Provenance-first retrieval for research synthesis",
            "Paper",
            "PubMed",
            6,
            "CC BY 4.0",
            "public",
            ["retrieval", "citations"],
        ),
        (
            "paper-human",
            "Human review gates for evidence workflows",
            "Paper",
            "Europe PMC",
            4,
            "CC BY 4.0",
            "public",
            ["human-in-the-loop"],
        ),
        (
            "grant-open",
            "Open Research Infrastructure Opportunity",
            "Funding notice",
            "Grants.gov",
            8,
            "U.S. Government Work",
            "public",
            ["grant", "open-science"],
        ),
        (
            "policy-irb",
            "IRB guidance for AI-assisted research",
            "Policy",
            "Institutional Library",
            5,
            "Institutional",
            "restricted",
            ["IRB", "policy"],
        ),
        (
            "policy-retention",
            "Research records retention standard",
            "Policy",
            "Institutional Library",
            3,
            "Institutional",
            "internal",
            ["records", "retention"],
        ),
        (
            "dataset-outcomes",
            "Pilot outcomes.csv",
            "Dataset",
            "Workspace upload",
            1,
            "Project supplied",
            "restricted",
            ["pilot", "outcomes"],
        ),
        (
            "profile-computational-biology",
            "Computational biology collaboration profile",
            "Research profile",
            "Faculty directory",
            2,
            "Institutional",
            "internal",
            ["genomics", "reproducibility"],
        ),
        (
            "facility-imaging",
            "Advanced Imaging Core",
            "Facility",
            "Core directory",
            2,
            "Institutional",
            "internal",
            ["microscopy", "image-analysis"],
        ),
        (
            "template-dmp",
            "Data-management plan template",
            "Template",
            "Research Office",
            3,
            "Institutional",
            "internal",
            ["DMP", "template"],
        ),
    ]
    return [
        LibraryItem(
            id=row[0],
            title=row[1],
            kind=row[2],
            source=row[3],
            status=LibraryStatus.READY,
            access=row[6],
            version="1.0",
            checksum=f"sha256:{row[0]}-fixture",
            license=row[5],
            added_at=now,
            evidence_count=row[4],
            connector=row[3],
            provider=row[3],
            publication_year=2026,
            description=f"Verified {row[2].lower()} record available to this project.",
            tags=row[7],
        )
        for row in rows
    ]


def _seed_runs(project_id: str) -> list[RunSummary]:
    now = utc_now()
    rows = [
        (
            "run-lit-001",
            Capability.LITERATURE,
            "Reproducible synthesis protocol",
            RunStatus.COMPLETED,
            100,
            "Citation audit complete",
            2,
        ),
        (
            "run-grant-001",
            Capability.GRANT,
            "Open infrastructure application",
            RunStatus.WAITING_FOR_APPROVAL,
            86,
            "Reviewer approval",
            4,
        ),
        (
            "run-match-001",
            Capability.MATCHING,
            "Genomics collaborator shortlist",
            RunStatus.COMPLETED,
            100,
            "Shortlist confirmed",
            1,
        ),
        (
            "run-data-001",
            Capability.DATASET,
            "Pilot outcomes profile",
            RunStatus.BLOCKED,
            15,
            "Compute adapter not configured",
            3,
        ),
        (
            "run-policy-001",
            Capability.INSTITUTIONAL_QA,
            "IRB disclosure guidance",
            RunStatus.COMPLETED,
            100,
            "Answer audited",
            1,
        ),
    ]
    return [
        RunSummary(
            id=row[0],
            durable_instance_id=f"research-{row[0]}",
            project_id=project_id,
            capability=row[1],
            title=row[2],
            status=row[3],
            progress=row[4],
            current_stage=row[5],
            owner="Workspace researcher",
            started_at=now,
            completed_at=now if row[3] == RunStatus.COMPLETED else None,
            artifact_count=row[6],
            approval_id=("approval-grant-export" if row[0] == "run-grant-001" else None),
        )
        for row in rows
    ]


def _seed_approvals() -> list[ApprovalRecord]:
    now = utc_now()
    return [
        ApprovalRecord(
            id="approval-grant-export",
            run_id="run-grant-001",
            title="Release grant package for institutional review",
            state=ApprovalState.PENDING,
            risk="High",
            gated_action="Export package version 0.8 and notify the assigned research-office reviewer.",
            destination="SharePoint research site / Grant reviews",
            requested_by="grant-agent",
            requested_at=now,
            evidence_summary=(
                "7/7 requirements mapped; 2 project fact gaps remain; no unsupported commitments detected."
            ),
            idempotency_key="grant-export-run-grant-001-v08",
        ),
    ]


def _connector(definition: ConnectorDefinition) -> ConnectorSetting:
    return ConnectorSetting(
        id=definition.id,
        name=definition.name,
        category=definition.category,
        description=definition.description,
        auth_kind=definition.auth_kind,
        secret_status=definition.secret_status,
        enabled=True,
        test_status=definition.test_status,
        assigned_agents=list(definition.assigned_agents),
        terms_url=HttpUrl(definition.terms_url),
        data_boundary=definition.data_boundary,
        capabilities=list(definition.capabilities),
        credential_kind=definition.credential.kind,
        credential_required=definition.credential.required,
        credential_help_url=definition.credential.help_url or None,
        operations=[
            operation.mcp_tool_name
            for operation in definition.operations
            if operation.operation_class != "delete"
        ],
    )


def _seed_connectors() -> list[ConnectorSetting]:
    return [_connector(definition) for definition in connector_definitions()]


def _seed_agents() -> list[AgentSetting]:
    return [
        AgentSetting(
            id="coordinator",
            name="Research coordinator",
            model_tier="Fast",
            status="Active",
            web_access="Never direct",
            workflow_steps=["Classify", "Route", "Reconcile"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="literature",
            name="Literature synthesis",
            model_tier="Primary",
            status="Active",
            web_access="Opt-in public only",
            workflow_steps=["Protocol", "Search", "Screen", "Extract", "Synthesize", "Audit"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="grant",
            name="Grant development",
            model_tier="Primary",
            status="Active",
            web_access="Opportunity only",
            workflow_steps=["Requirements", "Facts", "Aims", "Draft", "Compliance", "Red team"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="matching",
            name="PI & resource matching",
            model_tier="Fast",
            status="Active",
            web_access="Public metadata leads",
            workflow_steps=["Criteria", "Filter", "Resolve", "Score", "Compare"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="dataset",
            name="Dataset interpretation",
            model_tier="Fast",
            status="Active",
            web_access="No raw data",
            workflow_steps=["Validate", "Profile", "Plan", "Compute", "Interpret"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="institution",
            name="Institutional guidance",
            model_tier="Fast",
            status="Active",
            web_access="Forbidden",
            workflow_steps=["Scope", "Authorize", "Version", "Conflict", "Answer"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="literature_online",
            name="Literature public researcher",
            model_tier="Fast",
            status="Active",
            web_access="Public-only deployment",
            workflow_steps=["Public query", "Metadata", "Web", "Source handoff"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="grant_online",
            name="Grant opportunity researcher",
            model_tier="Fast",
            status="Active",
            web_access="Public opportunity only",
            workflow_steps=["Public notice", "Funding metadata", "Verify URL"],
            deployment="Foundry Hosted Agent",
        ),
        AgentSetting(
            id="matching_online",
            name="Public entity researcher",
            model_tier="Fast",
            status="Active",
            web_access="Public metadata only",
            workflow_steps=["Public criteria", "Resolve IDs", "Return leads"],
            deployment="Foundry Hosted Agent",
        ),
    ]
