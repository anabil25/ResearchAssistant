from __future__ import annotations

from pydantic import BaseModel, Field
from research_assistant_core.models import Capability


class AssistantRequest(BaseModel):
    message: str = Field(min_length=3, max_length=8000)
    capability: Capability | None = None


class AssistantResponse(BaseModel):
    mode: str
    agent_name: str
    content: str
    response_id: str | None = None


class HealthResponse(BaseModel):
    status: str
    service: str
    mode: str


class ProjectSummary(BaseModel):
    id: str
    name: str
    description: str
    active_runs: int
    source_count: int
    is_active: bool = False
