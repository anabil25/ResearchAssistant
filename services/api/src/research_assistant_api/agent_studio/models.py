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
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


#: Canonical JSON Schema version identifier for the persisted ``AgentManifest``
#: shape. Consumers outside this codebase (e.g. the harness) resolve the
#: manifest contract via ``GET /v1/agent-studio/schemas/agent-manifest``
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
    #: Workspace/project membership boundary. Every grant is scoped to an
    #: exact ``(tenant_id, project_id)`` partition (see ``scope.py``); there
    #: is no "tenant-wide" grant. Platform owners hold grants scoped to
    #: ``scope.PLATFORM_PROJECT_ID`` for system-agent ownership rather than a
    #: null/optional project.
    project_id: str = Field(min_length=1, max_length=200)


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class DeploymentEnvironment(StrEnum):
    DEVELOPMENT = "development"


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
    """Per-operation maturity. Only ``GA`` is ever attachable.

    ``RETIRED`` marks an operation that was once available but has been
    withdrawn (kept in the catalog for historical/audit visibility, never
    attachable again). ``UNKNOWN`` is the fail-closed default for an
    operation whose maturity could not be positively confirmed from
    provenance (e.g. a discovery source that didn't report a maturity tier);
    it is treated identically to ``UNAVAILABLE`` for attachment purposes —
    "unknown" must never be silently treated as safe-to-attach.
    """

    GA = "ga"
    PREVIEW = "preview"
    UNAVAILABLE = "unavailable"
    RETIRED = "retired"
    UNKNOWN = "unknown"


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
    """A single operation on a capability descriptor.

    ``maturity``/``risk``/``operation_class``/``requires_approval`` are all
    operation-level (not descriptor-level): two operations on the same
    descriptor can have entirely different maturity and risk profiles.
    ``source_url``/``source_version``/``last_verified_at`` are the honest
    provenance trail for the maturity claim — where it was confirmed, against
    which provider release, and when it was last checked; a maturity claim
    with no provenance is a catalog-authoring smell, not a runtime error.

    ``requires_approval`` is *not* purely informational: ``approval_policy_ref``
    names the versioned approval policy that ``CapabilityRegistry.attach``
    (attach-time satisfiability), the ``APPROVAL`` release gate (cut/release
    hard-block on missing/expired/mismatched approval), and deploy-time
    checks all resolve against. ``least_privilege_scopes``/``least_privilege_roles``
    declare the minimum access an invocation needs; ``timeout_seconds``/
    ``max_retries``/``idempotent`` are runtime dispatch contract facts (not
    behavior this backend executes itself — the harness/runtime owns
    invocation — but real, declared metadata a caller must honor).
    ``input_schema_digest``/``output_schema_digest`` are operation-level
    (independent of the manifest's own ``input_schema_ref``/``output_schema_ref``),
    since a single descriptor's operations can have distinct I/O shapes.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    maturity: OperationMaturity
    operation_class: OperationClass = OperationClass.READ
    risk: str = Field(default="low")
    side_effect_destinations: tuple[str, ...] = Field(default_factory=tuple)
    requires_approval: bool = False
    approval_policy_ref: str | None = None
    reason: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    last_verified_at: datetime | None = None
    input_schema_digest: str | None = None
    output_schema_digest: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    max_retries: int = Field(default=0, ge=0, le=10)
    idempotent: bool = False
    least_privilege_scopes: tuple[str, ...] = Field(default_factory=tuple)
    least_privilege_roles: tuple[str, ...] = Field(default_factory=tuple)


class CapabilityDescriptor(BaseModel):
    """Provider-declared capability *catalog/governance* entry.

    ``operations`` is the honest, per-operation maturity surface: GA
    operations are attachable, ``preview``/``retired``/``unavailable``/
    ``unknown`` operations remain visible (with ``reason``) but are rejected
    at attach time. ``version`` is the descriptor's own catalog version,
    pinned by any ``CapabilityBinding`` that attaches it (see below) so a
    later catalog update never silently changes an already-released agent's
    behavior.
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


