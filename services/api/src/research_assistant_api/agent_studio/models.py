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
    #: Optional workspace/project membership boundary (Phase 2 of the tenant+
    #: workspace scoping correction). ``None`` means "tenant-wide" (the
    #: legacy behavior every existing grant/test relies on); when set, the
    #: grant is only honored for role resolution scoped to that same
    #: ``project_id`` (see ``AgentStudioStore.role_for``). Full per-container
    #: partition scoping is deferred pending a Cosmos partition-key decision.
    project_id: str | None = None


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
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    maturity: OperationMaturity
    operation_class: OperationClass = OperationClass.READ
    side_effect_destinations: tuple[str, ...] = Field(default_factory=tuple)
    requires_approval: bool = False
    reason: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    last_verified_at: datetime | None = None


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
    discovered_provider_version: str | None = None
    readiness: InstanceReadiness = InstanceReadiness.UNAVAILABLE
    health_status: HealthStatus = HealthStatus.UNKNOWN
    config_fingerprint: str | None = None
    unavailable_reason: str | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    registered_by: str = Field(min_length=1, max_length=200)


class CapabilityBinding(BaseModel):
    """An agent's *attachment* of a catalog operation: config + version pins.

    Distinct from ``CapabilityDescriptor`` (the catalog/governance entry),
    ``CapabilityInstance`` (the discovered tenant/project resource), and
    ``ToolRegistration`` (the runtime handler wiring below): ``CapabilityBinding``
    only records that this manifest has chosen to use ``descriptor_id.operation``
    at ``descriptor_version``/``pinned_provider_version``/``schema_digest``,
    pointed at a specific ``instance_id`` (when the operation requires a
    discovered resource) via ``connection_ref``, with what config and policy.
    """

    model_config = ConfigDict(extra="forbid")

    descriptor_id: str = Field(min_length=1, max_length=160)
    descriptor_version: str = Field(default="1", min_length=1, max_length=40)
    operation: str = Field(min_length=1, max_length=120)
    instance_id: str | None = None
    pinned_provider_version: str | None = None
    schema_digest: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    connection_ref: str | None = None
    policy_ref: str | None = None
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
    manifest: AgentManifest
    updated_by: str
    updated_at: datetime = Field(default_factory=utc_now)
    based_on_version_id: str | None = None
    etag: str = Field(default_factory=lambda: str(uuid4()))


class LineageEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    child_logical_agent_id: str
    child_version_id: str
    parent_logical_agent_id: str
    parent_version_id: str
    relationship: str = Field(default="fork")


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
    environment: DeploymentEnvironment
    version_id: str
    release_id: str
    release_status: ReleaseStatus
    manifest_hash: str
    runtime_target: RuntimeTarget
    capability_versions: dict[str, str] = Field(default_factory=dict)
    input_schema_ref: SchemaRef | None = None
    output_schema_ref: SchemaRef | None = None
    package_version: str
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
