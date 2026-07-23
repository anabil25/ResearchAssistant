from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(UTC)


class Capability(StrEnum):
    LITERATURE = "literature"
    GRANT = "grant"
    MATCHING = "matching"
    DATASET = "dataset"
    INSTITUTIONAL_QA = "institutional_qa"
    ORCHESTRATION = "orchestration"


class SourceKind(StrEnum):
    PAPER = "paper"
    POLICY = "policy"
    GRANT = "grant"
    PERSON = "person"
    FACILITY = "facility"
    EQUIPMENT = "equipment"
    METHOD = "method"
    TEMPLATE = "template"
    DATASET = "dataset"


class RunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: Capability
    title: str
    short_title: str
    description: str
    example_prompt: str
    accent: str


class EvidenceChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source_id: str
    source_kind: SourceKind
    title: str
    content: str
    section: str
    page_start: int | None = None
    page_end: int | None = None
    canonical_url: HttpUrl | None = None
    identifier: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    license: str = "CC0 synthetic fixture"
    version: str = "1"
    checksum: str
    allowed_tenants: list[str] = Field(default_factory=lambda: ["demo"])
    allowed_projects: list[str] = Field(default_factory=lambda: ["demo-project"])
    allowed_groups: list[str] = Field(default_factory=lambda: ["researchers"])
    access: str = "internal"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    source_id: str
    chunk_id: str
    title: str
    canonical_url: HttpUrl | None = None
    identifier: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str
    quote: str
    checksum: str
    license: str
    retrieved_at: datetime = Field(default_factory=utc_now)


class ArtifactSection(BaseModel):
    heading: str
    body: str
    citation_ids: list[str] = Field(default_factory=list)
    tone: str = "neutral"


class MatchScoreFactor(BaseModel):
    id: str
    label: str
    weight: float = Field(ge=0, le=1)
    match: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=1)
    evidence_id: str


class MatchItem(BaseModel):
    id: str
    name: str
    kind: SourceKind
    score: float = Field(ge=0, le=100)
    rationale: str
    citation_ids: list[str]
    freshness: str
    tags: list[str] = Field(default_factory=list)
    score_factors: list[MatchScoreFactor] = Field(default_factory=list)


class Metric(BaseModel):
    label: str
    value: str
    detail: str | None = None


class ProvenanceManifest(BaseModel):
    schema_version: str = "research-assistant.provenance.v1"
    run_id: str
    capability: Capability
    generated_at: datetime = Field(default_factory=utc_now)
    mode: str
    source_ids: list[str]
    source_checksums: dict[str, str]
    model_deployment: str
    verification: str
    caveats: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    id: str
    project_id: str
    tenant_id: str
    capability: Capability
    title: str
    status: RunStatus
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    progress: int = Field(default=0, ge=0, le=100)
    current_step: str
    steps: list[str] = Field(default_factory=list)


class ResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=4000)
    project_id: str = Field(default="demo-project", min_length=1, max_length=100)
    tenant_id: str = Field(default="demo", min_length=1, max_length=100)
    group_ids: list[str] = Field(default_factory=lambda: ["researchers"])
    context: dict[str, Any] = Field(default_factory=dict)


class ResearchResult(BaseModel):
    run: RunRecord
    eyebrow: str
    title: str
    summary: str
    sections: list[ArtifactSection]
    citations: list[Citation]
    warnings: list[str] = Field(default_factory=list)
    matches: list[MatchItem] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: ProvenanceManifest
