"""Request/response schemas for the Agent Studio API surface.

Domain models from ``models.py`` are reused directly as response bodies
where they already have the right shape; this module only adds the request
bodies that need a narrower surface than the full domain model (e.g.
omitting server-derived fields like ``tenant_id``, which is always taken
from the authenticated identity, never from client input).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    AgentVisibility,
    CapabilityDescriptor,
    CapabilityInstance,
    DeploymentEnvironment,
    EvaluationTestCase,
    HealthStatus,
    MemoryScopeKind,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")
    project_id: str = Field(min_length=1, max_length=200)
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

    project_id: str = Field(min_length=1, max_length=200)
    source_version_id: str
    new_logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")


class RunGatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    evidence: GateEvidence = Field(default_factory=GateEvidence)
    #: Harness's own release identity/scoped-manifest digest for the runtime
    #: release this gate run corresponds to (harness blocker #1: "signed
    #: release linkage"). Optional -- omitted entirely for releases with no
    #: harness counterpart. Must be supplied together (both or neither); the
    #: resulting ``AgentRelease``/``ReleaseAttestation`` never asserts these
    #: are equal to this package's own ``manifest_hash``.
    harness_release_id: str | None = Field(default=None, max_length=500)
    harness_manifest_digest: str | None = Field(default=None, max_length=200)


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="medium")


class ActivationRequest(BaseModel):
    """Explicit request to activate an APPROVED release once deploy+smoke evidence exists.

    ACTIVE is never an implicit side effect of promotion, ``deploy()``, or
    ``record_health()`` — this is the one caller-visible action that flips a
    version live for an environment, and it is rejected unless a healthy
    ``DeploymentRecord`` for the exact version/environment already exists.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    approve: bool
    rationale: str | None = None


class RevokeApprovalRequest(BaseModel):
    """Request body to append an ``ApprovalRevocation`` for a request/decision.

    Revocation is permanent and append-only -- there is no corresponding
    "un-revoke" request; a fresh approval request is required afterward if
    the underlying action is still needed.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)


class ResolveApprovalContextRequest(BaseModel):
    """Request body to resolve a trusted ``ApprovalContext`` for an
    about-to-run capability-operation invocation.

    Carries only the plan's identifying facts (release/binding/operation) --
    deliberately excludes ``approval_id``/``invocation_id``: a caller can
    never assert which approval authorizes it or mint its own
    ``invocation_id``. Both are always resolved and minted server-side by
    ``ApprovalContextResolver`` from the release's own currently-effective
    ``CAPABILITY_OPERATION`` approval for this exact binding/operation,
    closing the gap where a hosted caller had no trusted way to discover
    either value before calling ``/approvals/{approval_id}/consume``.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    release_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)


class ConsumeCapabilityApprovalRequest(BaseModel):
    """Request body to durably, atomically spend a ``CAPABILITY_OPERATION``
    approval at actual runtime invocation.

    This is the wire contract the callers referenced in
    ``approval_consumption.ApprovalConsumptionRequest`` carry the client;
    ``tenant_id``/``project_id`` (via the authenticated identity + this
    request's ``project_id``) and ``principal_id`` (the caller's own
    authenticated identity) are never accepted from the client -- a runtime
    invocation cannot assert who it is or which tenant it belongs to, it can
    only present the decision reference (``approval_id``, from the path)
    plus the concrete facts of *this* invocation, all of which are
    independently revalidated against the approval's own pinned
    binding/operation/instance/policy/release server-side before anything
    is durably consumed. There is deliberately no boolean "is this
    approved" field anywhere on this request: authority comes entirely from
    the referenced, previously-decided ``StudioApprovalRecord``, never from
    anything the caller asserts inline.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    instance_fingerprint: str | None = None
    operation_id: str = Field(min_length=1, max_length=200)
    operation_version: str | None = None
    args_hash: str = Field(min_length=1)
    destination_hash: str = Field(min_length=1)
    policy_ref: str | None = None
    release_id: str | None = None
    invocation_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1)


class EscalationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    requested_role: AgentRole
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="high")


class CapabilityApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    descriptor_id: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    evidence_summary: str = Field(min_length=1, max_length=4000)
    risk: str = Field(default="medium")
    permissions_policy_ref: str | None = None
    destination_policy_ref: str | None = None


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    version_id: str
    trace_ref: str | None = None


class PublishPromptAgentRequest(BaseModel):
    """Request a durable, idempotent Foundry prompt-agent publication."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=256)


class HealthUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    status: HealthStatus
    detail: str = ""
    trace_ref: str | None = None


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
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

    project_id: str = Field(min_length=1, max_length=200)
    scope_kind: MemoryScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    role: str = Field(default="note", max_length=40)
    content: str = Field(min_length=1, max_length=20000)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    read_acl: tuple[str, ...] = Field(default_factory=tuple)
    write_acl: tuple[str, ...] = Field(default_factory=tuple)


class CorrectMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)


class ForgetMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


class RegisterToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    descriptor_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=120)
    kind: ToolRegistrationKind
    handler_ref: str = Field(min_length=1, max_length=500)


class CreateEvaluationSuiteRequest(BaseModel):
    """Request body to create a new named ``EvaluationSuite`` for an agent."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    test_cases: tuple[EvaluationTestCase, ...] = Field(default_factory=tuple)


class CreateEvaluationRunRequest(BaseModel):
    """Request body to trigger one advisory evaluation run of a suite.

    ``version_id`` pins an exact, immutable ``AgentVersion``; omitted, the
    run targets the agent's current draft. Never a request to *modify* the
    suite or the target -- purely which fixed content to run and score.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    version_id: str | None = None


