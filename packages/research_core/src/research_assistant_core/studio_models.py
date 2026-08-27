from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_core.models import Capability, Citation, RunStatus


class EvidenceState(StrEnum):
    VERIFIED = "verified"
    MODEL_ANALYSIS = "model_analysis"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class StudioRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=3, max_length=4000)
    inputs: dict[str, Any] = Field(default_factory=dict)


class StudioRun(BaseModel):
    id: str
    durable_instance_id: str
    capability: Capability
    title: str
    status: RunStatus
    current_stage: str
    progress: int = Field(ge=0, le=100)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    owner: str


class AgentInsight(BaseModel):
    agent_name: str
    content: str
    evidence_state: EvidenceState
    referenced_source_ids: list[str] = Field(default_factory=list)
    unresolved_source_ids: list[str] = Field(default_factory=list)
    source_retrieval_used: bool = False


class ReviewProtocol(BaseModel):
    research_question: str
    date_from: int
    date_to: int
    sources: list[str]
    inclusion_criteria: list[str]
    exclusion_criteria: list[str]


class ScreeningDecision(BaseModel):
    source_id: str
    title: str
    decision: str
    reason: str
    duplicate_group: str | None = None


class EvidenceExtraction(BaseModel):
    source_id: str
    method: str
    population: str
    outcome: str
    limitation: str
    citation_ids: list[str]


class LiteratureStudioResult(BaseModel):
    run: StudioRun
    protocol: ReviewProtocol
    search_queries: list[str]
    candidate_count: int
    screening: list[ScreeningDecision]
    extraction_matrix: list[EvidenceExtraction]
    synthesis: list[str]
    citations: list[Citation]
    insight: AgentInsight | None = None


class GrantOpportunity(BaseModel):
    identifier: str
    sponsor: str
    title: str
    deadline: str
    status: str
    canonical_url: str


class GrantRequirement(BaseModel):
    id: str
    text: str
    category: str
    status: str
    evidence_ids: list[str]


class ProjectFactGap(BaseModel):
    id: str
    label: str
    status: str
    guidance: str


class DraftSection(BaseModel):
    id: str
    title: str
    status: str
    word_count: int
    body: str
    evidence_ids: list[str]


class GrantStudioResult(BaseModel):
    run: StudioRun
    opportunity: GrantOpportunity
    requirements: list[GrantRequirement]
    fact_gaps: list[ProjectFactGap]
    specific_aims: list[str]
    sections: list[DraftSection]
    readiness: int = Field(ge=0, le=100)
    blockers: list[str]
    citations: list[Citation]
    insight: AgentInsight | None = None


class MatchCriterion(BaseModel):
    id: str
    label: str
    value: str
    kind: str
    weight: float = Field(ge=0, le=1)


class ScoreComponent(BaseModel):
    criterion_id: str
    label: str
    weight: float
    match: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0)
    evidence_id: str


class RankedEntity(BaseModel):
    id: str
    name: str
    kind: str
    hard_filters_passed: bool
    score: float = Field(ge=0, le=100)
    components: list[ScoreComponent]
    strengths: list[str]
    gaps: list[str]
    freshness: str


class MatchingStudioResult(BaseModel):
    run: StudioRun
    criteria: list[MatchCriterion]
    matches: list[RankedEntity]
    shortlist_ids: list[str]
    citations: list[Citation]
    insight: AgentInsight | None = None


class DatasetFieldProfile(BaseModel):
    name: str
    data_type: str
    missing: int
    unique: int
    range_or_values: str


class AnalysisStep(BaseModel):
    id: str
    question: str
    method: str
    status: str
    deterministic: bool


class ComputeProposal(BaseModel):
    adapter: str
    estimated_bytes: int
    estimated_cost_usd: float | None
    estimated_minutes: int | None
    approval_required: bool
    stages: list[str]


class DatasetStudioResult(BaseModel):
    run: StudioRun
    asset_name: str
    profile_status: str
    profile_note: str
    row_count: int
    column_count: int
    fields: list[DatasetFieldProfile]
    quality_findings: list[str]
    analysis_plan: list[AnalysisStep]
    compute_proposal: ComputeProposal
    interpretation: list[str]
    citations: list[Citation]
    insight: AgentInsight | None = None


class PolicyVersion(BaseModel):
    source_id: str
    title: str
    version: str
    effective_date: str
    status: str


class PolicyConflict(BaseModel):
    topic: str
    source_a: str
    source_b: str
    description: str
    severity: str


class InstitutionalStudioResult(BaseModel):
    run: StudioRun
    scope: str
    answer: str | None
    abstained: bool
    versions: list[PolicyVersion]
    conflicts: list[PolicyConflict]
    citations: list[Citation]
    escalation: str | None = None
    insight: AgentInsight | None = None


class AutomationStep(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    label: str
    kind: Literal[
        "activity",
        "fan_out",
        "agent",
        "approval",
        "external_action",
    ]
    depends_on: list[str]
    retry_limit: int = Field(ge=0, le=10)
    approval_required: bool


class AutomationStudioResult(BaseModel):
    run: StudioRun
    template_id: str
    trigger: str
    steps: list[AutomationStep]
    validation_errors: list[str]
    dry_run_status: str
    graph_version: str
    graph_hash: str
    citations: list[Citation]
    insight: AgentInsight | None = None
