"""Domain models for the Agent Studio platform.

All models are plain Pydantic models so they serialize the same way as the
rest of the API (``model_dump(mode="json")`` for Cosmos persistence). Mutable
records (drafts, approvals, deployments) are plain ``BaseModel`` instances
updated via ``model_copy(update=...)``, matching the existing
``ApprovalRecord`` pattern in ``research_assistant_api.workspace``. Records
that are contractually immutable once created (``AgentVersion``,
``ReleaseGateReport``, ``LineageEdge``, ``EvaluationRecord``) use
``model_config = ConfigDict(frozen=True)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


#: Canonical JSON Schema version identifier for the persisted ``AgentManifest``
#: shape. Consumers outside this codebase (e.g. the harness) resolve the
#: manifest contract via ``GET /api/agent-studio/schemas/agent-manifest``
#: (JSON Schema + content digest), never by importing this Python class.
AGENT_MANIFEST_SCHEMA_VERSION = "agent-studio.manifest.v1"

#: Wire/protocol version for the overall Agent Studio release contract
#: (independent of the manifest's own schema version), recorded on every
#: immutable ``AgentVersion`` for forward-compatible interop.
AGENT_STUDIO_PROTOCOL_VERSION = "agent-studio.protocol.v1"


# --------------------------------------------------------------------------
# Ownership, roles, visibility
# --------------------------------------------------------------------------


class AgentOwnerKind(StrEnum):
    SYSTEM = "system"
    USER = "user"


class AgentVisibility(StrEnum):
    SYSTEM = "system"
    PRIVATE = "private"
    ORG = "org"
    PUBLISHED = "published"


class AgentRole(StrEnum):
    OWNER = "owner"
    MAINTAINER = "maintainer"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


_ROLE_RANK: dict[AgentRole, int] = {
    AgentRole.VIEWER: 0,
    AgentRole.CONTRIBUTOR: 1,
    AgentRole.MAINTAINER: 2,
    AgentRole.OWNER: 3,
}


def role_at_least(role: AgentRole, minimum: AgentRole) -> bool:
    """Return True when ``role`` grants at least ``minimum`` privilege."""
    return _ROLE_RANK[role] >= _ROLE_RANK[minimum]


class OwnershipGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str
    principal_id: str = Field(min_length=1, max_length=200)
    role: AgentRole
    granted_by: str = Field(min_length=1, max_length=200)
    granted_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------
# Runtime selection
# --------------------------------------------------------------------------


class RuntimeTarget(StrEnum):
    MANAGED_FOUNDRY = "managed_foundry"
    CUSTOM_HOSTED = "custom_hosted"


class RuntimeRequirements(BaseModel):
    """Application-declared facts used by deterministic runtime selection.

    These are facts about the manifest, not a user's runtime preference: the
    platform derives ``RuntimeTarget`` from these fields rather than trusting
    an arbitrary "deploy target" choice.
    """

    model_config = ConfigDict(extra="forbid")

    requires_custom_code: bool = False
    requires_custom_orchestration_workflow: bool = False
    requires_non_ga_tool: bool = False
    uses_project_deployed_model_only: bool = True


class RuntimeSelection(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: RuntimeTarget
    reasons: tuple[str, ...]


# --------------------------------------------------------------------------
# Capability catalog and attachment
# --------------------------------------------------------------------------


class OperationMaturity(StrEnum):
    GA = "ga"
    PREVIEW = "preview"
    UNAVAILABLE = "unavailable"


class OperationClass(StrEnum):
    """Deterministic side-effect classification for a capability operation.

    Independent of ``maturity`` (GA/preview/unavailable eligibility) and of
    ``requires_approval``/``side_effect_destinations`` (declared alongside it
    on ``CapabilityOperation``): an operation's class describes *what kind*
    of effect invoking it can have, not whether it is safe to attach or
    whether it needs human sign-off.
    """

    PURE = "pure"
    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_IRREVERSIBLE = "write_irreversible"
    PRIVILEGED = "privileged"


class CapabilityOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    maturity: OperationMaturity
    operation_class: OperationClass = OperationClass.READ
    side_effect_destinations: tuple[str, ...] = Field(default_factory=tuple)
    requires_approval: bool = False
    reason: str | None = None


class CapabilityDescriptor(BaseModel):
    """Provider-declared capability *catalog/governance* entry.

    ``operations`` is the honest, per-operation maturity surface: GA
    operations are attachable, ``preview``/``unavailable`` operations remain
    visible (with ``reason``) but are rejected at attach time. ``version``
    is the descriptor's own catalog version, pinned by any
    ``CapabilityBinding`` that attaches it (see below) so a later catalog
    update never silently changes an already-released agent's behavior.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=160)
    version: str = Field(default="1", min_length=1, max_length=40)
    provider: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    operations: tuple[CapabilityOperation, ...]
    auth_requirements: tuple[str, ...] = Field(default_factory=tuple)
    risk_tier: str = Field(default="low")
    data_boundary: str = Field(default="project")
    config_schema: dict[str, Any] = Field(default_factory=dict)
    managed_foundry_native: bool = False

    def operation(self, name: str) -> CapabilityOperation | None:
        return next((op for op in self.operations if op.name == name), None)


