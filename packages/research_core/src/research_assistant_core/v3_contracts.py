from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from research_assistant_core.models import Capability, RunStatus

V3_SCHEMA_VERSION = "research-assistant.v3"


def utc_now() -> datetime:
    return datetime.now(UTC)


class V3Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceResolution(StrEnum):
    VERIFIED = "verified"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class EvidenceReferenceV3(V3Contract):
    id: str = Field(pattern=r"^evidence-[a-z0-9-]{3,96}$")
    source_id: str
    chunk_id: str | None = None
    citation_id: str | None = None
    title: str
    resolution: EvidenceResolution
    quote: str | None = None
    checksum: str | None = None
    canonical_url: HttpUrl | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def verified_evidence_has_provenance(self) -> EvidenceReferenceV3:
        if self.resolution == EvidenceResolution.VERIFIED and not (
            self.chunk_id and self.citation_id and self.quote and self.checksum
        ):
            raise ValueError("Verified evidence requires chunk, citation, quote, and checksum provenance.")
        return self


class ArtifactKind(StrEnum):
    LITERATURE_SYNTHESIS = "literature_synthesis"
    GRANT_PACKAGE = "grant_package"
    MATCH_SHORTLIST = "match_shortlist"
    DATASET_REPORT = "dataset_report"
    INSTITUTIONAL_ANSWER = "institutional_answer"
    WORKFLOW_DEFINITION = "workflow_definition"


class ResearchArtifactV3(V3Contract):
    schema_version: Literal["research-assistant.v3"] = "research-assistant.v3"
    id: str = Field(pattern=r"^artifact-[a-z0-9-]{3,96}$")
    kind: ArtifactKind
    title: str = Field(min_length=3, max_length=240)
    markdown: str = Field(max_length=200_000)
    evidence: list[EvidenceReferenceV3] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    source_run_id: str
    created_by: str
    created_at: datetime = Field(default_factory=utc_now)


class LiteratureView(StrEnum):
    PROTOCOL = "protocol"
    SCREEN = "screen"
    EXTRACT = "extract"
    SYNTHESIZE = "synthesize"
    AUDIT = "audit"


class ScreeningDecisionV3(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    MAYBE = "maybe"


class LiteratureProtocolV3(V3Contract):
    question: str = Field(min_length=3, max_length=4000)
    framework: Literal["freeform", "pico", "pcc", "spider"] = "freeform"
    date_from: int = Field(ge=1000, le=2100)
    date_to: int = Field(ge=1000, le=2100)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    source_connector_ids: list[str] = Field(min_length=1)
    inclusion_criteria: list[str] = Field(min_length=1)
    exclusion_criteria: list[str] = Field(min_length=1)
    protocol_version: str = Field(pattern=r"^\d+\.\d+$")

    @model_validator(mode="after")
    def date_window_is_ordered(self) -> LiteratureProtocolV3:
        if self.date_from > self.date_to:
            raise ValueError("Literature protocol start year must not exceed end year.")
        return self


class ScreeningRecordV3(V3Contract):
    source_id: str
    decision: ScreeningDecisionV3
    reason: str = Field(min_length=3, max_length=2000)
    decided_by: str
    decided_at: datetime = Field(default_factory=utc_now)
    duplicate_group: str | None = None


class ExtractionValueV3(V3Contract):
    field_id: str
    value: str | int | float | bool | None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class ExtractionRecordV3(V3Contract):
    source_id: str
    values: list[ExtractionValueV3]


class LiteratureWorkspaceV3(V3Contract):
    active_view: LiteratureView
    protocol: LiteratureProtocolV3
    screening: list[ScreeningRecordV3] = Field(default_factory=list)
    extraction: list[ExtractionRecordV3] = Field(default_factory=list)
    artifact: ResearchArtifactV3 | None = None
    unresolved_evidence_ids: list[str] = Field(default_factory=list)


class GrantDiscoveryQueryV3(V3Contract):
    keywords: list[str] = Field(min_length=1)
    connector_ids: list[str] = Field(min_length=1)
    agencies: list[str] = Field(default_factory=list)
    deadline_from: datetime | None = None
    deadline_to: datetime | None = None
    saved_search_name: str | None = None


class GrantOpportunityV3(V3Contract):
    id: str
    connector_id: str
    sponsor: str
    title: str
    canonical_url: HttpUrl
    deadline: datetime | None = None
    status: Literal["forecast", "open", "closed", "archived"]
    amendment_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class EligibilityEvaluationV3(V3Contract):
    rule_id: str
    label: str
    result: Literal["pass", "fail", "unknown"]
    deterministic: bool = True
    evidence_ids: list[str] = Field(default_factory=list)
    missing_fact_ids: list[str] = Field(default_factory=list)


class GrantWorkspaceV3(V3Contract):
    discovery: GrantDiscoveryQueryV3
    opportunities: list[GrantOpportunityV3] = Field(default_factory=list)
    selected_opportunity_id: str | None = None
    eligibility: list[EligibilityEvaluationV3] = Field(default_factory=list)
    artifact: ResearchArtifactV3 | None = None
    review_findings: list[str] = Field(default_factory=list)
    export_ready: bool = False


class MatchEntityType(StrEnum):
    PERSON = "person"
    FACILITY = "facility"
    EQUIPMENT = "equipment"
    METHOD = "method"
    DATASET = "dataset"
    TEMPLATE = "template"


class MatchConstraintV3(V3Contract):
    id: str
    label: str
    value: str
    hard_filter: bool
    weight: float = Field(default=0, ge=0, le=1)


class MatchNeedV3(V3Contract):
    description: str = Field(min_length=3, max_length=4000)
    entity_types: list[MatchEntityType] = Field(min_length=1)
    source_connector_ids: list[str] = Field(min_length=1)
    constraints: list[MatchConstraintV3] = Field(default_factory=list)


class MatchScoreComponentV3(V3Contract):
    criterion_id: str
    label: str
    weight: float = Field(ge=0, le=1)
    match: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=100)
    evidence_ids: list[str] = Field(min_length=1)


