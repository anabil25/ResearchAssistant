from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from research_assistant_core.models import Capability, PublicDiscoveryRequest


class AssistantRequest(BaseModel):
    message: str = Field(min_length=3, max_length=8000)
    capability: Capability | None = None
    public_discovery: PublicDiscoveryRequest | None = None


class AssistantResponse(BaseModel):
    mode: str
    agent_name: str
    content: str
    response_id: str | None = None


class AgentEvidenceView(BaseModel):
    evidence_id: str = Field(min_length=1, max_length=256)
    source_uri: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=512)
    version: str | None = Field(default=None, max_length=128)


class AgentClaimView(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    support: Literal["supported", "unsupported", "conflicting"]
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_matches_support(self) -> AgentClaimView:
        if self.support in {"supported", "conflicting"} and not self.evidence_ids:
            raise ValueError("supported and conflicting claims require evidence identifiers")
        if self.support == "unsupported" and self.evidence_ids:
            raise ValueError("unsupported claims cannot cite evidence")
        return self


class AgentResearchResponse(BaseModel):
    capability: Capability
    run_id: str
    agent_name: str
    response_id: str | None = None
    summary: str = Field(min_length=1, max_length=40_000)
    claims: list[AgentClaimView] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence: list[AgentEvidenceView] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    description: str
    active_runs: int
    source_count: int
    is_active: bool = False