class CapabilityBinding(BaseModel):
    """An agent's *attachment* of a catalog operation: config + version pin.

    Distinct from ``CapabilityDescriptor`` (the catalog/governance entry) and
    from ``ToolRegistration`` (the runtime handler wiring below):
    ``CapabilityBinding`` only records that this manifest has chosen to use
    ``descriptor_id.operation`` at ``descriptor_version``, with what config
    and workspace connection.
    """

    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=160)
    descriptor_version: str = Field(default="1", min_length=1, max_length=40)
    operation: str = Field(min_length=1, max_length=120)
    workspace_connection_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    attached_by: str = Field(min_length=1, max_length=200)
    attached_at: datetime = Field(default_factory=utc_now)


class ToolRegistrationKind(StrEnum):
    """How a bound capability operation is actually invoked at runtime."""

    MANAGED_FOUNDRY_NATIVE = "managed_foundry_native"
    CUSTOM_HANDLER = "custom_handler"


class ToolRegistration(BaseModel):
    """Runtime handler wiring for a ``CapabilityBinding``.

    Separate from ``CapabilityDescriptor`` (catalog/governance) and
    ``CapabilityBinding`` (agent attachment/config/version pin): this record
    declares *how* an attached operation is dispatched at runtime — resolved
    natively by the Managed Foundry runtime, or routed to an
    application-owned handler for the Custom Hosted runtime. Immutable once
    created; re-pointing a tool to a different handler creates a new
    registration rather than mutating this one.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str
    descriptor_id: str = Field(min_length=1, max_length=160)
    operation: str = Field(min_length=1, max_length=120)
    kind: ToolRegistrationKind
    handler_ref: str = Field(min_length=1, max_length=500)
    registered_by: str = Field(min_length=1, max_length=200)
    registered_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------
# Workspace connections and model discovery
# --------------------------------------------------------------------------


class WorkspaceConnectionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=200)
    tenant_id: str
    project_id: str


class ModelDeploymentRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    deployment_name: str
    model_name: str
    model_format: str
    capacity: int | None = None


# --------------------------------------------------------------------------
# Memory scopes (GA mechanisms only)
# --------------------------------------------------------------------------


class MemoryScopeKind(StrEnum):
    CONVERSATION = "conversation"
    USER = "user"
    PROJECT = "project"
    PRIVATE_AGENT = "private_agent"


class MemoryMechanism(StrEnum):
    """GA-only memory mechanisms.

    ``application_thread`` and ``application_memory_store`` are both
    application-owned (our Cosmos persistence), which is GA today.
    ``foundry_native_memory_store`` refers to the Microsoft Foundry Agent
    Service "Memory" feature, which is documented as **preview** as of this
    writing; it is intentionally excluded from the default/attachable set and
    only ever surfaced through the capability catalog with
    ``OperationMaturity.PREVIEW``.
    """

    APPLICATION_THREAD = "application_thread"
    APPLICATION_MEMORY_STORE = "application_memory_store"
    FOUNDRY_NATIVE_MEMORY_STORE = "foundry_native_memory_store"

    @property
    def is_ga(self) -> bool:
        return self is not MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE


class MemoryScopeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MemoryScopeKind
    mechanism: MemoryMechanism = MemoryMechanism.APPLICATION_MEMORY_STORE
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class MemoryPolicy(BaseModel):
    """Manifest-level memory policy.

    Persistent memory is **off by default** (``enabled=False``): a manifest
    with an empty/absent policy has no memory access at all, even if a
    caller declares ``scopes``. Setting ``enabled=True`` is an explicit,
    auditable opt-in (recorded via draft updates) into application-owned GA
    memory mechanisms for the declared ``scopes`` only.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    scopes: tuple[MemoryScopeBinding, ...] = Field(default_factory=tuple)


