"""Request/response schemas for the Agent Studio API surface.

Domain models from ``models.py`` are reused directly as response bodies
where they already have the right shape; this module only adds the request
bodies that need a narrower surface than the full domain model (e.g.
omitting server-derived fields like ``tenant_id``, which is always taken
from the authenticated identity, never from client input).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    AgentVisibility,
    HealthStatus,
    MemoryScopeKind,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    owner_kind: AgentOwnerKind = AgentOwnerKind.USER
    owner_id: str | None = None
    visibility: AgentVisibility = AgentVisibility.PRIVATE


class UpdateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: AgentManifest


class ForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_id: str
    new_logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")


class RunGatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: GateEvidence = Field(default_factory=GateEvidence)


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(min_length=1, max_length=200)
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="medium")


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    rationale: str | None = None


class EscalationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_role: AgentRole
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="high")


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    trace_ref: str | None = None


class HealthUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: HealthStatus
    detail: str = ""
    trace_ref: str | None = None


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    target_version_id: str


class AttachCapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor_id: str
    operation: str
    workspace_connection_id: str | None = None
    config: dict[str, object] = Field(default_factory=dict)


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_kind: MemoryScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default="note", max_length=40)
    content: str = Field(min_length=1, max_length=20000)


class RegisterToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=120)
    kind: ToolRegistrationKind
    handler_ref: str = Field(min_length=1, max_length=500)