class InstanceReadiness(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityInstance(BaseModel):
    """A tenant/project-discovered *resource* for a capability descriptor.

    Distinct from ``CapabilityDescriptor`` (immutable provider-wide catalog
    semantics/governance) and from ``CapabilityBinding`` (an agent's
    attachment): this is the concrete, discovered thing a binding points at
    via ``instance_id`` — e.g. a specific Azure AI Search index connection
    that this tenant/project actually has, with its own readiness/health
    independent of the descriptor's static catalog entry. Never persisted
    inside ``AgentManifest``; resolved and validated at attach/gate time.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    descriptor_id: str = Field(min_length=1, max_length=160)
    #: The exact ``CapabilityDescriptor.version`` consulted when this
    #: instance was discovered/registered — the descriptor-side half of the
    #: pin that ``CapabilityBinding.descriptor_digest``/``instance_fingerprint``
    #: freeze at attach time.
    descriptor_version: str = Field(default="1", min_length=1, max_length=40)
    discovered_provider_version: str | None = None
    readiness: InstanceReadiness = InstanceReadiness.UNAVAILABLE
    health_status: HealthStatus = HealthStatus.UNKNOWN
    config_fingerprint: str | None = None
    #: Canonical digest pinning provider/descriptor/operation identity,
    #: operation definitions/versions, side-effect destinations, tenant/data
    #: boundaries, and non-secret discovered configuration for *this*
    #: instance — see ``capability_registry.compute_instance_fingerprint``.
    #: Excludes health/timestamps/secrets/credential material, so a
    #: readiness flap alone never invalidates a pinned binding. Computed at
    #: discovery/registration time and copied into any ``CapabilityBinding``
    #: that attaches this instance, so reconfiguration (not just health
    #: drift) is independently detectable before release/invoke.
    instance_fingerprint: str | None = None
    unavailable_reason: str | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    registered_by: str = Field(min_length=1, max_length=200)


class CapabilityBinding(BaseModel):
    """An agent's *attachment* of a catalog operation: config + version pins.

    Distinct from ``CapabilityDescriptor`` (the catalog/governance entry),
    ``CapabilityInstance`` (the discovered tenant/project resource), and
    ``ToolRegistrationSpec`` (the runtime handler wiring below): ``CapabilityBinding``
    only records that this manifest has chosen to use ``descriptor_id.operation``
    at ``descriptor_version``/``pinned_provider_version``, pointed at a specific
    ``instance_id`` (when the operation requires a discovered resource) via
    ``connection_ref``, with what config and policy.

    ``descriptor_digest`` pins the *content* of the attached descriptor (not
    just its declared ``descriptor_version`` string) so a catalog edit that
    bumps content without bumping the version string cannot silently change
    an already-attached binding's behavior. ``input_schema_digest``/
    ``output_schema_digest`` are copied from the resolved
    ``CapabilityOperation`` at attach time — independent digests because a
    descriptor's operations can have distinct I/O shapes. ``instance_fingerprint``
    is copied from the resolved ``CapabilityInstance`` at attach time (see
    ``CapabilityInstance.instance_fingerprint``); release/invoke re-checks it
    against the current instance and hard-fails on drift (stale binding).
    ``config_hash`` is a canonical digest of ``config`` computed at attach
    time so any later config drift is independently detectable.
    """

    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=160)
    descriptor_version: str = Field(default="1", min_length=1, max_length=40)
    descriptor_digest: str | None = None
    operation: str = Field(min_length=1, max_length=120)
    instance_id: str | None = None
    pinned_provider_version: str | None = None
    instance_fingerprint: str | None = None
    input_schema_digest: str | None = None
    output_schema_digest: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    config_hash: str | None = None
    connection_ref: str | None = None
    policy_ref: str | None = None
    attached_by: str = Field(min_length=1, max_length=200)
    attached_at: datetime = Field(default_factory=utc_now)


class ToolRegistrationKind(StrEnum):
    """How a bound capability operation is actually invoked at runtime."""

    MANAGED_FOUNDRY_NATIVE = "managed_foundry_native"
    CUSTOM_HANDLER = "custom_handler"


class ToolRegistrationSpec(BaseModel):
    """Persisted *spec* declaring how a ``CapabilityBinding`` is dispatched.

    Separate from ``CapabilityDescriptor`` (catalog/governance) and
    ``CapabilityBinding`` (agent attachment/config/version pin): this record
    declares *how* an attached operation is dispatched at runtime — resolved
    natively by the Managed Foundry runtime, or routed to an
    application-owned handler for the Custom Hosted runtime.

    This is a *spec* (data), not a runtime handler: ``handler_ref`` is an
    opaque reference the harness/provider compiler resolves into the actual
    non-serializable, callable ``ToolRegistration`` object at dispatch time.
    This backend never constructs or serializes a callable handler — only
    this spec. Immutable once created; re-pointing a tool to a different
    handler creates a new spec rather than mutating this one.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
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
    """Per-scope memory configuration.

    There is deliberately no manifest-wide "memory enabled" bit: each scope
    independently declares ``enabled`` (may this scope be accessed at all)
    and, separately, ``persistent`` (may entries in this scope outlive the
    current conversation/session). Conversation memory may be ``enabled=True``
    while user/project/private-agent scopes stay ``enabled=False``; a scope
    can even be ``enabled=True`` but ``persistent=False`` (session-only
    working memory). ``persistent`` defaults ``False`` even when the scope
    itself is enabled. ``default_read_acl``/``default_write_acl`` declare the
    scope's baseline ACL (applied to entries that don't override it);
    ``allow_user_inspect``/``allow_user_forget``/``allow_user_export`` are the
    end-user self-service controls this scope exposes; ``provenance``
    records where/why this scope was configured (e.g. an admin policy vs. an
    owner opt-in), for inspect/audit.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MemoryScopeKind
    enabled: bool = False
    persistent: bool = False
    mechanism: MemoryMechanism = MemoryMechanism.APPLICATION_MEMORY_STORE
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    default_read_acl: tuple[str, ...] = Field(default_factory=tuple)
    default_write_acl: tuple[str, ...] = Field(default_factory=tuple)
    allow_user_inspect: bool = True
    allow_user_forget: bool = True
    allow_user_export: bool = True
    provenance: str = Field(default="", max_length=200)


class MemoryPolicy(BaseModel):
    """Manifest-level memory policy: an independent per-scope declaration.

    A manifest with no declared ``scopes`` (or a scope with ``enabled=False``,
    the default) has no memory access at all for that scope. There is no
    single global switch; access is evaluated per ``MemoryScopeKind`` via
    ``scope()``/``is_enabled()``.
    """

    model_config = ConfigDict(extra="forbid")

    scopes: tuple[MemoryScopeBinding, ...] = Field(default_factory=tuple)

    def scope(self, kind: MemoryScopeKind) -> MemoryScopeBinding | None:
        return next((binding for binding in self.scopes if binding.kind == kind), None)

    def is_enabled(self, kind: MemoryScopeKind) -> bool:
        binding = self.scope(kind)
        return binding is not None and binding.enabled


class MemoryEntry(BaseModel):
    """A single application-owned (GA) memory record.

    Persisted and queried by ``(tenant_id, scope_kind, scope_id,
    logical_agent_id)`` for strict tenant/scope isolation. Governance fields
    (opt-in enforcement happens at ``MemoryPolicy``/``MemoryService`` level,
    not here) required by the memory-governance correction:

    * ``ttl_days``/``expires_at`` — retention; ``expires_at`` is computed at
      write time from ``ttl_days`` when set, so expiry is deterministic and
      queryable without re-deriving it from ``created_at`` each read.
    * ``read_acl``/``write_acl`` — principal IDs allowed to read/correct this
      entry beyond the creator; empty means "creator + agent context only".
    * ``provenance`` — where this memory came from (e.g. a conversation turn
      vs. an operator correction), for inspect/audit.
    * ``deleted_at`` — soft-delete marker set by ``forget()``; forgotten
      entries are excluded from ``recall``/``export`` but the audit trail
      (``MemoryAuditRecord``) of the deletion itself is retained.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1, max_length=200)
    scope_kind: MemoryScopeKind
    scope_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str
    role: str = Field(default="note", max_length=40)
    content: str = Field(min_length=1, max_length=20000)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = Field(min_length=1, max_length=200)
    ttl_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    read_acl: tuple[str, ...] = Field(default_factory=tuple)
    write_acl: tuple[str, ...] = Field(default_factory=tuple)
    provenance: str = Field(default="user_conversation", max_length=200)
    deleted_at: datetime | None = None