class MemoryEntry(BaseModel):
    """A single application-owned (GA) memory record.

    Persisted and queried by ``(tenant_id, scope_kind, scope_id,
    logical_agent_id)`` for strict tenant/scope isolation.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str = Field(min_length=1)
    scope_kind: MemoryScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str
    role: str = Field(default="note", max_length=40)
    content: str = Field(min_length=1, max_length=20000)
    created_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------
# Agent manifest, drafts, immutable versions, lineage
# --------------------------------------------------------------------------


class AgentManifest(BaseModel):
    """The mutable, editable definition of an agent (the draft surface)."""

    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")
    tenant_id: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(default=AGENT_MANIFEST_SCHEMA_VERSION, min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    owner_kind: AgentOwnerKind
    owner_id: str = Field(min_length=1, max_length=200)
    visibility: AgentVisibility = AgentVisibility.PRIVATE
    capabilities: tuple[CapabilityBinding, ...] = Field(default_factory=tuple)
    runtime_requirements: RuntimeRequirements = Field(default_factory=RuntimeRequirements)
    model_deployment: ModelDeploymentRef | None = None
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    workspace_connections: tuple[str, ...] = Field(default_factory=tuple)
    tags: tuple[str, ...] = Field(default_factory=tuple)


class AgentDraft(BaseModel):
    """A mutable, in-progress edit of a manifest prior to cutting a version."""

    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    manifest: AgentManifest
    updated_by: str
    updated_at: datetime = Field(default_factory=utc_now)
    based_on_version_id: str | None = None


class LineageEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    child_logical_agent_id: str
    child_version_id: str
    parent_logical_agent_id: str
    parent_version_id: str
    relationship: str = Field(default="fork")


class AgentVersionStatus(StrEnum):
    DRAFT = "draft"
    GATED = "gated"
    APPROVED = "approved"
    RELEASED = "released"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


class AgentVersion(BaseModel):
    """An immutable, content-addressed release candidate/record.

    Once created, no field on this model is ever mutated in place; lifecycle
    progression is represented by appending a *new* ``AgentVersion`` document
    with an updated ``status`` copy (``model_copy(update={"status": ...})``)
    persisted under the same ``id`` only for status/gate_report linkage, never
    by changing ``manifest_hash``/``bundle_uri``.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    manifest: AgentManifest
    manifest_hash: str
    bundle_uri: str | None = None
    bundle_checksum: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str
    parent_version_id: str | None = None
    fork_of_version_id: str | None = None
    runtime_target: RuntimeTarget | None = None
    runtime_selection_reasons: tuple[str, ...] = Field(default_factory=tuple)
    model_deployment: ModelDeploymentRef | None = None
    capability_versions: dict[str, str] = Field(default_factory=dict)
    package_version: str = Field(default="0.0.0")
    protocol_version: str = Field(default=AGENT_STUDIO_PROTOCOL_VERSION)
    gate_report_id: str | None = None
    status: AgentVersionStatus = AgentVersionStatus.DRAFT


# --------------------------------------------------------------------------
# Advisory evaluations
# --------------------------------------------------------------------------


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version_id: str
    evaluator: str
    score: float | None = None
    advisory: bool = True
    summary: str
    created_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------
# Hard deterministic release gates
# --------------------------------------------------------------------------


class GateName(StrEnum):
    SCHEMA = "schema"
    BUILD = "build"
    TEST = "test"
    AUTH = "auth"
    POLICY = "policy"
    SECURITY = "security"
    SMOKE = "smoke"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: GateName
    status: GateStatus
    detail: str = ""


class ReleaseGateReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version_id: str
    results: tuple[GateResult, ...]
    evaluations: tuple[EvaluationRecord, ...] = Field(default_factory=tuple)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        """A release gate report only passes when every gate explicitly passed.

        ``SKIPPED`` (missing evidence) is deliberately treated as non-passing:
        a hard gate can never be silently bypassed by omission.
        """
        return all(result.status == GateStatus.PASSED for result in self.results)

    def blocking_gates(self) -> tuple[GateResult, ...]:
        return tuple(result for result in self.results if result.status != GateStatus.PASSED)


# --------------------------------------------------------------------------
# Approvals and admin escalation
# --------------------------------------------------------------------------


class ApprovalKind(StrEnum):
    RELEASE_PROMOTION = "release_promotion"
    FORK_PROMOTION = "fork_promotion"
    ADMIN_ESCALATION = "admin_escalation"


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class StudioApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    kind: ApprovalKind
    state: ApprovalState = ApprovalState.PENDING
    gated_action: str
    destination: str
    requested_by: str
    requested_at: datetime = Field(default_factory=utc_now)
    evidence_summary: str
    risk: str
    idempotency_key: str
    approver_id: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    requested_role: AgentRole | None = None
    """Only set for ADMIN_ESCALATION requests: the elevated role requested."""


# --------------------------------------------------------------------------
# Development deployments, health, rollback, logical ID resolution
# --------------------------------------------------------------------------


class DeploymentEnvironment(StrEnum):
    DEVELOPMENT = "development"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class DeploymentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: HealthStatus = HealthStatus.UNKNOWN
    checked_at: datetime = Field(default_factory=utc_now)
    detail: str = ""


class DeploymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    version_id: str
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    runtime_target: RuntimeTarget
    deployed_at: datetime = Field(default_factory=utc_now)
    deployed_by: str
    health: DeploymentHealth = Field(default_factory=DeploymentHealth)
    trace_ref: str | None = None
    rollback_of_deployment_id: str | None = None


class LogicalAgentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    resolved_version_id: str
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str