class MatchCandidateV3(V3Contract):
    id: str
    name: str
    entity_type: MatchEntityType
    hard_filters_passed: bool
    score: float = Field(ge=0, le=100)
    components: list[MatchScoreComponentV3]
    evidence_ids: list[str] = Field(min_length=1)
    availability: Literal["unknown", "available", "unavailable"]

    @model_validator(mode="after")
    def score_equals_components(self) -> MatchCandidateV3:
        expected = round(sum(component.contribution for component in self.components), 4)
        if round(self.score, 4) != expected:
            raise ValueError("Match score must equal the sum of deterministic component contributions.")
        return self


class MatchingWorkspaceV3(V3Contract):
    need: MatchNeedV3
    candidates: list[MatchCandidateV3] = Field(default_factory=list)
    shortlist_ids: list[str] = Field(default_factory=list)
    artifact: ResearchArtifactV3 | None = None


class DatasetAssetState(StrEnum):
    QUARANTINED = "quarantined"
    VALIDATED = "validated"
    REJECTED = "rejected"
    READY = "ready"


class DatasetAssetV3(V3Contract):
    id: str
    filename: str
    content_type: str
    size_bytes: int = Field(gt=0)
    checksum: str
    blob_uri: str
    state: DatasetAssetState
    owner_id: str