class MemoryAuditAction(StrEnum):
    REMEMBER = "remember"
    RECALL = "recall"
    INSPECT = "inspect"
    CORRECT = "correct"
    FORGET = "forget"
    EXPORT = "export"


class MemoryAuditRecord(BaseModel):
    """Append-only audit trail for memory inspect/correct/forget/export.

    Every governance action on a ``MemoryEntry`` (not ordinary ``remember``
    writes from normal agent operation, which are already durable as the
    entry itself) is recorded here, independent of the entry's own lifecycle
    — a ``forget`` still leaves an audit record even though the entry content
    itself is no longer recallable.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    entry_id: str
    action: MemoryAuditAction
    actor_id: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    detail: str = ""


# --------------------------------------------------------------------------
# Agent manifest, drafts, immutable versions, lineage
# --------------------------------------------------------------------------


class SchemaRef(BaseModel):
    """A reference to a JSON Schema plus its content digest.

    Mirrors the pattern used for the manifest's own schema
    (``GET /schemas/agent-manifest``): consumers resolve the schema via
    ``ref`` + verify ``digest``, never by importing a Python type. ``ref`` is
    typically a stored schema id/URL; ``inline_schema`` is an optional
    convenience copy for callers that don't want a second round-trip, but
    ``digest`` (computed the same way as the manifest schema digest —
    ``sha256:`` + canonical JSON) is always the source of truth.
    """

    model_config = ConfigDict(frozen=True)

    ref: str = Field(min_length=1, max_length=500)
    digest: str = Field(min_length=1, max_length=200)
    inline_schema: dict[str, Any] | None = None


class KnowledgeBindingKind(StrEnum):
    FILE_SEARCH = "file_search"
    AZURE_AI_SEARCH = "azure_ai_search"
    SHAREPOINT = "sharepoint"


class KnowledgeBinding(BaseModel):
    """A knowledge/grounding source attached to the manifest.

    Distinct from ``CapabilityBinding``: a knowledge binding may be backed by
    a capability operation (in which case ``capability_binding_index`` points
    at the corresponding entry in ``AgentManifest.capabilities``), but the
    manifest keeps knowledge sources separately enumerated so consumers don't
    have to reverse-engineer "which capabilities are actually grounding
    sources" from the general capability list.
    """

    model_config = ConfigDict(extra="forbid")

    kind: KnowledgeBindingKind
    connection_ref: str | None = None
    capability_binding_index: int | None = Field(default=None, ge=0)
    description: str = Field(default="", max_length=2000)


class DelegationScope(StrEnum):
    NONE = "none"
    SPECIALIST_POOL = "specialist_pool"
    ANY_RELEASED_AGENT = "any_released_agent"


class SpecialistPolicy(BaseModel):
    """Specialist/delegation policy: whether/how this agent may delegate to
    other (specialist) agents. Delegation is off by default (``NONE``); an
    explicit, bounded pool (``SPECIALIST_POOL`` + ``allowed_specialist_logical_agent_ids``)
    is the expected shape for anything beyond a single agent.
    """

    model_config = ConfigDict(extra="forbid")

    delegation_scope: DelegationScope = DelegationScope.NONE
    allowed_specialist_logical_agent_ids: tuple[str, ...] = Field(default_factory=tuple)
    max_delegation_depth: int = Field(default=0, ge=0, le=5)


class CitationPolicy(BaseModel):
    """Citation/evidence policy: whether responses must cite evidence and,
    if so, from which declared sources. Advisory at the model-behavior level
    (no gate can verify a model actually cited correctly) but is a
    deterministic, auditable declaration of intent that evaluation/observability
    tooling can check against.
    """

    model_config = ConfigDict(extra="forbid")

    require_citations: bool = False
    allowed_evidence_sources: tuple[str, ...] = Field(default_factory=tuple)


class ArtifactContract(BaseModel):
    """Declares the packaging contract for this agent's output artifacts,
    independent of ``output_schema_ref`` (which describes the *structured
    data shape*; this describes the *delivery kind* — e.g. plain text vs. a
    file attachment — and any size ceiling).
    """

    model_config = ConfigDict(extra="forbid")

    output_kind: str = Field(default="text", max_length=80)
    max_output_bytes: int | None = Field(default=None, ge=1)
    requires_human_review: bool = False


class TemplateProvenance(BaseModel):
    """Lineage/template provenance: which template (if any) this manifest
    was originally generated from, distinct from fork lineage (``LineageEdge``,
    which tracks agent-to-agent forks). A manifest can both be forked from
    another agent *and* have been originally created from a template.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str | None = None
    template_version: str | None = None
    source_url: str | None = None