class CreateTestRunRequest(BaseModel):
    """Request body to invoke the agent once in the interactive Test/Playground tab.

    ``version_id`` pins an exact, immutable ``AgentVersion``; omitted, the
    run targets the agent's current draft. Side effects during a run are
    always the deterministic, domain-owned ``SideEffectPolicy.DRY_RUN`` --
    never a client- or model-chosen behavior.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    version_id: str | None = None
    input: str = Field(min_length=1, max_length=8000)


class BuilderMessageRequest(BaseModel):
    """Request body for the Builder Agent's ``/builder/messages`` endpoint.

    Deliberately opaque: a free-form natural-language ``message`` string,
    never a JSON patch. This is the first of two structural safeguards
    against "arbitrary client-authored path patches" -- there is simply no
    patch-shaped input surface here at all.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    base_etag: str = Field(min_length=1, max_length=200)


class BuilderApplyRequest(BaseModel):
    """Request body to apply a stored proposal. Never accepts a patch body:

    the server applies the proposal's own already-validated, already-stored
    ``after_manifest`` -- the second structural safeguard against arbitrary
    client-authored patches.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    base_etag: str = Field(min_length=1, max_length=200)


class BuilderRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


class CapabilityDiscoverySnapshot(BaseModel):
    """Combined descriptor/instance discovery read model.

    Convenience aggregate of ``GET /capabilities/descriptors`` and
    ``GET /capabilities/instances`` for callers (e.g. the future node
    palette/compiler) that want both plus honest, non-fatal discovery
    ``warnings`` and the ``refreshed_at`` timestamp in one call. Never
    itself persisted -- it is a read-time projection over the current
    ``CapabilityRegistry`` state.

    ``available``/``unavailable_reason`` distinguish "discovery ran and
    honestly found nothing" (``available=True``, empty ``descriptors``/
    ``instances``) from "no provider integration is configured/reachable
    right now" (``available=False``) -- a UI must never render these two
    situations identically.
    """

    model_config = ConfigDict(extra="forbid")

    descriptors: tuple[CapabilityDescriptor, ...]
    instances: tuple[CapabilityInstance, ...]
    warnings: tuple[str, ...]
    refreshed_at: datetime
    available: bool = True
    unavailable_reason: str | None = None


class IdempotencyKeyFields(BaseModel):
    """Shared, non-optional identity fields for a single idempotent runtime
    invocation, common to every ``/idempotency/*`` request.

    ``tenant_id`` is deliberately absent -- it always comes from the
    authenticated identity via the resolved ``ScopeContext``, never from a
    client-supplied field, mirroring every other scoped request body in
    this module.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=512)
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=256)
    destination: str = Field(min_length=1, max_length=1024)
    caller_key: str = Field(min_length=1, max_length=256)
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClaimIdempotencyRequest(IdempotencyKeyFields):
    """Request body to atomically claim a fresh durable idempotency lease.

    ``actor_id`` is never accepted here -- the acting actor is always the
    authenticated caller's own identity, exactly like every other mutating
    Agent Studio route.
    """

    release_id: str = Field(min_length=1, max_length=256)
    lease_seconds: float = Field(default=300.0, gt=0, le=3600)


class MarkIdempotencyInProgressRequest(IdempotencyKeyFields):
    """Request body to transition a durably claimed key to ``IN_PROGRESS``.

    ``claim_token``/``expected_version`` are the one-time proof of ownership
    returned by ``claim`` -- without the exact current pair, the transition
    is rejected as a concurrency conflict rather than silently reapplied.
    """

    claim_token: str = Field(min_length=32, max_length=256)
    expected_version: str = Field(min_length=1, max_length=32)
    irreversible: bool = False


class CompleteIdempotencyRequest(IdempotencyKeyFields):
    """Request body to durably record a successful completion.

    ``result`` is the raw, JSON-able outcome payload; its digest is always
    independently recomputed server-side (never trusted from
    ``expected_result_hash``, which is only an optional caller-side sanity
    assertion checked against that recomputed digest).
    """

    claim_token: str = Field(min_length=32, max_length=256)
    expected_version: str = Field(min_length=1, max_length=32)
    result: dict[str, Any] = Field(default_factory=dict)
    expected_result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FailIdempotencyRequest(IdempotencyKeyFields):
    """Request body to durably record a failed outcome requiring
    reconciliation (the true side-effect outcome of the original attempt is
    unknown and must never be silently retried as if it were fresh)."""

    claim_token: str = Field(min_length=32, max_length=256)
    expected_version: str = Field(min_length=1, max_length=32)
    failure_code: str = Field(min_length=1, max_length=128)


class LoadIdempotencyResultRequest(IdempotencyKeyFields):
    """Request body to replay a previously completed idempotency result.

    Deliberately keyed by the full ``IdempotencyKeyFields`` identity plus
    ``release_id`` -- never by a caller-supplied ``result_ref`` string,
    which alone carries no binding to any specific key or release and
    cannot be independently re-verified. ``release_id`` is the caller's
    provenance assertion: it is checked against the release that actually
    completed this key, and a mismatch is rejected rather than silently
    served.
    """

    release_id: str = Field(min_length=1, max_length=256)