class DatasetAnalysisStepV3(V3Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    question: str
    method: str
    code_language: Literal["python", "javascript", "shell"]
    deterministic: bool
    approval_required: bool


class DatasetExecutionV3(V3Contract):
    foundry_response_id: str | None = None
    toolbox_name: str = "research-dataset"
    tool_name: Literal["code_interpreter"] = "code_interpreter"
    status: Literal["planned", "waiting_for_approval", "running", "completed", "failed", "cancelled"]
    code_checksum: str | None = None
    environment_digest: str | None = None
    output_artifact_ids: list[str] = Field(default_factory=list)
    project_scoped_context: Literal[True] = True
    allowed_data_classification: Literal["public_or_synthetic"] = "public_or_synthetic"


class DatasetWorkspaceV3(V3Contract):
    assets: list[DatasetAssetV3] = Field(default_factory=list)
    analysis_plan: list[DatasetAnalysisStepV3] = Field(default_factory=list)
    execution: DatasetExecutionV3 | None = None
    artifact: ResearchArtifactV3 | None = None


class InstitutionalCorpusV3(V3Contract):
    id: str
    label: str
    enabled: bool
    authorized: bool
    source_kind: Literal["azure_ai_search", "file_search", "work_iq"]


class InstitutionalConflictV3(V3Contract):
    topic: str
    source_ids: list[str] = Field(min_length=2)
    description: str
    severity: Literal["info", "warning", "blocking"]


class InstitutionalAnswerV3(V3Contract):
    question: str
    answer_markdown: str | None
    abstained: bool
    evidence: list[EvidenceReferenceV3] = Field(default_factory=list)
    conflicts: list[InstitutionalConflictV3] = Field(default_factory=list)
    escalation: str | None = None

    @model_validator(mode="after")
    def abstention_is_explicit(self) -> InstitutionalAnswerV3:
        if self.abstained and self.answer_markdown:
            raise ValueError("An abstained institutional answer cannot include answer prose.")
        if not self.abstained and not self.answer_markdown:
            raise ValueError("A non-abstained institutional answer requires answer prose.")
        return self


class InstitutionalWorkspaceV3(V3Contract):
    corpora: list[InstitutionalCorpusV3]
    work_iq_ready: bool = False
    answer: InstitutionalAnswerV3 | None = None


class ConnectorProtocol(StrEnum):
    OPENAPI = "openapi"
    MCP = "mcp"
    WEB_ADAPTER = "web_adapter"


class ConnectorAuthMode(StrEnum):
    NONE = "none"
    MANAGED_IDENTITY = "managed_identity"
    KEY_VAULT_SECRET = "key_vault_secret"
    OAUTH_USER = "oauth_user"


class ConnectorLifecycle(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    CANARY = "canary"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class ToolboxBindingV3(V3Contract):
    toolbox_name: str
    toolbox_version: str
    server_label: str
    require_approval: Literal["always", "never"]
    default_version: bool


class ConnectorContractV3(V3Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str
    protocol: ConnectorProtocol
    auth_mode: ConnectorAuthMode
    lifecycle: ConnectorLifecycle
    allowed_hosts: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    terms_url: HttpUrl
    license_summary: str
    apim_api_version: str | None = None
    apim_revision: str | None = None
    mcp_server_version: str | None = None
    toolbox_binding: ToolboxBindingV3 | None = None
    assigned_capabilities: list[Capability] = Field(default_factory=list)

    @model_validator(mode="after")
    def active_connector_is_governed(self) -> ConnectorContractV3:
        if self.lifecycle == ConnectorLifecycle.ACTIVE and not (
            self.apim_api_version and self.mcp_server_version and self.toolbox_binding
        ):
            raise ValueError("An active connector requires APIM, MCP, and Toolbox version bindings.")
        return self


class WorkflowNodeKind(StrEnum):
    STUDIO = "studio"
    AGENT = "agent"
    A2A_AGENT = "a2a_agent"
    TOOLBOX_TOOL = "toolbox_tool"
    RETRIEVAL = "retrieval"
    TRANSFORM = "transform"
    CONDITION = "condition"
    PARALLEL = "parallel"
    JOIN = "join"
    APPROVAL = "approval"
    DELAY = "delay"
    EXPORT = "export"


class WorkflowPortV3(V3Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    schema_ref: str
    required: bool = True


class WorkflowNodeV3(V3Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    label: str
    kind: WorkflowNodeKind
    inputs: list[WorkflowPortV3] = Field(default_factory=list)
    outputs: list[WorkflowPortV3] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    retry_limit: int = Field(default=0, ge=0, le=10)
    approval_required: bool = False


class WorkflowEdgeV3(V3Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    source_node_id: str
    source_port_id: str
    target_node_id: str
    target_port_id: str


class WorkflowTriggerV3(V3Contract):
    kind: Literal["manual", "schedule", "webhook", "github_issue", "library_ingest"]
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowDefinitionV3(V3Contract):
    schema_version: Literal["research-assistant.v3"] = "research-assistant.v3"
    id: str = Field(pattern=r"^workflow-[a-z0-9-]{3,96}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    name: str
    trigger: WorkflowTriggerV3
    nodes: list[WorkflowNodeV3] = Field(min_length=1)
    edges: list[WorkflowEdgeV3] = Field(default_factory=list)
    execution_mode: Literal["agent_framework", "durable_agent_framework", "durable_task"]
    active: bool = False

    @model_validator(mode="after")
    def graph_is_valid(self) -> WorkflowDefinitionV3:
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Workflow node IDs must be unique.")
        if len({edge.id for edge in self.edges}) != len(self.edges):
            raise ValueError("Workflow edge IDs must be unique.")
        adjacency: dict[str, list[str]] = {node_id: [] for node_id in nodes}
        for edge in self.edges:
            source = nodes.get(edge.source_node_id)
            target = nodes.get(edge.target_node_id)
            if source is None or target is None:
                raise ValueError("Workflow edges must reference existing nodes.")
            if edge.source_port_id not in {port.id for port in source.outputs}:
                raise ValueError(f"Unknown source port {edge.source_port_id}.")
            if edge.target_port_id not in {port.id for port in target.inputs}:
                raise ValueError(f"Unknown target port {edge.target_port_id}.")
            adjacency[source.id].append(target.id)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("Workflow graphs must be acyclic.")
            if node_id in visited:
                return
            visiting.add(node_id)
            for child_id in adjacency[node_id]:
                visit(child_id)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in nodes:
            visit(node_id)
        return self


class ApprovalRequestV3(V3Contract):
    """Approval request lifecycle contract.

    ``state`` intentionally mirrors the live, wired implementation in
    ``research_assistant_api.workspace.ApprovalState`` (pending/approved/
    rejected/cancelled) rather than a broader aspirational set. There is no
    ``changes_requested``/``expired``/``withdrawn`` transition anywhere in the
    running API, generated frontend client (``ApprovalState`` in
    ``generated-api.ts``), or UI — keeping this literal in sync with those
    boundaries avoids the contract drift where this "authoritative" schema
    promised a decision the platform does not implement.
    """

    id: str = Field(pattern=r"^approval-[a-z0-9-]{3,96}$")
    run_id: str
    node_id: str | None = None
    action: str = Field(min_length=3, max_length=2000)
    destination: str = Field(min_length=1, max_length=1000)
    payload_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    evidence_ids: list[str] = Field(default_factory=list)
    policy_reason: str
    requested_by: str
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    state: Literal["pending", "approved", "rejected", "cancelled"] = "pending"


class ApprovalDecisionV3(V3Contract):
    """Approval decision contract.

    ``decision`` is restricted to ``approved``/``rejected`` to match the
    live enforcement in ``research_assistant_api.workspace.ApprovalDecision.
    validate_decision``, which explicitly rejects any other value
    (including ``changes_requested``). ``cancelled`` is a system-driven
    state transition (e.g. run cancellation), not an approver decision, so
    it is correctly excluded here too.
    """

    approval_id: str
    decision: Literal["approved", "rejected"]
    rationale: str = Field(min_length=3, max_length=2000)
    decided_by: str
    decided_at: datetime = Field(default_factory=utc_now)


class RunEventV3(V3Contract):
    sequence: int = Field(ge=0)
    event_type: str
    node_id: str | None = None
    status: str
    message: str
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RunArtifactV3(V3Contract):
    artifact_id: str
    kind: ArtifactKind
    blob_uri: str
    checksum: str


class RunContractV3(V3Contract):
    schema_version: Literal["research-assistant.v3"] = "research-assistant.v3"
    id: str
    tenant_id: str
    project_id: str
    capability: Capability | None = None
    workflow_id: str | None = None
    workflow_version: str | None = None
    status: RunStatus
    current_node_id: str | None = None
    progress: int = Field(ge=0, le=100)
    execution_mode: Literal["request", "agent_framework", "durable_agent_framework", "durable_task", "routine"]
    trace_id: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    events: list[RunEventV3] = Field(default_factory=list)
    artifacts: list[RunArtifactV3] = Field(default_factory=list)
    pending_approval_ids: list[str] = Field(default_factory=list)


V3_CONTRACT_MODELS: tuple[type[BaseModel], ...] = (
    EvidenceReferenceV3,
    ResearchArtifactV3,
    LiteratureProtocolV3,
    ScreeningRecordV3,
    ExtractionRecordV3,
    LiteratureWorkspaceV3,
    GrantDiscoveryQueryV3,
    GrantOpportunityV3,
    EligibilityEvaluationV3,
    GrantWorkspaceV3,
    MatchNeedV3,
    MatchCandidateV3,
    MatchingWorkspaceV3,
    DatasetAssetV3,
    DatasetAnalysisStepV3,
    DatasetExecutionV3,
    DatasetWorkspaceV3,
    InstitutionalCorpusV3,
    InstitutionalAnswerV3,
    InstitutionalWorkspaceV3,
    ConnectorContractV3,
    WorkflowDefinitionV3,
    ApprovalRequestV3,
    ApprovalDecisionV3,
    RunContractV3,
)


def v3_contract_bundle() -> dict[str, Any]:
    return {
        "schema_version": V3_SCHEMA_VERSION,
        "generated_at": "deterministic",
        "schemas": {
            model.__name__: model.model_json_schema(mode="serialization")
            for model in V3_CONTRACT_MODELS
        },
    }