class AgentManifest(BaseModel):
    """The mutable, editable, runtime-neutral definition of an agent.

    Runtime-neutral means this manifest never encodes a "deploy target"
    choice directly — ``runtime_requirements`` states facts that
    ``select_runtime`` uses to *derive* the target deterministically (see
    ``runtime_selection.py``). Every other cross-cutting concern (I/O
    contract, knowledge, delegation, policy/evaluation references, citation
    policy, artifact contract, lineage/template provenance) is declared here
    so an ``AgentVersion`` cut from this manifest is a complete, self-describing
    release candidate.
    """

    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str = Field(pattern=r"^agent-[a-z0-9-]{3,80}$")
    tenant_id: str = Field(min_length=1, max_length=200)
    #: Workspace/project membership boundary (Phase 2 tenant+workspace scoping
    #: correction). Defaults to ``"default"`` for backward compatibility with
    #: single-project tenants/tests; see ``OwnershipGrant.project_id`` and
    #: ``AgentStudioStore.role_for`` for how this is enforced when supplied.
    project_id: str = Field(default="default", min_length=1, max_length=200)
    schema_version: str = Field(default=AGENT_MANIFEST_SCHEMA_VERSION, min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    #: The agent's actual instructions/system-prompt text. Runtime-neutral:
    #: both Managed Foundry and Custom Hosted runtimes consume the same
    #: ``instructions`` string. Covered by ``manifest_hash`` like every other
    #: manifest field, so any instructions edit is reflected in a new content
    #: hash at cut time.
    instructions: str = Field(default="", max_length=40000)
    owner_kind: AgentOwnerKind
    owner_id: str = Field(min_length=1, max_length=200)
    visibility: AgentVisibility = AgentVisibility.PRIVATE
    capabilities: tuple[CapabilityBinding, ...] = Field(default_factory=tuple)
    runtime_requirements: RuntimeRequirements = Field(default_factory=RuntimeRequirements)
    model_deployment: ModelDeploymentRef | None = None
    input_schema_ref: SchemaRef | None = None
    output_schema_ref: SchemaRef | None = None
    knowledge_bindings: tuple[KnowledgeBinding, ...] = Field(default_factory=tuple)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    specialist_policy: SpecialistPolicy = Field(default_factory=SpecialistPolicy)
    policy_refs: tuple[str, ...] = Field(default_factory=tuple)
    evaluation_suite_refs: tuple[str, ...] = Field(default_factory=tuple)
    citation_policy: CitationPolicy = Field(default_factory=CitationPolicy)
    artifact_contract: ArtifactContract = Field(default_factory=ArtifactContract)
    template_provenance: TemplateProvenance | None = None
    workspace_connections: tuple[str, ...] = Field(default_factory=tuple)
    tags: tuple[str, ...] = Field(default_factory=tuple)


class AgentDraft(BaseModel):
    """A mutable, in-progress edit of a manifest prior to cutting a version.

    ``etag`` changes on every ``save_draft`` (see ``store.py``), giving
    callers optimistic concurrency: a client that read an older ``etag`` and
    submits an update built against stale state can be rejected rather than
    silently clobbering a concurrent edit.
    """

    model_config = ConfigDict(extra="forbid")

    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    manifest: AgentManifest
    updated_by: str
    updated_at: datetime = Field(default_factory=utc_now)
    based_on_version_id: str | None = None
    etag: str = Field(default_factory=lambda: str(uuid4()))


class LineageEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    child_logical_agent_id: str
    child_version_id: str
    parent_logical_agent_id: str
    parent_version_id: str
    relationship: str = Field(default="fork")


class ReleaseArtifactMetadata(BaseModel):
    """Real, non-fabricated build/package metadata recorded at cut time.

    Distinct from ``AgentVersion.sequence`` (a display/ordering integer):
    ``sequence`` is never used to derive a version string here.
    ``package_versions`` is the actual locked dependency version map read
    from the running distribution (see ``release_artifact_metadata.py``);
    ``lock_digest`` is a content hash over that map; ``framework_version``/
    ``hosting_package_version`` are the real installed framework/hosting
    package versions; ``source_revision`` is the source control revision the
    cut was built from (``None`` when genuinely unknown — never fabricated).
    """

    model_config = ConfigDict(frozen=True)

    package_versions: dict[str, str] = Field(default_factory=dict)
    lock_digest: str | None = None
    framework_version: str = Field(default="unknown")
    hosting_package_version: str = Field(default="unknown")
    source_revision: str | None = None


class AgentVersion(BaseModel):
    """An immutable, content-addressed release candidate/record.

    Pure content only — ``AgentVersion`` never carries a mutable draft/
    released/rollback status or a gate-report linkage; that lifecycle state
    lives entirely in append-only ``AgentRelease`` records (below), keyed by
    ``version_id``. Once created, no field on this model is ever mutated;
    ``manifest_hash``/``bundle_uri``/``bundle_checksum`` are frozen forever at
    cut time.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
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
    artifact_metadata: ReleaseArtifactMetadata = Field(default_factory=ReleaseArtifactMetadata)
    protocol_version: str = Field(default=AGENT_STUDIO_PROTOCOL_VERSION)


class ReleaseStatus(StrEnum):
    """Lifecycle status of an ``AgentRelease``.

    There is deliberately no ``DRAFT`` state here: a version with no
    ``AgentRelease`` record at all *is* the "not yet gated" state. Each
    transition below is a new, append-only ``AgentRelease`` row chained via
    ``previous_release_id``, never an in-place mutation of an existing one.
    """

    GATED = "gated"
    APPROVED = "approved"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


class AgentRelease(BaseModel):
    """Append-only lifecycle/governance record for an immutable ``AgentVersion``.

    Distinct from ``AgentVersion`` (pure content) and from ``DeploymentRecord``
    (a concrete development deployment instance): ``AgentRelease`` tracks the
    *governance* lifecycle — gated/approved/active/deprecated/rolled_back —
    for a version within a given ``environment``. Each transition creates a
    new record; ``previous_release_id`` forms a full audit chain so "how did
    this version become active" is always reconstructable.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    version_id: str
    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    status: ReleaseStatus
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT
    gate_report_id: str | None = None
    approval_id: str | None = None
    previous_release_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str
    detail: str = ""


class ResolvedAgentContract(BaseModel):
    """The exact, pinned contract returned by ``/resolve`` (workflow composition).

    A published workflow pins these fields at compose time; execution reads
    them back verbatim and must never silently re-resolve to "whatever is
    latest now" — that would defeat the entire purpose of an immutable,
    exact-version resolution contract. This is also the shape the future
    typed workflow compiler/node palette consumes for capability/IO typing.
    """

    model_config = ConfigDict(frozen=True)

    logical_agent_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    version_id: str
    release_id: str
    release_status: ReleaseStatus
    manifest_hash: str
    runtime_target: RuntimeTarget
    capability_versions: dict[str, str] = Field(default_factory=dict)
    input_schema_ref: SchemaRef | None = None
    output_schema_ref: SchemaRef | None = None
    artifact_metadata: ReleaseArtifactMetadata
    protocol_version: str


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
    APPROVAL = "approval"
    SECURITY = "security"
    SMOKE = "smoke"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    #: This gate does not apply to the version's selected runtime (e.g. the
    #: BUILD gate for a Managed Foundry agent, which has no separate build
    #: step). Distinct from ``SKIPPED`` (evidence was expected but missing):
    #: ``NOT_APPLICABLE`` is a deterministic, runtime-derived fact, never a
    #: caller-chosen bypass, and is treated as passing.
    NOT_APPLICABLE = "not_applicable"


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
        """A release gate report only passes when every gate explicitly passed
        or was correctly determined not to apply to this runtime.

        ``SKIPPED`` (missing evidence) is deliberately treated as non-passing:
        a hard gate can never be silently bypassed by omission. ``NOT_APPLICABLE``
        is different — it is a deterministic runtime fact (e.g. there is no
        synthetic build step for a Managed Foundry agent), not missing evidence.
        """
        return all(result.status in (GateStatus.PASSED, GateStatus.NOT_APPLICABLE) for result in self.results)

    def blocking_gates(self) -> tuple[GateResult, ...]:
        return tuple(
            result for result in self.results if result.status not in (GateStatus.PASSED, GateStatus.NOT_APPLICABLE)
        )


# --------------------------------------------------------------------------
# Approvals and admin escalation
# --------------------------------------------------------------------------


class ApprovalKind(StrEnum):
    RELEASE_PROMOTION = "release_promotion"
    FORK_PROMOTION = "fork_promotion"
    ADMIN_ESCALATION = "admin_escalation"
    CAPABILITY_OPERATION = "capability_operation"


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class StudioApprovalRecord(BaseModel):
    """A behavioral-approval or admin-escalation request/decision.

    ``content_hash``/``environment``/``permissions_policy_ref``/
    ``destination_policy_ref``/``expires_at`` bind the approval to an exact,
    reproducible context: *what* is being approved (content hash), *where*
    (environment), *under what permissions/destination policy*, and *until
    when* it remains valid to act on — an approval is never a blanket,
    open-ended grant.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    version_id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
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
    content_hash: str | None = None
    """The exact version/manifest (or behavioral-contract) content hash this approval is bound to."""
    environment: DeploymentEnvironment | None = None
    permissions_policy_ref: str | None = None
    destination_policy_ref: str | None = None
    expires_at: datetime | None = None


# --------------------------------------------------------------------------
# Development deployments, health, rollback, logical ID resolution
# --------------------------------------------------------------------------
# ``DeploymentEnvironment`` and ``HealthStatus`` are defined earlier (near
# ``OwnershipGrant``) since ``CapabilityInstance`` and ``AgentRelease`` need
# them ahead of this section.


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
    project_id: str = Field(min_length=1, max_length=200)
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
    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    resolved_version_id: str
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str


# --------------------------------------------------------------------------
# Builder Agent: stored proposals (propose -> researcher review -> apply)
# --------------------------------------------------------------------------
# The conversational Builder Agent itself lives outside this codebase (owned
# by the harness's ``agents/**`` surface). It never mutates a draft, attaches
# a connection, approves, or deploys anything directly: it only ever
# produces a *stored proposal* via ``builder_service.BuilderService.propose``.
# A human researcher must explicitly ``apply`` (or ``reject``) it through a
# separate, optimistic-concurrency-guarded endpoint. The client-facing
# request surface for both sides is deliberately opaque -- a free-form
# ``message`` string to propose, a bare ``base_etag`` to apply/reject --
# there is no JSON-patch-shaped input anywhere in this contract, so
# "arbitrary client-authored path patches" are structurally impossible
# rather than merely rejected by validation.


class BuilderProposalState(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    REJECTED = "rejected"


class ManifestFieldChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


class ManifestChangeSummary(BaseModel):
    """One deterministic, top-level ``AgentManifest`` field-level change.

    Computed server-side from the canonical before/after manifest dumps
    (``model_dump(mode="json")``); capability-binding changes are reported
    separately via ``CapabilityChangeSummary`` since they have their own
    natural identity (``descriptor_id``/``operation``) rather than a single
    scalar value.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    kind: ManifestFieldChangeKind
    before: Any | None = None
    after: Any | None = None


class CapabilityChangeKind(StrEnum):
    ATTACHED = "attached"
    DETACHED = "detached"
    RECONFIGURED = "reconfigured"


class CapabilityChangeSummary(BaseModel):
    """One deterministic capability-binding change.

    Keyed by ``(descriptor_id, operation)`` -- the natural identity of a
    ``CapabilityBinding`` within a manifest's ``capabilities`` tuple -- so a
    reconfiguration of an existing binding is reported distinctly from an
    attach/detach.
    """

    model_config = ConfigDict(extra="forbid")

    descriptor_id: str
    operation: str
    kind: CapabilityChangeKind
    before: CapabilityBinding | None = None
    after: CapabilityBinding | None = None


class BuilderProvenance(BaseModel):
    """Where a proposal's content came from: which generator, what request."""

    model_config = ConfigDict(extra="forbid")

    generator: str = Field(min_length=1, max_length=200)
    generator_version: str | None = None
    message: str = Field(min_length=1, max_length=8000)
    requested_by: str = Field(min_length=1, max_length=200)
    requested_at: datetime = Field(default_factory=utc_now)


class BuilderProposal(BaseModel):
    """A stored, reviewable manifest-change proposal from the Builder Agent.

    Immutable content (``before_manifest``/``after_manifest``/diff summaries/
    hashes/provenance) plus a mutable decision envelope (``state``/``decided_*``)
    -- the same request/decision split already used by ``StudioApprovalRecord``.
    Never carries a client-authored patch: ``after_manifest`` is always the
    generator's own typed, canonical ``AgentManifest`` output.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str
    draft_base_etag: str
    """The draft ``etag`` this proposal was generated against. ``apply`` fails
    closed with a concurrency error if the draft has since changed, even if
    the caller's own ``base_etag`` happens to still match the *current*
    draft -- the proposal itself is stale and must be regenerated."""
    before_manifest: AgentManifest
    after_manifest: AgentManifest
    before_manifest_hash: str
    after_manifest_hash: str
    changes: tuple[ManifestChangeSummary, ...] = Field(default_factory=tuple)
    capability_changes: tuple[CapabilityChangeSummary, ...] = Field(default_factory=tuple)
    validation_warnings: tuple[str, ...] = Field(default_factory=tuple)
    source_bundle_ref: str | None = None
    """Content-addressed, immutable reference (see ``artifact_bundle_store``)
    to any generated source/code bundle backing this proposal. Never a raw,
    freely-editable string -- generated code changes are always stored
    immutably and referenced by hash/URI."""
    provenance: BuilderProvenance
    state: BuilderProposalState = BuilderProposalState.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    decided_by: str | None = None
    decided_at: datetime | None = None
    rejection_reason: str | None = None
    applied_draft_etag: str | None = None
    """The new draft ``etag`` minted at apply time (set only once APPLIED)."""


# --------------------------------------------------------------------------
# Platform audit trail (dedicated ``agentStudioAuditV1`` container)
# --------------------------------------------------------------------------


class AuditEventKind(StrEnum):
    """Structured category of a platform-level governance event.

    Distinct from ``MemoryAuditAction``, which audits governance actions on
    an individual ``MemoryEntry`` and stays colocated with memory records in
    ``agentStudioMemoryV1``. ``AuditEvent`` covers cross-cutting platform
    governance: ownership, approvals, deployments, and policy/deletion
    events, independent of any single memory entry's lifecycle.
    """

    OWNERSHIP_GRANTED = "ownership_granted"
    OWNERSHIP_REVOKED = "ownership_revoked"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    APPROVAL_REVOKED = "approval_revoked"
    RELEASE_CUT = "release_cut"
    DEPLOYMENT_ACTIVATED = "deployment_activated"
    DEPLOYMENT_DEPRECATED = "deployment_deprecated"
    DEPLOYMENT_ROLLED_BACK = "deployment_rolled_back"
    POLICY_GATE_FAILED = "policy_gate_failed"
    ARTIFACT_DELETED = "artifact_deleted"


class AuditEvent(BaseModel):
    """A single append-only platform governance event.

    Never mutated or deleted once written (retention/expiry is handled by
    the dedicated ``agentStudioAuditV1`` container's own TTL policy,
    independent from the metadata container). ``detail`` is a small,
    non-secret structured payload (already-JSON-safe primitives only, since
    ``AuditEvent`` itself is persisted verbatim) describing the event; never
    holds credential material.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    logical_agent_id: str | None = None
    kind: AuditEventKind
    actor_id: str = Field(min_length=1, max_length=200)
    subject_id: str
    """The id of the record this event is about (approval id, deployment id,
    release id, ownership-grant key, memory entry id, ...); the specific
    meaning is determined by ``kind``."""
    created_at: datetime = Field(default_factory=utc_now)
    detail: dict[str, str] = Field(default_factory=dict)
