"""Request/response schemas for the Agent Studio API surface.

Domain models from ``models.py`` are reused directly as response bodies
where they already have the right shape; this module only adds the request
bodies that need a narrower surface than the full domain model (e.g.
omitting server-derived fields like ``tenant_id``, which is always taken
from the authenticated identity, never from client input).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    AgentVisibility,
    CapabilityDescriptor,
    CapabilityInstance,
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


class CapabilityApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="medium")
    permissions_policy_ref: str | None = None
    destination_policy_ref: str | None = None


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
    instance_id: str | None = None
    connection_ref: str | None = None
    policy_ref: str | None = None
    config: dict[str, object] = Field(default_factory=dict)


class RememberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_kind: MemoryScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default="note", max_length=40)
    content: str = Field(min_length=1, max_length=20000)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    read_acl: tuple[str, ...] = Field(default_factory=tuple)
    write_acl: tuple[str, ...] = Field(default_factory=tuple)


class CorrectMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000)


class ForgetMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class RegisterToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=120)
    kind: ToolRegistrationKind
    handler_ref: str = Field(min_length=1, max_length=500)


class BuilderMessageRequest(BaseModel):
    """Request body for the Builder Agent's ``/builder/messages`` endpoint.

    Deliberately opaque: a free-form natural-language ``message`` string,
    never a JSON patch. This is the first of two structural safeguards
    against "arbitrary client-authored path patches" -- there is simply no
    patch-shaped input surface here at all.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000)
    base_etag: str = Field(min_length=1, max_length=200)


class BuilderApplyRequest(BaseModel):
    """Request body to apply a stored proposal. Never accepts a patch body:

    the server applies the proposal's own already-validated, already-stored
    ``after_manifest`` -- the second structural safeguard against arbitrary
    client-authored patches.
    """

    model_config = ConfigDict(extra="forbid")

    base_etag: str = Field(min_length=1, max_length=200)


class BuilderRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class CapabilityDiscoverySnapshot(BaseModel):
    """Combined descriptor/instance discovery read model.

    Convenience aggregate of ``GET /capabilities/descriptors`` and
    ``GET /capabilities/instances`` for callers (e.g. the future node
    palette/compiler) that want both plus honest, non-fatal discovery
    ``warnings`` and the ``refreshed_at`` timestamp in one call. Never
    itself persisted -- it is a read-time projection over the current
    ``CapabilityRegistry`` state.
    """

    model_config = ConfigDict(extra="forbid")

    descriptors: tuple[CapabilityDescriptor, ...]
    instances: tuple[CapabilityInstance, ...]
    warnings: tuple[str, ...]
    refreshed_at: datetime
