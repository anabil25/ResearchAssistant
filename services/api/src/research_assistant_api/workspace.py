from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from research_assistant_core.models import Capability, RunStatus

from research_assistant_api.identity import DEMO_SANDBOX_SOURCE, IdentityContext


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
_DATASET_FINGERPRINT_VERSION = 2


def compute_dataset_plan_fingerprint(
    *,
    project_id: str,
    objective: str,
    filename: str,
    csv_text: str,
) -> str:
    """Deterministically bind a dataset approval to the exact plan it may
    authorize.

    A decided approval can never be replayed against a different project,
    objective, filename, or CSV content: changing any one of those facts
    changes the fingerprint, so ``consume_dataset_approval_request`` will
    reject the mismatch rather than silently accepting a look-alike request.
    This closes the gap where a client-supplied
    ``analysis_approved``/``compute_adapter_configured`` boolean asserted its
    own authority with no binding to what was actually reviewed.

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


class DatasetApprovalAuditEntry(BaseModel):
    """Durable, append-only audit/outbox intent for a dataset-approval state
    transition (a reviewer decision or a single-use consumption).

    Written atomically with the state mutation it records (in-memory: under the
    same lock; Cosmos: inside the same document under the same ETag CAS), so
    the audit trail can never be lost by a crash *after* the mutation
    succeeded. ``delivery`` is the outbox marker: a pending entry is a durable
    intent that a downstream emitter can recover and (re)deliver, then mark
    ``delivered`` -- there is no post-mutation "best effort" audit write that
    could silently drop.
    """

    id: str
    request_id: str
    project_id: str
    action: Literal["decided", "consumed"]
    actor_principal_id: str
    decision: str | None = None
    invocation_id: str | None = None
    plan_fingerprint: str
    recorded_at: datetime
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


class ConnectorUpdate(BaseModel):
    enabled: bool
    assigned_agents: list[str]


class AgentSetting(BaseModel):
    id: str
    name: str
    model_tier: str
    status: str
    web_access: str
    workflow_steps: list[str]
    deployment: str


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


class WorkspaceSummary(BaseModel):
    project: ProjectSettings
    library_items: int
    active_runs: int
    pending_approvals: int
    connector_ready: int
    connector_total: int
    last_activity_at: datetime
    persistence: str


class WorkspaceStore:
    persistence = "in-memory demo"

    def __init__(
        self,
        tenant_id: str = "demo",
        project_id: str = "demo-project",
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self._lock = RLock()
        self._library = _seed_library()
        self._runs = _seed_runs(project_id)
        self._approvals = _seed_approvals()
        self._dataset_approvals: list[DatasetApprovalRequest] = []
        #: Requester principal id (``IdentityContext.user_id``) per approval id,
        #: kept distinct from the public read model (which exposes only the
        #: ``requested_by`` display name) so a reviewer-vs-requester
        #: separation-of-duties check has an authoritative identifier without
        #: broadening what the API returns to project members.
        self._dataset_requester_principals: dict[str, str] = {}
        self._dataset_audit: list[DatasetApprovalAuditEntry] = []
        self._connectors = _seed_connectors()
        self._settings = ProjectSettings(
            project_id=project_id,
            name="AI for equitable clinical research",
            description=(
                "A governed workspace for evidence review, grant development, "
                "collaborator discovery, dataset analysis, and institutional guidance."
            ),
            default_classification="internal",
            online_research_default=False,
            retention_days=2555,
            citation_coverage_threshold=1.0,
            require_human_approval=True,
            allowed_export_destinations=["Workspace Library", "SharePoint research site"],
            model_profile="Balanced quality",
            evaluation_policy="Block release on unresolved citations or critical policy findings",
        )
        self._agents = _seed_agents()

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
                last_activity_at=max(item.started_at for item in self._runs),
                persistence=self.persistence,
            )

    def library(self) -> list[LibraryItem]:
        with self._lock:
            return deepcopy(self._library)

    def ingest(
        self,
        payload: LibraryIngestRecord,
        identity: IdentityContext,
        *,
        scheduler_managed: bool = False,
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
            scheduler_managed=scheduler_managed,
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
            run.scheduling_state = "failed"
            run.current_stage = "Scheduling failed"
            run.completed_at = utc_now()
            _fail_active_stage(run)
            return LibraryIngestResponse(item=deepcopy(item), run=deepcopy(run))

    def fail_run(self, run_id: str, reason: str) -> RunSummary | None:
        with self._lock:
            run = next((row for row in self._runs if row.id == run_id), None)
            if run is None:
                return None
            run.status = RunStatus.FAILED
            run.progress = 100
            run.scheduling_state = "failed"
            run.current_stage = "Scheduling failed"
            run.completed_at = utc_now()
            _fail_active_stage(run)
            for approval in self._approvals:
                if approval.run_id == run_id and approval.state == ApprovalState.PENDING:
                    approval.state = ApprovalState.CANCELLED
                    approval.rationale = reason
                    approval.decided_at = utc_now()
            return deepcopy(run)

    def set_run_orchestration(
        self,
        run_id: str,
        orchestration_input: dict[str, Any],
    ) -> RunSummary | None:
        with self._lock:
            run = next((row for row in self._runs if row.id == run_id), None)
            if run is None:
                return None
            run.orchestration_input = deepcopy(orchestration_input)
            run.scheduling_state = "pending" if run.scheduler_managed else "not_managed"
            return deepcopy(run)

    def mark_run_scheduling(
        self,
        run_id: str,
        state: str,
    ) -> RunSummary | None:
        if state not in {"scheduled", "uncertain", "failed"}:
            raise ValueError("Unsupported run scheduling state.")
        with self._lock:
            run = next((row for row in self._runs if row.id == run_id), None)
            if run is None:
                return None
            was_reconciliation_placeholder = (
                run.status == RunStatus.PLANNED and run.current_stage == "Scheduling reconciliation required"
            )
            run.scheduling_state = state
            if state == "uncertain":
                run.status = RunStatus.PLANNED
                run.current_stage = "Scheduling reconciliation required"
            elif state == "failed":
                run.status = RunStatus.FAILED
                run.progress = 100
                run.current_stage = "Scheduling failed"
                run.completed_at = utc_now()
                _fail_active_stage(run)
            elif was_reconciliation_placeholder and run.orchestration_input:
                original_status = run.orchestration_input.get("ui_status")
                if isinstance(original_status, str):
                    run.status = RunStatus(original_status)
                original_stage = run.orchestration_input.get("ui_current_stage")
                if isinstance(original_stage, str):
                    run.current_stage = original_stage
                original_progress = run.orchestration_input.get("ui_progress")
                if isinstance(original_progress, int):
                    run.progress = original_progress
            return deepcopy(run)

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
                if run.scheduler_managed:
                    run.status = (
                        RunStatus.RUNNING
                        if decision.decision == ApprovalState.APPROVED
                        else RunStatus.BLOCKED
                    )
                    run.current_stage = (
                        "Approved action queued"
                        if decision.decision == ApprovalState.APPROVED
                        else "Approval rejected"
                    )
                else:
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
                sorted(self._dataset_approvals, key=lambda item: item.requested_at, reverse=True)
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
        approval is the local/dev demo-sandbox identity (issued solely when
        ``Settings.allow_demo_identity`` is explicitly enabled and never
        carrying a real Entra principal). This mirrors the existing
        ``DEMO_SANDBOX_SOURCE`` exemption in ``identity``/``agent_studio.authz``
        and keeps SOD strictly enforced for every real platform identity.
        """
        return identity.source == DEMO_SANDBOX_SOURCE

    def _append_dataset_audit(
        self,
        *,
        request_id: str,
        project_id: str,
        action: Literal["decided", "consumed"],
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
            delivery="pending",
        )
        with self._lock:
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
            requester_principal = self._dataset_requester_principals.get(request_id)
            if (
                requester_principal is not None
                and not self._dataset_sod_exempt(identity)
                and requester_principal == identity.user_id
            ):
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

        A caller can never mistake a denial for a grant: this either
        returns a ``CONSUMED`` record (never previously consumed, decided
        ``APPROVED``, matching this exact plan fingerprint, and not yet
        expired) or raises :class:`DatasetApprovalError`. Never fabricates a
        context and never allows the same decided approval to authorize a
        second, different, or replayed invocation.
        """
        with self._lock:
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
            now = utc_now()
            if record.expires_at <= now:
                raise DatasetApprovalError(
                    DatasetApprovalDenialReason.EXPIRED,
                    "Dataset approval request has expired.",
                )
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

    def dataset_approval_audit(self) -> list[DatasetApprovalAuditEntry]:
        """Full append-only dataset-approval audit trail, oldest first."""
        with self._lock:
            return deepcopy(sorted(self._dataset_audit, key=lambda entry: entry.recorded_at))

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
        scheduler_managed: bool = False,
        orchestration_input: dict[str, Any] | None = None,
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
            scheduler_managed=scheduler_managed,
            scheduling_state="pending" if scheduler_managed else "not_managed",
            orchestration_input=deepcopy(orchestration_input),
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
            "person-chen",
            "Dr. Maya Chen — Computational Biology",
            "Person",
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
            owner="Dr. Maya Chen",
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


def _connector(
    id: str,
    name: str,
    category: str,
    description: str,
    agents: list[str],
    terms_url: str,
    capabilities: list[str],
    *,
    auth_kind: str = "None",
    secret_status: str = "Not required",
    test_status: str = "ready",
) -> ConnectorSetting:
    return ConnectorSetting(
        id=id,
        name=name,
        category=category,
        description=description,
        auth_kind=auth_kind,
        secret_status=secret_status,
        enabled=True,
        test_status=test_status,
        assigned_agents=agents,
        terms_url=HttpUrl(terms_url),
        data_boundary="Public metadata only; query text is sent to the provider.",
        capabilities=capabilities,
    )


def _seed_connectors() -> list[ConnectorSetting]:
    return [
        _connector(
            "pubmed",
            "PubMed",
            "Literature",
            "Biomedical citations and abstracts from NCBI.",
            ["literature"],
            "https://www.ncbi.nlm.nih.gov/home/about/policies/",
            ["Search", "Metadata"],
        ),
        _connector(
            "europe_pmc",
            "Europe PMC",
            "Literature",
            "Life-sciences publications, grants, and links.",
            ["literature"],
            "https://europepmc.org/terms",
            ["Search", "Metadata"],
        ),
        _connector(
            "crossref",
            "Crossref",
            "Literature",
            "DOI metadata and scholarly work resolution.",
            ["literature", "grant"],
            "https://www.crossref.org/services/metadata-delivery/rest-api/",
            ["DOI resolution", "Metadata"],
        ),
        _connector(
            "openalex",
            "OpenAlex",
            "Discovery",
            "Open catalog of works, people, venues, and institutions.",
            ["literature", "matching", "dataset"],
            "https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication",
            ["Search", "Entity leads"],
        ),
        _connector(
            "arxiv",
            "arXiv",
            "Literature",
            "Preprint metadata for supported disciplines.",
            ["literature"],
            "https://info.arxiv.org/help/api/tou.html",
            ["Search", "Preprints"],
        ),
        _connector(
            "clinical_trials",
            "ClinicalTrials.gov",
            "Clinical research",
            "Clinical study records from the U.S. NLM.",
            ["literature"],
            "https://clinicaltrials.gov/about-site/terms-conditions",
            ["Trials", "Metadata"],
        ),
        _connector(
            "grants_gov",
            "Grants.gov",
            "Funding",
            "Authoritative U.S. federal opportunity records.",
            ["grant"],
            "https://www.grants.gov/web/grants/legal-privacy.html",
            ["Opportunities", "Requirements"],
        ),
        _connector(
            "nih_reporter",
            "NIH RePORTER",
            "Funding",
            "NIH funded-project and investigator metadata.",
            ["grant", "matching"],
            "https://reporter.nih.gov/termsconditions",
            ["Awards", "Project leads"],
        ),
        _connector(
            "datacite",
            "DataCite",
            "Datasets",
            "DOI metadata for datasets and research outputs.",
            ["literature", "dataset"],
            "https://support.datacite.org/docs/terms-and-conditions",
            ["Dataset discovery", "DOI resolution"],
        ),
        _connector(
            "orcid",
            "ORCID",
            "Identity",
            "Public researcher identifier records.",
            ["matching"],
            "https://info.orcid.org/terms-of-use/",
            ["Identity resolution"],
        ),
        _connector(
            "ror",
            "ROR",
            "Identity",
            "Open identifiers for research organizations.",
            ["matching"],
            "https://ror.org/terms/",
            ["Organization resolution"],
        ),
        _connector(
            "semantic_scholar",
            "Semantic Scholar",
            "Literature",
            "Paper and citation graph metadata.",
            ["literature"],
            "https://www.semanticscholar.org/product/api/license",
            ["Search", "Citation graph"],
            auth_kind="API key recommended",
            secret_status="Optional secret not configured",
            test_status="ready_with_key",
        ),
    ]


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
