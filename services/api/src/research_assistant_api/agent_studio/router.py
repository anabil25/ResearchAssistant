"""FastAPI router for the Agent Studio platform.

Mounted by ``app.py`` via ``app.include_router(agent_studio_router)``. Every
route resolves the caller's ``IdentityContext`` the same way the rest of the
API does (``identity.resolve_identity``) and derives the caller's
per-agent ``AgentRole`` from the store's ownership grants — no route ever
trusts a client-supplied role or tenant ID.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status

from research_assistant_api.agent_studio.approval_consumption import (
    ApprovalConsumptionPort,
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
)
from research_assistant_api.agent_studio.approval_context import (
    ApprovalContextRequest,
    ApprovalContextResolver,
    ApprovalContextResult,
)
from research_assistant_api.agent_studio.approvals import (
    ApprovalError,
    compute_approval_effective_state,
    revoke_approval,
)
from research_assistant_api.agent_studio.audit_service import AuditService
from research_assistant_api.agent_studio.authz import (
    ClaimsGroupMembershipResolver,
    DemoSandboxMembershipPolicy,
    MembershipCheckRequest,
    ProjectMembershipError,
    ProjectMembershipResolver,
    enforce_project_membership,
)
from research_assistant_api.agent_studio.builder_service import (
    BuilderConcurrencyError,
    BuilderNotFoundError,
    BuilderService,
    BuilderServiceError,
    BuilderUnavailableError,
)
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityAttachmentError,
    CapabilityRegistry,
)
from research_assistant_api.agent_studio.deployment_service import (
    DeploymentService,
    DeploymentServiceError,
)
from research_assistant_api.agent_studio.evaluation_runner import EvaluationRunner, EvaluationRunnerError
from research_assistant_api.agent_studio.idempotency import (
    IdempotencyPort,
    IdempotencyResultMismatchError,
)
from research_assistant_api.agent_studio.memory_service import (
    MemoryAccessError,
    MemoryPolicyError,
    MemoryService,
)
from research_assistant_api.agent_studio.model_discovery import ModelDiscovery, ModelDiscoveryError
from research_assistant_api.agent_studio.models import (
    AGENT_MANIFEST_SCHEMA_VERSION,
    AgentDraft,
    AgentDraftView,
    AgentListResponse,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentSummary,
    AgentTemplate,
    AgentVersion,
    AgentWorkspaceView,
    ApprovalKind,
    ApprovalRecordView,
    AuditEventKind,
    BuilderProposal,
    CapabilityBinding,
    CapabilityBindingView,
    CapabilityDescriptor,
    CapabilityInstance,
    DeploymentEnvironment,
    DeploymentObservabilitySummary,
    DeploymentRecord,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    IdempotencyClaim,
    IdempotencyKey,
    IdempotencyRecord,
    MemoryAuditRecord,
    MemoryEntry,
    MemoryScopeKind,
    ModelDeploymentRef,
    PlaygroundRunStatus,
    PlaygroundTestRun,
    ReleaseAttestation,
    ReleaseGateReport,
    ResolvedAgentContract,
    StudioApprovalRecord,
    TemplateListResponse,
    TemplateReadiness,
    ToolRegistrationSpec,
    role_at_least,
    utc_now,
)
from research_assistant_api.agent_studio.observability_provider import (
    ObservabilityProvider,
    ObservabilityProviderError,
)
from research_assistant_api.agent_studio.playground_invoker import PlaygroundInvocationError, PlaygroundInvoker
from research_assistant_api.agent_studio.release_attestation import (
    ReleaseAttestationOutcome,
    ReleaseAttestationPort,
    ReleaseAttestationRequest,
)
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    DraftConflictError,
    ReleaseService,
    ReleaseServiceError,
    resolve_actor_role,
)
from research_assistant_api.agent_studio.schemas import (
    ActivationRequest,
    ApprovalDecisionRequest,
    AttachCapabilityRequest,
    BuilderApplyRequest,
    BuilderMessageRequest,
    BuilderRejectRequest,
    CapabilityApprovalRequest,
    CapabilityDiscoverySnapshot,
    ClaimIdempotencyRequest,
    CompleteIdempotencyRequest,
    ConsumeCapabilityApprovalRequest,
    CorrectMemoryRequest,
    CreateAgentRequest,
    CreateEvaluationRunRequest,
    CreateEvaluationSuiteRequest,
    CreateTestRunRequest,
    DeployRequest,
    EscalationRequest,
    FailIdempotencyRequest,
    ForgetMemoryRequest,
    ForkRequest,
    HealthUpdateRequest,
    IdempotencyKeyFields,
    MarkIdempotencyInProgressRequest,
    PromotionRequest,
    RegisterToolRequest,
    RememberRequest,
    ResolveApprovalContextRequest,
    RevokeApprovalRequest,
    RollbackRequest,
    RunGatesRequest,
    UpdateDraftRequest,
)
from research_assistant_api.agent_studio.scope import PLATFORM_PROJECT_ID, ScopeContext
from research_assistant_api.agent_studio.store import (
    AgentStudioStore,
    IdempotencyConcurrencyError,
    IdempotencyNotFoundError,
)
from research_assistant_api.agent_studio.template_catalog import TemplateCatalog
from research_assistant_api.config import Settings
from research_assistant_api.identity import (
    DEMO_SANDBOX_SOURCE,
    IdentityContext,
    resolve_identity,
)

PLATFORM_OWNER_GROUPS = {"research-admins", "agent-studio-admins"}

#: Default ``ProjectMembershipResolver`` used whenever the composed app
#: hasn't wired an adapter onto ``app.state.agent_studio_membership_resolver``.
#: See ``agent_studio.authz`` for why this is a Protocol-backed seam rather
#: than a direct ``identity.groups`` check.
_DEFAULT_MEMBERSHIP_RESOLVER = ClaimsGroupMembershipResolver()

#: The single, explicit, named local/test-only membership policy for the
#: demo sandbox identity -- see ``DemoSandboxMembershipPolicy`` for why this
#: exists instead of an ad hoc ``if identity.source == ...`` skip in
#: ``_scope``. It is never influenced by (and never influences) whatever
#: ``ProjectMembershipResolver`` the application composes for real
#: identities via ``_membership_resolver``.
_DEMO_SANDBOX_MEMBERSHIP_POLICY = DemoSandboxMembershipPolicy()

router = APIRouter(prefix="/api/agent-studio", tags=["agent-studio"])


def _identity(request: Request) -> IdentityContext:
    settings = cast(Settings, request.app.state.settings)
    return resolve_identity(request, settings)


def _store(request: Request) -> AgentStudioStore:
    store = request.app.state.agent_studio_store
    if store is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(AgentStudioStore, store)


def _registry(request: Request) -> CapabilityRegistry:
    return cast(CapabilityRegistry, request.app.state.agent_studio_registry)


def _release_service(request: Request) -> ReleaseService:
    service = request.app.state.agent_studio_release_service
    if service is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(ReleaseService, service)


def _deployment_service(request: Request) -> DeploymentService:
    service = request.app.state.agent_studio_deployment_service
    if service is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(DeploymentService, service)


def _model_discovery(request: Request) -> ModelDiscovery:
    return cast(ModelDiscovery, request.app.state.agent_studio_model_discovery)


def _template_catalog(request: Request) -> TemplateCatalog:
    return cast(TemplateCatalog, request.app.state.agent_studio_template_catalog)


def _evaluation_runner(request: Request) -> EvaluationRunner:
    return cast(EvaluationRunner, request.app.state.agent_studio_evaluation_runner)


def _playground_invoker(request: Request) -> PlaygroundInvoker:
    return cast(PlaygroundInvoker, request.app.state.agent_studio_playground_invoker)


def _observability_provider(request: Request) -> ObservabilityProvider:
    return cast(ObservabilityProvider, request.app.state.agent_studio_observability_provider)


def _memory_service(request: Request) -> MemoryService:
    service = request.app.state.agent_studio_memory_service
    if service is None:
        raise _unavailable("Agent Studio memory persistence is unavailable (no Cosmos DB configured).")
    return cast(MemoryService, service)


def _builder_service(request: Request) -> BuilderService:
    service = request.app.state.agent_studio_builder_service
    if service is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(BuilderService, service)


def _approval_consumption_port(request: Request) -> ApprovalConsumptionPort:
    """Resolve the app-composed durable approval-consumption adapter.

    Defaults to ``StoreBackedApprovalConsumptionPort`` (backed by this
    package's own ``AgentStudioStore``) wired at composition root; a future
    runtime/provider adapter may wrap this (e.g. to additionally confirm
    the actual tool execution succeeded before durably recording
    consumption) via ``app.state.agent_studio_approval_consumption_port``
    without any router change. Unlike the membership resolver, there is no
    in-process fallback here: consuming an approval durably requires the
    same Cosmos-backed persistence as every other write in this package, so
    a missing port means the same "metadata persistence unavailable" 503 as
    ``_store``/``_release_service``.
    """
    port = getattr(request.app.state, "agent_studio_approval_consumption_port", None)
    if port is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(ApprovalConsumptionPort, port)


def _idempotency_port(request: Request) -> IdempotencyPort:
    """Resolve the app-composed durable idempotency adapter.

    Defaults to ``StoreBackedIdempotencyPort`` (backed by this package's
    own ``AgentStudioStore``) wired at composition root -- there is no
    external provider dependency for a backend-owned idempotency ledger,
    so a missing port here means the same "metadata persistence
    unavailable" 503 as ``_store``/``_approval_consumption_port``.
    """
    port = getattr(request.app.state, "agent_studio_idempotency_port", None)
    if port is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(IdempotencyPort, port)


def _approval_context_resolver(request: Request) -> ApprovalContextResolver:
    """Resolve the app-composed ``ApprovalContextResolver`` adapter.

    Defaults to ``StoreBackedApprovalContextResolver`` (backed by this
    package's own ``AgentStudioStore``) wired at composition root -- there
    is no external provider dependency for resolving which of this
    package's own approval records currently authorizes a binding, so a
    missing port here means the same "metadata persistence unavailable" 503
    as ``_store``/``_approval_consumption_port``.
    """
    resolver = getattr(request.app.state, "agent_studio_approval_context_resolver", None)
    if resolver is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(ApprovalContextResolver, resolver)


def _release_attestation_port(request: Request) -> ReleaseAttestationPort:
    """Resolve the app-composed ``ReleaseAttestationPort`` adapter.

    Defaults to ``StoreBackedReleaseAttestationPort`` wired at composition
    root; a missing port here means the same "metadata persistence
    unavailable" 503 as every other Agent Studio persistence-backed read.
    """
    port = getattr(request.app.state, "agent_studio_release_attestation_port", None)
    if port is None:
        raise _unavailable("Agent Studio metadata persistence is unavailable (no Cosmos DB configured).")
    return cast(ReleaseAttestationPort, port)


def _audit_service(request: Request) -> AuditService:
    """Resolve the app-composed ``AuditService``.

    Every consequential platform mutation route (draft/version/release/
    deploy/health/rollback/approval/revocation/ownership/capability/tool
    registration/artifact/builder-apply) resolves this *before* invoking
    its domain service, so a missing audit store fails the whole request
    closed (503) rather than mutating durable state with no audit trail.
    Memory mutations are audited separately via ``MemoryAuditAction`` (see
    ``audit_service`` module docstring), not through this service.
    """
    service = getattr(request.app.state, "agent_studio_audit_service", None)
    if service is None:
        raise _unavailable("Agent Studio audit persistence is unavailable (no Cosmos DB configured).")
    return cast(AuditService, service)


def _audit(
    request: Request,
    *,
    scope: ScopeContext,
    kind: AuditEventKind,
    actor_id: str,
    subject_id: str,
    logical_agent_id: str | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    """Record one append-only ``AuditEvent`` for a completed mutation.

    Called only after the underlying mutation has already durably
    succeeded. If the write itself raises (e.g. a transient Cosmos error),
    that exception propagates as an unhandled 500 -- the mutation is not
    rolled back (there is no distributed transaction across the metadata
    and audit containers), but the caller sees a failure rather than a
    silently-unaudited success. This is the documented fail-closed policy
    for this package: an audit failure surfaces as a request failure.

    ``detail`` values are coerced to ``str`` (``AuditEvent.detail`` is a
    ``dict[str, str]``): callers may pass ints/bools/enums directly for a
    concise call site without needing to stringify every field themselves.
    """
    _audit_service(request).record(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        kind=kind,
        actor_id=actor_id,
        subject_id=subject_id,
        logical_agent_id=logical_agent_id,
        detail={key: str(value) for key, value in detail.items()} if detail else None,
    )


def _is_platform_owner(identity: IdentityContext) -> bool:
    return bool(PLATFORM_OWNER_GROUPS.intersection(identity.groups))


def _membership_resolver(request: Request) -> ProjectMembershipResolver:
    """Resolve the app-composed ``ProjectMembershipResolver`` adapter.

    Falls back to the default claims-based adapter when the application
    hasn't wired one onto ``app.state`` (e.g. minimal test apps that only
    mount this router) -- this keeps the fail-closed claims behavior as the
    baseline while still letting real deployments inject a Graph/app-role
    membership adapter without any router change.
    """
    resolver = getattr(request.app.state, "agent_studio_membership_resolver", None)
    if resolver is None:
        return _DEFAULT_MEMBERSHIP_RESOLVER
    return cast(ProjectMembershipResolver, resolver)


def _reject_client_supplied_platform_project_id(project_id: str, owner_kind: AgentOwnerKind) -> None:
    """Reserve ``PLATFORM_PROJECT_ID`` for server-assigned system-agent scope.

    ``PLATFORM_PROJECT_ID`` exists so system-owned agents have a concrete,
    total partition (never a "project-less" document); it is not a
    substitute for real per-project scope and must never let a client
    deposit a non-system-owned draft/version/release/binding into that
    shared, platform-wide partition. ``create_agent`` already computes
    ``PLATFORM_PROJECT_ID`` itself for ``owner_kind == SYSTEM`` rather than
    trusting client input for that case, so this only needs to reject the
    reserved id when the *client-supplied* ``project_id`` would otherwise
    be used verbatim for a non-system-owned resource -- which is exactly
    every other caller of this helper.
    """
    if project_id == PLATFORM_PROJECT_ID and owner_kind is not AgentOwnerKind.SYSTEM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"project_id {PLATFORM_PROJECT_ID!r} is reserved for platform-owned system agents "
                f"and cannot be requested for a {owner_kind.value}-owned agent."
            ),
        )


def _scope(request: Request, identity: IdentityContext, project_id: str) -> ScopeContext:
    if project_id == PLATFORM_PROJECT_ID:
        if not _is_platform_owner(identity):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform owners may access platform-scoped resources.",
            )
    else:
        membership_request = MembershipCheckRequest(
            tenant_id=identity.tenant_id,
            project_id=project_id,
            principal_id=identity.user_id,
            claimed_groups=identity.groups,
            # ``groups_overage`` (provider-reported truncation) is the only
            # signal that flips this to "not known-complete" today; a future
            # non-claims adapter (e.g. Graph) would ignore this field
            # entirely and resolve membership out-of-band instead.
            groups_known_complete=not identity.groups_overage,
        )
        # The demo sandbox identity always routes to its own explicit,
        # named local/test-only policy (see ``DemoSandboxMembershipPolicy``)
        # -- never to the application-composed resolver used for real
        # identities. This still goes through the same
        # ``enforce_project_membership`` seam (unlike a bare
        # ``if identity.source == DEMO_SANDBOX_SOURCE: skip`` short-circuit)
        # so the "any project is reachable" behavior is a single,
        # unit-testable symbol rather than logic embedded here.
        resolver = (
            _DEMO_SANDBOX_MEMBERSHIP_POLICY
            if identity.source == DEMO_SANDBOX_SOURCE
            else _membership_resolver(request)
        )
        try:
            enforce_project_membership(resolver, membership_request)
        except ProjectMembershipError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return ScopeContext(tenant_id=identity.tenant_id, project_id=project_id)


def _actor_role(request: Request, identity: IdentityContext, logical_agent_id: str, project_id: str) -> AgentRole:
    return resolve_actor_role(
        _store(request),
        tenant_id=identity.tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        principal_id=identity.user_id,
    )


def _resolve_approval_logical_agent_id(
    store: AgentStudioStore, scope: ScopeContext, record: StudioApprovalRecord
) -> str:
    """Resolve the logical agent an approval record is "about", for role checks.

    Admin escalation records reuse ``version_id`` as the logical_agent_id
    (there is no version yet to escalate against); promotion and
    capability-operation records carry a real ``version_id`` that must be
    dereferenced to find the agent.
    """
    if record.kind is ApprovalKind.ADMIN_ESCALATION:
        return record.version_id
    version = store.get_version(scope, record.version_id)
    if version is None:
        raise _not_found(f"Version '{record.version_id}' was not found.")
    return version.logical_agent_id


def _approval_record_view(
    store: AgentStudioStore, scope: ScopeContext, record: StudioApprovalRecord
) -> ApprovalRecordView:
    revocations = store.list_revocations(scope, record.id)
    effective_state = compute_approval_effective_state(record, revoked=bool(revocations))
    return ApprovalRecordView(record=record, effective_state=effective_state, revocations=revocations)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@router.get("/capabilities/descriptors", response_model=list[CapabilityDescriptor])
def list_capability_descriptors(request: Request) -> list[CapabilityDescriptor]:
    """Honest capability catalog: GA operations are attachable; preview/unavailable
    operations remain visible with their ``reason`` rather than being hidden.

    Separate canonical resource from ``/capabilities/instances`` -- descriptors
    are immutable provider-wide catalog/governance semantics, never tenant-
    or project-scoped resource state.
    """
    _identity(request)
    return list(_registry(request).catalog())


@router.get("/capabilities/instances", response_model=list[CapabilityInstance])
def list_capability_instances(request: Request, project_id: str | None = None) -> list[CapabilityInstance]:
    """Discovered, tenant/project-scoped capability resources.

    Separate canonical resource from ``/capabilities/descriptors``: these are
    the concrete, discovered things a ``CapabilityBinding`` points at via
    ``instance_id``, always isolated to the caller's tenant (and, when
    ``project_id`` is supplied, that project too).
    """
    identity = _identity(request)
    if project_id is not None:
        _scope(request, identity, project_id)
    # Intentionally preserved: registry discovery can aggregate a caller's
    # instances across all of their projects when no project_id is supplied.
    return list(_registry(request).instances_for(tenant_id=identity.tenant_id, project_id=project_id))


@router.get("/capabilities/discovery", response_model=CapabilityDiscoverySnapshot)
def get_capability_discovery(request: Request, project_id: str | None = None) -> CapabilityDiscoverySnapshot:
    """Combined descriptor/instance discovery snapshot for UI/compiler convenience.

    Never a separate canonical resource -- just an aggregate read-time
    projection over ``/capabilities/descriptors`` and ``/capabilities/
    instances``, plus honest, non-fatal discovery ``warnings`` and the
    ``refreshed_at`` timestamp of the underlying registry's last discovery
    pass.
    """
    identity = _identity(request)
    if project_id is not None:
        _scope(request, identity, project_id)
    registry = _registry(request)
    return CapabilityDiscoverySnapshot(
        descriptors=registry.catalog(),
        instances=registry.instances_for(tenant_id=identity.tenant_id, project_id=project_id),
        warnings=registry.warnings,
        refreshed_at=registry.refreshed_at,
        available=registry.available,
        unavailable_reason=registry.unavailable_reason,
    )


@router.get("/models", response_model=list[ModelDeploymentRef])
def list_deployed_models(request: Request) -> list[ModelDeploymentRef]:
    _identity(request)
    try:
        return list(_model_discovery(request).list_deployed_models())
    except ModelDiscoveryError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/capabilities/attach", response_model=CapabilityBinding)
def attach_capability(request: Request, payload: AttachCapabilityRequest) -> CapabilityBinding:
    """Attach a capability operation, enforcing GA-only maturity.

    Rejects preview/unavailable operations with an honest reason rather than
    silently succeeding; the resulting ``CapabilityBinding`` is returned for
    the caller to merge into a draft manifest via ``PUT .../draft``. When
    ``instance_id`` is supplied it must reference a registered, non-unavailable
    ``CapabilityInstance`` for this descriptor.
    """
    identity = _identity(request)
    try:
        return _registry(request).attach(
            descriptor_id=payload.descriptor_id,
            operation=payload.operation,
            attached_by=identity.user_id,
            instance_id=payload.instance_id,
            connection_ref=payload.connection_ref,
            policy_ref=payload.policy_ref,
            config=payload.config,
        )
    except CapabilityAttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/schemas/agent-manifest")
def get_agent_manifest_schema(request: Request) -> dict[str, object]:
    """Canonical JSON Schema + content digest for the persisted ``AgentManifest``.

    External consumers (e.g. the harness) resolve the manifest contract from
    this endpoint's JSON Schema and digest rather than importing this
    codebase's Python model class.
    """
    _identity(request)
    schema = AgentManifest.model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema_version": AGENT_MANIFEST_SCHEMA_VERSION,
        "digest": f"sha256:{digest}",
        "json_schema": schema,
    }


@router.post("/agents", response_model=AgentDraft, status_code=status.HTTP_201_CREATED)
def create_agent(request: Request, payload: CreateAgentRequest) -> AgentDraft:
    identity = _identity(request)
    _reject_client_supplied_platform_project_id(payload.project_id, payload.owner_kind)
    scope = _scope(
        request,
        identity,
        PLATFORM_PROJECT_ID if payload.owner_kind is AgentOwnerKind.SYSTEM else payload.project_id,
    )
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        draft = _release_service(request).create_agent(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=payload.logical_agent_id,
            display_name=payload.display_name,
            description=payload.description,
            owner_kind=payload.owner_kind,
            owner_id=payload.owner_id or identity.user_id,
            requested_by=identity.user_id,
            is_platform_owner=_is_platform_owner(identity),
            visibility=payload.visibility,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DRAFT_CREATED,
        actor_id=identity.user_id,
        subject_id=draft.logical_agent_id,
        logical_agent_id=draft.logical_agent_id,
        detail={"owner_kind": payload.owner_kind.value, "owner_id": draft.manifest.owner_id},
    )
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.OWNERSHIP_GRANTED,
        actor_id=identity.user_id,
        subject_id=draft.manifest.owner_id,
        logical_agent_id=draft.logical_agent_id,
        detail={"role": AgentRole.OWNER.value},
    )
    return draft


@router.get("/agents", response_model=AgentListResponse)
def list_agents(
    request: Request,
    project_id: str,
    owner_kind: AgentOwnerKind | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AgentListResponse:
    """Authorized registry listing of agent summaries within one scope.

    Distinct from ``POST /agents`` (create) and from
    ``/agents/{id}/workspace`` (full single-agent aggregate): this is the
    read-side registry surface researchers/platform owners use to browse
    *many* existing agents at once -- draft + latest cut version + latest
    release status -- with optional ``owner_kind``/``q`` (case-insensitive
    display-name substring) filters and ``limit``/``offset`` pagination.

    Scoped identically to every other read in this package: ``project_id``
    selects one ``ScopeContext`` (either a real project or, for a caller
    who is a platform owner, ``PLATFORM_PROJECT_ID`` to browse system-owned
    agents). There is no cross-scope merge -- a caller that wants both
    their project's own agents and the system catalog makes two calls, one
    per scope, matching every other scope-partitioned read here.
    """
    if not 1 <= limit <= 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit must be between 1 and 200."
        )
    if offset < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="offset must be >= 0.")
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    store = _store(request)
    needle = q.strip().lower() if q else None
    summaries: list[AgentSummary] = []
    for draft in store.list_drafts(scope):
        manifest = draft.manifest
        if owner_kind is not None and manifest.owner_kind != owner_kind:
            continue
        if needle and needle not in manifest.display_name.lower():
            continue
        versions = store.list_versions(scope, draft.logical_agent_id)
        latest_version = versions[-1] if versions else None
        latest_release = (
            store.latest_release_for_version(scope, latest_version.id) if latest_version is not None else None
        )
        summaries.append(
            AgentSummary(
                logical_agent_id=draft.logical_agent_id,
                owner_kind=manifest.owner_kind,
                owner_id=manifest.owner_id,
                tenant_id=draft.tenant_id,
                project_id=draft.project_id,
                display_name=manifest.display_name,
                description=manifest.description,
                visibility=manifest.visibility,
                tags=manifest.tags,
                updated_at=draft.updated_at,
                updated_by=draft.updated_by,
                latest_version_id=latest_version.id if latest_version is not None else None,
                latest_version_sequence=latest_version.sequence if latest_version is not None else None,
                latest_release_status=latest_release.status if latest_release is not None else None,
                latest_release_environment=(latest_release.environment if latest_release is not None else None),
                runtime_target=latest_version.runtime_target if latest_version is not None else None,
            )
        )
    # Stable two-pass sort: tie-break ascending by logical_agent_id first,
    # then the dominant sort (most-recently-updated first) is applied
    # stably on top, so equal ``updated_at`` values keep deterministic
    # agent-id ordering rather than depending on dict/list iteration order.
    summaries.sort(key=lambda item: item.logical_agent_id)
    summaries.sort(key=lambda item: item.updated_at, reverse=True)
    total = len(summaries)
    page = summaries[offset : offset + limit]
    return AgentListResponse(items=tuple(page), total=total, limit=limit, offset=offset)


@router.get("/templates", response_model=TemplateListResponse)
def list_templates(
    request: Request,
    category: str | None = None,
    readiness: TemplateReadiness | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TemplateListResponse:
    """Governed task template catalog listing, used by create-from-template.

    Templates are tenant/project-neutral platform reference content (see
    ``template_catalog`` module docstring), so this only requires a valid
    identity -- no ``project_id``/scope -- matching the
    ``/capabilities/descriptors`` catalog-read convention. Readiness is
    never used to hide entries: ``PREVIEW``/``DEPRECATED`` templates remain
    listed with their honest ``readiness`` label unless the caller
    explicitly filters them out via the ``readiness`` query parameter.
    """
    if not 1 <= limit <= 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit must be between 1 and 200."
        )
    if offset < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="offset must be >= 0.")
    _identity(request)
    needle = q.strip().lower() if q else None
    templates = [
        template
        for template in _template_catalog(request).list_templates()
        if (category is None or template.category == category)
        and (readiness is None or template.readiness == readiness)
        and (needle is None or needle in template.display_name.lower())
    ]
    # Deterministic ordering: ascending template_id, then descending version
    # so the newest version of a given template_id sorts first within it.
    templates.sort(key=lambda template: template.version, reverse=True)
    templates.sort(key=lambda template: template.template_id)
    total = len(templates)
    page = templates[offset : offset + limit]
    return TemplateListResponse(items=tuple(page), total=total, limit=limit, offset=offset)


@router.get("/templates/{template_id}", response_model=AgentTemplate)
def get_template(request: Request, template_id: str, version: str | None = None) -> AgentTemplate:
    """Fetch one governed template's full ``seed`` content by id.

    ``version`` pins an exact version; omitted, this returns the
    highest-versioned entry for ``template_id`` (see
    ``StaticTemplateCatalog.get_template``). 404s rather than falling back
    to a different template if the requested id/version pair does not
    exist -- consistent with every other exact-pin lookup in this module.
    """
    _identity(request)
    template = _template_catalog(request).get_template(template_id, version)
    if template is None:
        raise _not_found(f"Template {template_id!r} (version={version!r}) was not found.")
    return template


@router.post(
    "/agents/{logical_agent_id}/evaluation-suites",
    response_model=EvaluationSuite,
    status_code=status.HTTP_201_CREATED,
)
def create_evaluation_suite(
    request: Request, logical_agent_id: str, payload: CreateEvaluationSuiteRequest
) -> EvaluationSuite:
    """Create a durable, reusable advisory ``EvaluationSuite`` for an agent.

    Requires ``CONTRIBUTOR`` or above -- a pure ``VIEWER`` may inspect
    suites/runs but not author new ones. Never gates a release: see
    ``EvaluationSuite``/``EvaluationRun`` docstrings.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    if not role_at_least(role, AgentRole.CONTRIBUTOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{role.value}' does not meet the minimum 'contributor'.",
        )
    suite = EvaluationSuite(
        id=str(uuid4()),
        logical_agent_id=logical_agent_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=payload.name,
        description=payload.description,
        test_cases=payload.test_cases,
        created_by=identity.user_id,
    )
    return _store(request).create_evaluation_suite(scope, suite)


@router.get("/agents/{logical_agent_id}/evaluation-suites", response_model=list[EvaluationSuite])
def list_evaluation_suites(request: Request, logical_agent_id: str, project_id: str) -> list[EvaluationSuite]:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(_store(request).list_evaluation_suites(scope, logical_agent_id))


@router.get("/agents/{logical_agent_id}/evaluation-suites/{suite_id}", response_model=EvaluationSuite)
def get_evaluation_suite(
    request: Request, logical_agent_id: str, suite_id: str, project_id: str
) -> EvaluationSuite:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    suite = _store(request).get_evaluation_suite(scope, suite_id)
    if suite is None or suite.logical_agent_id != logical_agent_id:
        raise _not_found(f"Evaluation suite '{suite_id}' was not found.")
    return suite


@router.post(
    "/agents/{logical_agent_id}/evaluation-suites/{suite_id}/runs",
    response_model=EvaluationRun,
    status_code=status.HTTP_201_CREATED,
)
def create_evaluation_run(
    request: Request,
    logical_agent_id: str,
    suite_id: str,
    payload: CreateEvaluationRunRequest,
) -> EvaluationRun:
    """Trigger one advisory evaluation run of ``suite_id`` against either the
    agent's current draft (``version_id`` omitted) or one exact, immutable
    ``AgentVersion`` (``version_id`` set).

    Honestly fails with 503 when no evaluation execution adapter is wired
    (see ``evaluation_runner.UnavailableEvaluationRunner``) rather than
    persisting or returning a fabricated ``COMPLETED`` run -- no run record
    is created at all in that case, since there is nothing genuine to
    record. Requires ``CONTRIBUTOR`` or above, matching suite creation.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    if not role_at_least(role, AgentRole.CONTRIBUTOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{role.value}' does not meet the minimum 'contributor'.",
        )
    store = _store(request)
    suite = store.get_evaluation_suite(scope, suite_id)
    if suite is None or suite.logical_agent_id != logical_agent_id:
        raise _not_found(f"Evaluation suite '{suite_id}' was not found.")
    if payload.version_id is not None:
        version = store.get_version(scope, payload.version_id)
        if version is None or version.logical_agent_id != logical_agent_id:
            raise _not_found(f"Version '{payload.version_id}' was not found.")
        instructions = version.manifest.instructions
    else:
        draft = store.get_draft(scope, logical_agent_id)
        if draft is None:
            raise _not_found(f"Agent '{logical_agent_id}' was not found.")
        instructions = draft.manifest.instructions
    try:
        results = _evaluation_runner(request).run_suite(suite, instructions=instructions)
    except EvaluationRunnerError as exc:
        raise _unavailable(str(exc)) from exc
    run = EvaluationRun(
        id=str(uuid4()),
        suite_id=suite_id,
        logical_agent_id=logical_agent_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        version_id=payload.version_id,
        status=EvaluationRunStatus.COMPLETED,
        results=results,
        requested_by=identity.user_id,
        completed_at=utc_now(),
    )
    return store.create_evaluation_run(scope, run)


@router.get("/agents/{logical_agent_id}/evaluation-runs", response_model=list[EvaluationRun])
def list_evaluation_runs(
    request: Request, logical_agent_id: str, project_id: str, suite_id: str | None = None
) -> list[EvaluationRun]:
    """History/trends read surface: every past run for an agent, optionally
    filtered to one suite via ``suite_id``. Advisory only -- never consulted
    by a release gate.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(_store(request).list_evaluation_runs(scope, logical_agent_id, suite_id=suite_id))


@router.get("/agents/{logical_agent_id}/evaluation-runs/{run_id}", response_model=EvaluationRun)
def get_evaluation_run(request: Request, logical_agent_id: str, run_id: str, project_id: str) -> EvaluationRun:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    run = _store(request).get_evaluation_run(scope, run_id)
    if run is None or run.logical_agent_id != logical_agent_id:
        raise _not_found(f"Evaluation run '{run_id}' was not found.")
    return run


@router.post(
    "/agents/{logical_agent_id}/test-runs",
    response_model=PlaygroundTestRun,
    status_code=status.HTTP_201_CREATED,
)
def create_test_run(
    request: Request, logical_agent_id: str, payload: CreateTestRunRequest
) -> PlaygroundTestRun:
    """Invoke the agent once with a single typed input for the interactive
    Test/Playground tab, against either the current draft (``version_id``
    omitted) or one exact, immutable ``AgentVersion`` (``version_id`` set).

    Honestly fails with 503 when no playground execution adapter is wired
    (see ``playground_invoker.UnavailablePlaygroundInvoker``) rather than
    persisting or returning a fabricated response -- no run record is
    created at all in that case. Requires ``CONTRIBUTOR`` or above, matching
    evaluation-run creation. Side effects are always the deterministic
    ``SideEffectPolicy.DRY_RUN``.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    if not role_at_least(role, AgentRole.CONTRIBUTOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{role.value}' does not meet the minimum 'contributor'.",
        )
    store = _store(request)
    if payload.version_id is not None:
        version = store.get_version(scope, payload.version_id)
        if version is None or version.logical_agent_id != logical_agent_id:
            raise _not_found(f"Version '{payload.version_id}' was not found.")
        instructions = version.manifest.instructions
    else:
        draft = store.get_draft(scope, logical_agent_id)
        if draft is None:
            raise _not_found(f"Agent '{logical_agent_id}' was not found.")
        instructions = draft.manifest.instructions
    try:
        result = _playground_invoker(request).invoke(instructions=instructions, input_text=payload.input)
    except PlaygroundInvocationError as exc:
        raise _unavailable(str(exc)) from exc
    run = PlaygroundTestRun(
        id=str(uuid4()),
        logical_agent_id=logical_agent_id,
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        version_id=payload.version_id,
        input=payload.input,
        output=result.output,
        status=PlaygroundRunStatus.COMPLETED,
        trace=result.trace,
        tool_calls=result.tool_calls,
        requested_by=identity.user_id,
        completed_at=utc_now(),
    )
    return store.create_test_run(scope, run)


@router.get("/agents/{logical_agent_id}/test-runs", response_model=list[PlaygroundTestRun])
def list_test_runs(
    request: Request, logical_agent_id: str, project_id: str, version_id: str | None = None
) -> list[PlaygroundTestRun]:
    """History read surface: every past playground/test run for an agent,
    optionally filtered to one ``version_id``. Purely diagnostic -- never
    consulted by a release gate.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(_store(request).list_test_runs(scope, logical_agent_id, version_id=version_id))


@router.get("/agents/{logical_agent_id}/test-runs/{run_id}", response_model=PlaygroundTestRun)
def get_test_run(request: Request, logical_agent_id: str, run_id: str, project_id: str) -> PlaygroundTestRun:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    run = _store(request).get_test_run(scope, run_id)
    if run is None or run.logical_agent_id != logical_agent_id:
        raise _not_found(f"Test run '{run_id}' was not found.")
    return run


@router.get("/agents/{logical_agent_id}/draft", response_model=AgentDraftView)
def get_draft(request: Request, logical_agent_id: str, project_id: str) -> AgentDraftView:
    """Raw draft plus a derived, volatile ``capability_views`` sidecar.

    ``draft`` is exactly the persisted ``AgentDraft`` (raw ``CapabilityBinding``
    only, unchanged); ``capability_views`` is a read-time expansion the
    editor uses to show current descriptor/instance resolution and staleness
    without ever being written back into the draft/manifest.
    """
    identity = _identity(request)
    draft = _store(request).get_draft(_scope(request, identity, project_id), logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    capability_views = _registry(request).resolve_binding_views(draft.manifest.capabilities)
    return AgentDraftView(draft=draft, capability_views=capability_views)


@router.put("/agents/{logical_agent_id}/draft", response_model=AgentDraft)
def update_draft(
    request: Request,
    logical_agent_id: str,
    payload: UpdateDraftRequest,
    if_match: str = Header(..., alias="If-Match", description="Expected current draft etag (optimistic concurrency)."),
) -> AgentDraft:
    identity = _identity(request)
    scope = _scope(request, identity, payload.manifest.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        draft = _release_service(request).update_draft(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            manifest=payload.manifest,
            updated_by=identity.user_id,
            actor_role=role,
            expected_etag=if_match,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except DraftConflictError as exc:
        raise HTTPException(status_code=status.HTTP_412_PRECONDITION_FAILED, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DRAFT_UPDATED,
        actor_id=identity.user_id,
        subject_id=logical_agent_id,
        logical_agent_id=logical_agent_id,
        detail={"etag": draft.etag},
    )
    return draft


@router.post("/agents/{logical_agent_id}/fork", response_model=AgentDraft, status_code=status.HTTP_201_CREATED)
def fork_agent(request: Request, logical_agent_id: str, payload: ForkRequest) -> AgentDraft:
    identity = _identity(request)
    # ``release_service.fork`` always assigns ``owner_kind=USER`` to the new
    # draft regardless of the source agent's ownership, so the reserved
    # platform project id can never be a legitimate target here.
    _reject_client_supplied_platform_project_id(payload.project_id, AgentOwnerKind.USER)
    scope = _scope(request, identity, payload.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        draft = _release_service(request).fork(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            source_logical_agent_id=logical_agent_id,
            source_version_id=payload.source_version_id,
            new_logical_agent_id=payload.new_logical_agent_id,
            requested_by=identity.user_id,
        )
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DRAFT_FORKED,
        actor_id=identity.user_id,
        subject_id=payload.new_logical_agent_id,
        logical_agent_id=payload.new_logical_agent_id,
        detail={"source_logical_agent_id": logical_agent_id, "source_version_id": payload.source_version_id},
    )
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.OWNERSHIP_GRANTED,
        actor_id=identity.user_id,
        subject_id=draft.manifest.owner_id,
        logical_agent_id=payload.new_logical_agent_id,
        detail={"role": AgentRole.OWNER.value},
    )
    return draft


@router.post(
    "/agents/{logical_agent_id}/tool-registrations",
    response_model=ToolRegistrationSpec,
    status_code=status.HTTP_201_CREATED,
)
def register_tool(
    request: Request,
    logical_agent_id: str,
    payload: RegisterToolRequest,
) -> ToolRegistrationSpec:
    """Register the runtime handler for a GA capability operation.

    Rejects operations that are not GA-attachable with the same honest
    reason as ``/capabilities/attach`` (never silently registers a handler
    for a preview/unavailable operation).
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        spec = _release_service(request).register_tool(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            descriptor_id=payload.descriptor_id,
            operation=payload.operation,
            kind=payload.kind,
            handler_ref=payload.handler_ref,
            registered_by=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except CapabilityAttachmentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.TOOL_REGISTERED,
        actor_id=identity.user_id,
        subject_id=spec.id,
        logical_agent_id=logical_agent_id,
        detail={"descriptor_id": payload.descriptor_id, "operation": payload.operation, "kind": payload.kind.value},
    )
    return spec


@router.get("/agents/{logical_agent_id}/tool-registrations", response_model=list[ToolRegistrationSpec])
def list_tool_registrations(request: Request, logical_agent_id: str, project_id: str) -> list[ToolRegistrationSpec]:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(_release_service(request).list_tool_registrations(scope.tenant_id, scope.project_id, logical_agent_id))


@router.post(
    "/agents/{logical_agent_id}/versions",
    response_model=AgentVersion,
    status_code=status.HTTP_201_CREATED,
)
def cut_version(request: Request, logical_agent_id: str, project_id: str) -> AgentVersion:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        version = _release_service(request).cut_version(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            actor_id=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.RELEASE_CUT,
        actor_id=identity.user_id,
        subject_id=version.id,
        logical_agent_id=logical_agent_id,
        detail={"sequence": version.sequence, "manifest_hash": version.manifest_hash},
    )
    return version


@router.get("/agents/{logical_agent_id}/versions", response_model=list[AgentVersion])
def list_versions(request: Request, logical_agent_id: str, project_id: str) -> list[AgentVersion]:
    identity = _identity(request)
    return list(_store(request).list_versions(_scope(request, identity, project_id), logical_agent_id))


@router.get("/versions/{version_id}/capability-views", response_model=list[CapabilityBindingView])
def get_version_capability_views(
    request: Request, version_id: str, project_id: str
) -> list[CapabilityBindingView]:
    """Current expanded ``{binding, resolved_descriptor, resolved_instance,
    bindability, stale_reason}`` view for one immutable, already-cut version.

    Distinct from ``/versions/{id}/contract`` (raw binding-only, the actual
    execution contract): this is volatile, read-time presentation data for
    UI/audit and can never be substituted for the raw contract by a
    runtime/compiler.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    version = _store(request).get_version(scope, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    return list(_registry(request).resolve_binding_views(version.manifest.capabilities))


@router.get("/agents/{logical_agent_id}/workspace", response_model=AgentWorkspaceView)
def get_agent_workspace(request: Request, logical_agent_id: str, project_id: str) -> AgentWorkspaceView:
    """Aggregate, volatile view of an agent's current draft/release/deployment state.

    Convenience read-time composition for the UI: current draft (with its
    expanded ``capability_views``), the most recently cut version, that
    version's latest release, and recent deployment history. Never usable as
    an execution contract -- the runtime/compiler always resolves via
    ``/resolve``/``/versions/{id}/contract`` instead.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    store = _store(request)
    draft = store.get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    versions = store.list_versions(scope, logical_agent_id)
    latest_version = versions[-1] if versions else None
    latest_release = (
        store.latest_release_for_version(scope, latest_version.id) if latest_version is not None else None
    )
    deployments = store.list_deployments(scope, logical_agent_id)
    capability_views = _registry(request).resolve_binding_views(draft.manifest.capabilities)
    return AgentWorkspaceView(
        logical_agent_id=logical_agent_id,
        draft=draft,
        latest_version=latest_version,
        latest_release=latest_release,
        deployments=deployments,
        capability_views=capability_views,
    )


@router.get("/agents/{logical_agent_id}/lineage")
def list_lineage(request: Request, logical_agent_id: str, project_id: str) -> list[dict[str, object]]:
    identity = _identity(request)
    edges = _store(request).list_lineage(_scope(request, identity, project_id), logical_agent_id)
    return [edge.model_dump(mode="json") for edge in edges]


@router.post("/versions/{version_id}/gates", response_model=ReleaseGateReport)
def run_gates(request: Request, version_id: str, payload: RunGatesRequest) -> ReleaseGateReport:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    version = _store(request).get_version(scope, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    role = _actor_role(request, identity, version.logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        report = _release_service(request).run_release_gates(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            version_id=version_id,
            actor_id=identity.user_id,
            actor_role=role,
            evidence=payload.evidence,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.GATE_PASSED if report.passed else AuditEventKind.POLICY_GATE_FAILED,
        actor_id=identity.user_id,
        subject_id=report.id,
        logical_agent_id=version.logical_agent_id,
        detail={
            "version_id": version_id,
            "blocking_gates": [result.name.value for result in report.blocking_gates()],
        },
    )
    return report


@router.post("/versions/{version_id}/promote")
def request_promotion(
    request: Request,
    version_id: str,
    payload: PromotionRequest,
) -> StudioApprovalRecord | AgentVersion:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    version = _store(request).get_version(scope, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    role = _actor_role(request, identity, version.logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        result = _release_service(request).request_promotion(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            version_id=version_id,
            actor_id=identity.user_id,
            actor_role=role,
            destination=payload.destination,
            evidence_summary=payload.evidence_summary,
            risk=payload.risk,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.RELEASE_PROMOTION_REQUESTED,
        actor_id=identity.user_id,
        subject_id=result.id,
        logical_agent_id=version.logical_agent_id,
        detail={"version_id": version_id, "destination": payload.destination, "result_type": type(result).__name__},
    )
    return result


@router.post("/versions/{version_id}/activate", response_model=AgentRelease)
def activate_release_route(
    request: Request,
    version_id: str,
    payload: ActivationRequest,
) -> AgentRelease:
    """Explicit ACTIVE transition, gated on a healthy deploy+smoke record.

    Never triggered implicitly by ``/promote``, ``/deployments`` (deploy),
    or ``/deployments/{id}/health`` (record_health) — the caller must ask
    for activation explicitly, and it is rejected unless the exact
    version/environment already has a ``DeploymentRecord`` with a healthy
    smoke result.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    version = _store(request).get_version(scope, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    role = _actor_role(request, identity, version.logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        release = _release_service(request).activate_release(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            version_id=version_id,
            actor_id=identity.user_id,
            actor_role=role,
            environment=payload.environment,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.RELEASE_ACTIVATED,
        actor_id=identity.user_id,
        subject_id=release.id,
        logical_agent_id=version.logical_agent_id,
        detail={"version_id": version_id, "environment": payload.environment.value},
    )
    return release


@router.post("/versions/{version_id}/capability-approvals")
def request_capability_approval(
    request: Request,
    version_id: str,
    payload: CapabilityApprovalRequest,
) -> StudioApprovalRecord:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    version = _store(request).get_version(scope, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        record = _release_service(request).request_capability_approval(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            version_id=version_id,
            descriptor_id=payload.descriptor_id,
            operation=payload.operation,
            actor_id=identity.user_id,
            actor_role=_actor_role(request, identity, version.logical_agent_id, scope.project_id),
            evidence_summary=payload.evidence_summary,
            risk=payload.risk,
            permissions_policy_ref=payload.permissions_policy_ref,
            destination_policy_ref=payload.destination_policy_ref,
        )
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.APPROVAL_REQUESTED,
        actor_id=identity.user_id,
        subject_id=record.id,
        logical_agent_id=version.logical_agent_id,
        detail={"kind": record.kind.value, "descriptor_id": payload.descriptor_id, "operation": payload.operation},
    )
    return record


@router.post("/approvals/{approval_id}/decision", response_model=StudioApprovalRecord)
def decide_approval_route(
    request: Request,
    approval_id: str,
    payload: ApprovalDecisionRequest,
) -> StudioApprovalRecord:
    identity = _identity(request)
    service = _release_service(request)
    store = _store(request)
    scope = _scope(request, identity, payload.project_id)
    record = store.get_approval(scope, approval_id)
    if record is None:
        raise _not_found(f"Approval '{approval_id}' was not found.")
    if store.list_revocations(scope, approval_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Approval '{approval_id}' has been revoked and can no longer be decided.",
        )
    logical_agent_id = _resolve_approval_logical_agent_id(store, scope, record)
    approver_role = (
        AgentRole.OWNER
        if _is_platform_owner(identity)
        else _actor_role(request, identity, logical_agent_id, scope.project_id)
    )
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        if record.kind is ApprovalKind.ADMIN_ESCALATION:
            decided = service.decide_role_escalation(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                approval_id=approval_id,
                approver_id=identity.user_id,
                approver_role=approver_role,
                approve=payload.approve,
                rationale=payload.rationale,
            )
        elif record.kind is ApprovalKind.CAPABILITY_OPERATION:
            decided = service.decide_capability_approval(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                approval_id=approval_id,
                approver_id=identity.user_id,
                approver_role=approver_role,
                approve=payload.approve,
                rationale=payload.rationale,
            )
        else:
            decided = service.decide_promotion(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                approval_id=approval_id,
                approver_id=identity.user_id,
                approver_role=approver_role,
                approve=payload.approve,
                rationale=payload.rationale,
            )
    except (ReleaseServiceError, ApprovalError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id=identity.user_id,
        subject_id=decided.id,
        logical_agent_id=logical_agent_id,
        detail={"kind": decided.kind.value, "approved": payload.approve, "state": decided.state.value},
    )
    if (
        decided.kind is ApprovalKind.ADMIN_ESCALATION
        and payload.approve
        and decided.requested_role is not None
    ):
        _audit(
            request,
            scope=scope,
            kind=AuditEventKind.OWNERSHIP_GRANTED,
            actor_id=identity.user_id,
            subject_id=decided.requested_by,
            logical_agent_id=logical_agent_id,
            detail={"role": decided.requested_role.value, "via_approval_id": decided.id},
        )
    return decided


@router.get("/approvals/{approval_id}", response_model=ApprovalRecordView)
def get_approval_route(request: Request, approval_id: str, project_id: str) -> ApprovalRecordView:
    """UI/audit read of one approval: record + recomputed ``effective_state`` + revocations."""
    identity = _identity(request)
    store = _store(request)
    scope = _scope(request, identity, project_id)
    record = store.get_approval(scope, approval_id)
    if record is None:
        raise _not_found(f"Approval '{approval_id}' was not found.")
    # Resolving the logical agent enforces that the approval actually belongs
    # to this scope's agent graph before any data is returned.
    _resolve_approval_logical_agent_id(store, scope, record)
    return _approval_record_view(store, scope, record)


@router.post("/approvals/{approval_id}/revoke", response_model=ApprovalRecordView)
def revoke_approval_route(
    request: Request, approval_id: str, payload: RevokeApprovalRequest
) -> ApprovalRecordView:
    """Append an ``ApprovalRevocation``. Permanent, append-only; no un-revoke.

    Self-revocation (the original requester revoking their own already-
    decided request) is always allowed; any other actor must meet the same
    minimum role required to have decided the approval in the first place,
    or be a platform owner.
    """
    identity = _identity(request)
    store = _store(request)
    scope = _scope(request, identity, payload.project_id)
    record = store.get_approval(scope, approval_id)
    if record is None:
        raise _not_found(f"Approval '{approval_id}' was not found.")
    logical_agent_id = _resolve_approval_logical_agent_id(store, scope, record)
    actor_role = (
        AgentRole.OWNER
        if _is_platform_owner(identity)
        else _actor_role(request, identity, logical_agent_id, scope.project_id)
    )
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        revocation = revoke_approval(
            record,
            revocation_id=f"rev-{uuid4().hex}",
            actor_id=identity.user_id,
            actor_role=actor_role,
            is_platform_owner=_is_platform_owner(identity),
            reason=payload.reason,
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    store.create_revocation(scope, revocation)
    revocations = store.list_revocations(scope, approval_id)
    effective_state = compute_approval_effective_state(record, revoked=bool(revocations))
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.APPROVAL_REVOKED,
        actor_id=identity.user_id,
        subject_id=approval_id,
        logical_agent_id=logical_agent_id,
        detail={"reason": payload.reason},
    )
    return ApprovalRecordView(record=record, effective_state=effective_state, revocations=revocations)


@router.post("/approvals/{approval_id}/consume", response_model=ApprovalConsumptionResult)
async def consume_approval_route(
    request: Request,
    approval_id: str,
    payload: ConsumeCapabilityApprovalRequest,
) -> ApprovalConsumptionResult:
    """Durably, atomically spend a ``CAPABILITY_OPERATION`` approval at
    actual runtime invocation.

    This is the *only* backend path by which a runtime invocation can turn
    a decided approval into a spent, one-time authorization: the hosted
    caller supplies nothing but the decision reference (``approval_id``,
    from the path) and the concrete facts of this specific invocation
    (binding/operation/instance/args/destination/policy/release/
    idempotency) -- never a boolean claim of "this is approved". The acting
    ``principal_id`` is always the authenticated caller's own identity,
    never client-supplied. Every identifying field is independently
    revalidated against the approval's own pinned version/binding by
    ``ApprovalConsumptionPort`` before anything is durably recorded, so a
    request naming the right ``approval_id`` cannot be used to spend it
    against a different binding/operation/instance than what was actually
    approved. Fails closed (``DENIED``) rather than raising an error for
    every "not currently authorized" case; only scope/existence failures
    raise HTTP errors.
    """
    identity = _identity(request)
    store = _store(request)
    scope = _scope(request, identity, payload.project_id)
    record = store.get_approval(scope, approval_id)
    if record is None:
        raise _not_found(f"Approval '{approval_id}' was not found.")
    # Resolving the logical agent enforces that the approval actually
    # belongs to this scope's agent graph before any consumption is
    # attempted -- the same boundary ``get_approval_route`` enforces for
    # reads.
    logical_agent_id = _resolve_approval_logical_agent_id(store, scope, record)
    port = _approval_consumption_port(request)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    consumption_request = ApprovalConsumptionRequest(
        scope=scope,
        approval_id=approval_id,
        principal_id=identity.user_id,
        binding_id=payload.binding_id,
        instance_fingerprint=payload.instance_fingerprint,
        operation_id=payload.operation_id,
        operation_version=payload.operation_version,
        args_hash=payload.args_hash,
        destination_hash=payload.destination_hash,
        policy_ref=payload.policy_ref,
        release_id=payload.release_id,
        invocation_id=payload.invocation_id,
        idempotency_key=payload.idempotency_key,
    )
    result = await port.consume_approval(consumption_request)
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.APPROVAL_CONSUMED,
        actor_id=identity.user_id,
        subject_id=approval_id,
        logical_agent_id=logical_agent_id,
        detail={
            "outcome": result.outcome.value,
            "operation_id": payload.operation_id,
            "binding_id": payload.binding_id,
            "invocation_id": payload.invocation_id,
        },
    )
    return result


@router.post("/approvals/context", response_model=ApprovalContextResult)
async def resolve_approval_context_route(
    request: Request,
    payload: ResolveApprovalContextRequest,
) -> ApprovalContextResult:
    """Resolve the trusted ``approval_id``/``invocation_id`` a runtime caller
    must pass to ``POST /approvals/{approval_id}/consume``.

    The caller supplies only the plan facts it already knows on its own
    (release/binding/operation) -- never an ``approval_id`` or
    ``invocation_id``, both of which are always resolved/minted server-side
    from this release's own currently-effectively-approved
    ``CAPABILITY_OPERATION`` approval for the exact destination. This closes
    the "API never supplies trusted approval_id/invocation_id" gap without
    creating any new trust dependency: ``consume_approval_route`` still
    independently revalidates everything regardless of what this route
    returned. Read-only; performs no durable write and grants no authority
    by itself.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    resolver = _approval_context_resolver(request)
    context_request = ApprovalContextRequest(
        scope=scope,
        release_id=payload.release_id,
        binding_id=payload.binding_id,
        operation_id=payload.operation_id,
    )
    return await resolver.resolve_context(context_request)


@router.get("/releases/{release_id}/attestation", response_model=ReleaseAttestation)
async def get_release_attestation_route(
    request: Request, release_id: str, project_id: str
) -> ReleaseAttestation:
    """Return a signed, objective ``ReleaseAttestation`` for one release.

    Derived read-only from the release's own immutable ``ReleaseGateReport``
    (never re-run, never influenced by advisory evaluations); intended for
    harness/runtime startup to verify hard release gates passed before
    trusting a release. Raises 404 if the release does not exist in this
    scope, or has never had release gates run against it.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    port = _release_attestation_port(request)
    result = await port.get_attestation(ReleaseAttestationRequest(scope=scope, release_id=release_id))
    if result.outcome is ReleaseAttestationOutcome.NOT_FOUND:
        raise _not_found(result.reason or f"Release '{release_id}' has no attestation available.")
    assert result.attestation is not None  # guaranteed by ATTESTED outcome
    return result.attestation


@router.post(
    "/agents/{logical_agent_id}/escalations",
    response_model=StudioApprovalRecord,
    status_code=status.HTTP_201_CREATED,
)
def request_escalation(
    request: Request,
    logical_agent_id: str,
    payload: EscalationRequest,
) -> StudioApprovalRecord:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    record = _release_service(request).request_role_escalation(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        logical_agent_id=logical_agent_id,
        requested_by=identity.user_id,
        requested_role=payload.requested_role,
        evidence_summary=payload.evidence_summary,
        risk=payload.risk,
    )
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.APPROVAL_REQUESTED,
        actor_id=identity.user_id,
        subject_id=record.id,
        logical_agent_id=logical_agent_id,
        detail={"kind": record.kind.value, "requested_role": payload.requested_role.value},
    )
    return record


@router.post(
    "/agents/{logical_agent_id}/deployments",
    response_model=DeploymentRecord,
    status_code=status.HTTP_201_CREATED,
)
def deploy(request: Request, logical_agent_id: str, payload: DeployRequest) -> DeploymentRecord:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        record = _deployment_service(request).deploy(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            version_id=payload.version_id,
            deployed_by=identity.user_id,
            actor_role=role,
            trace_ref=payload.trace_ref,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DEPLOYMENT_CREATED,
        actor_id=identity.user_id,
        subject_id=record.id,
        logical_agent_id=logical_agent_id,
        detail={"version_id": payload.version_id, "environment": record.environment.value},
    )
    return record


@router.get("/agents/{logical_agent_id}/deployments", response_model=list[DeploymentRecord])
def list_deployments(request: Request, logical_agent_id: str, project_id: str) -> list[DeploymentRecord]:
    identity = _identity(request)
    return list(_store(request).list_deployments(_scope(request, identity, project_id), logical_agent_id))


@router.get(
    "/agents/{logical_agent_id}/deployments/{deployment_id}/observability",
    response_model=DeploymentObservabilitySummary,
)
def get_deployment_observability(
    request: Request,
    logical_agent_id: str,
    deployment_id: str,
    project_id: str,
    window_hours: int = 24,
) -> DeploymentObservabilitySummary:
    """Redacted health/invocation/trace/cost Monitor-tab read view.

    Distinct from ``GET .../deployments`` (which only ever exposes the
    single point-in-time ``DeploymentHealth``/``trace_ref`` recorded via
    ``POST /deployments/{id}/health``): this aggregates invocation count,
    error rate, p50/p95 latency, per-tool counters, and opaque trace
    correlation links over a caller-chosen window, sourced from
    Application Insights via ``ObservabilityProvider`` (see that module's
    docstring). Honestly fails with 503 when no Application Insights
    resource is configured (``UnavailableObservabilityProvider``) rather
    than returning fabricated metrics. Read-only: requires only project
    membership, matching every other GET route in this package.
    """
    if not 1 <= window_hours <= 24 * 30:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_hours must be between 1 and 720.",
        )
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    deployment = _store(request).get_deployment(scope, deployment_id)
    if deployment is None or deployment.logical_agent_id != logical_agent_id:
        raise _not_found(f"Deployment '{deployment_id}' was not found.")
    try:
        return _observability_provider(request).get_deployment_summary(
            deployment, window=timedelta(hours=window_hours)
        )
    except ObservabilityProviderError as exc:
        raise _unavailable(str(exc)) from exc


@router.post("/deployments/{deployment_id}/health", response_model=DeploymentRecord)
def record_health(request: Request, deployment_id: str, payload: HealthUpdateRequest) -> DeploymentRecord:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    deployment = _store(request).get_deployment(scope, deployment_id)
    if deployment is None:
        raise _not_found(f"Deployment '{deployment_id}' not found.")
    role = _actor_role(request, identity, deployment.logical_agent_id, scope.project_id)
    if not role_at_least(role, AgentRole.MAINTAINER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{role.value}' cannot record deployment health.",
        )
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    record = _deployment_service(request).record_health(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        deployment_id=deployment_id,
        actor_role=role,
        status=payload.status,
        detail=payload.detail,
        trace_ref=payload.trace_ref,
    )
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DEPLOYMENT_HEALTH_RECORDED,
        actor_id=identity.user_id,
        subject_id=deployment_id,
        logical_agent_id=record.logical_agent_id,
        detail={"status": payload.status.value},
    )
    return record


@router.post(
    "/agents/{logical_agent_id}/rollback",
    response_model=DeploymentRecord,
    status_code=status.HTTP_201_CREATED,
)
def rollback(request: Request, logical_agent_id: str, payload: RollbackRequest) -> DeploymentRecord:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        record = _deployment_service(request).rollback(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            deployment_id=payload.deployment_id,
            target_version_id=payload.target_version_id,
            deployed_by=identity.user_id,
            actor_role=role,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK,
        actor_id=identity.user_id,
        subject_id=record.id,
        logical_agent_id=logical_agent_id,
        detail={"from_deployment_id": payload.deployment_id, "target_version_id": payload.target_version_id},
    )
    return record


@router.get("/agents/{logical_agent_id}/resolve", response_model=ResolvedAgentContract)
def resolve_logical_agent(
    request: Request,
    logical_agent_id: str,
    project_id: str,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> ResolvedAgentContract:
    """Composition/resolution contract for the future typed workflow compiler.

    Resolves a stable logical agent ID (within this tenant/workspace and the
    requested ``environment``) to the exact pinned version/release, its
    manifest hash, runtime endpoint, typed I/O schema refs, and capability
    versions. A published workflow must pin this response's ``version_id``/
    ``release_id``/``manifest_hash`` at compose time; execution must never
    silently re-resolve to "whatever is latest" later.

    Fails closed: if the pinned release's capability bindings have gone
    stale since cut (descriptor/instance/schema/destination drift), this
    raises 409 rather than returning a contract that would no longer pass
    gate-time validation.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    try:
        contract = _deployment_service(request).resolve(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            environment=environment,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if contract is None:
        raise _not_found(
            f"Agent '{logical_agent_id}' has no resolved, released contract for environment "
            f"'{environment.value}'."
        )
    return contract


@router.get("/versions/{version_id}/contract", response_model=ResolvedAgentContract)
def get_exact_version_contract(
    request: Request,
    version_id: str,
    project_id: str,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> ResolvedAgentContract:
    """Exact-version contract lookup for the future node palette/compiler.

    Unlike ``/resolve`` (which follows the *current* environment binding for
    a logical agent), this looks up one already-known ``version_id``
    directly - for re-validating a previously composed/pinned workflow node
    without depending on whatever is currently bound to an environment.

    Fails closed on stale capability bindings, same as ``/resolve``.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    try:
        contract = _deployment_service(request).contract_for_version(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            version_id=version_id,
            environment=environment,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if contract is None:
        raise _not_found(f"Version '{version_id}' has no released contract.")
    return contract


@router.get("/catalog", response_model=list[ResolvedAgentContract])
def get_released_agent_catalog(
    request: Request,
    project_id: str,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> list[ResolvedAgentContract]:
    """Released-agent catalog for the future node palette/compiler.

    Lists the exact, pinned contract currently bound to ``environment`` for
    every logical agent this tenant owns a draft for. Agents with no
    environment binding yet are omitted (there is nothing to pin).
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(
        _deployment_service(request).catalog(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            environment=environment,
        )
    )


@router.post(
    "/agents/{logical_agent_id}/memory",
    response_model=MemoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def remember(request: Request, logical_agent_id: str, payload: RememberRequest) -> MemoryEntry:
    """Append a GA-mechanism memory entry (conversation/user/project/private-agent scope).

    Rejects non-GA memory mechanisms (e.g. the Foundry native "Memory"
    preview feature) rather than silently accepting them. Memory is opt-in
    per the agent's ``MemoryPolicy`` and persistent storage defaults off;
    entries are governed by TTL, an explicit read/write ACL, and a
    ``REMEMBER`` provenance audit record.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    entry = MemoryEntry(
        id=str(uuid4()),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        scope_kind=payload.scope_kind,
        scope_id=payload.scope_id,
        logical_agent_id=logical_agent_id,
        role=payload.role,
        content=payload.content,
        created_by=identity.user_id,
        ttl_days=payload.ttl_days,
        read_acl=payload.read_acl,
        write_acl=payload.write_acl,
    )
    try:
        return _memory_service(request).remember(draft.manifest, entry)
    except MemoryPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory", response_model=list[MemoryEntry])
def recall(
    request: Request,
    logical_agent_id: str,
    project_id: str,
    scope_kind: MemoryScopeKind,
    scope_id: str,
    limit: int = 100,
) -> list[MemoryEntry]:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return list(
            _memory_service(request).recall(
                draft.manifest,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                actor_id=identity.user_id,
                limit=limit,
            )
        )
    except MemoryPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory/{entry_id}", response_model=MemoryEntry)
def inspect_memory_entry(request: Request, logical_agent_id: str, entry_id: str, project_id: str) -> MemoryEntry:
    """Inspect a single memory entry (GA-mechanism memory governance: inspect)."""
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).inspect(
            draft.manifest,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            entry_id=entry_id,
            actor_id=identity.user_id,
        )
    except MemoryAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MemoryPolicyError as exc:
        raise _not_found(str(exc)) from exc


@router.put("/agents/{logical_agent_id}/memory/{entry_id}", response_model=MemoryEntry)
def correct_memory_entry(
    request: Request,
    logical_agent_id: str,
    entry_id: str,
    payload: CorrectMemoryRequest,
) -> MemoryEntry:
    """Correct a memory entry's content (GA-mechanism memory governance: correct)."""
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).correct(
            draft.manifest,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            entry_id=entry_id,
            actor_id=identity.user_id,
            content=payload.content,
        )
    except MemoryAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MemoryPolicyError as exc:
        raise _not_found(str(exc)) from exc


@router.delete("/agents/{logical_agent_id}/memory/{entry_id}", response_model=MemoryEntry)
def forget_memory_entry(
    request: Request,
    logical_agent_id: str,
    entry_id: str,
    payload: ForgetMemoryRequest,
) -> MemoryEntry:
    """Forget (deletion-audited soft removal of) a memory entry: GA-mechanism memory governance."""
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).forget(
            draft.manifest,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            entry_id=entry_id,
            actor_id=identity.user_id,
            reason=payload.reason,
        )
    except MemoryAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MemoryPolicyError as exc:
        raise _not_found(str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory-export", response_model=list[MemoryEntry])
def export_memory(
    request: Request,
    logical_agent_id: str,
    project_id: str,
    scope_kind: MemoryScopeKind,
    scope_id: str,
) -> list[MemoryEntry]:
    """Export all readable memory entries for a scope (GA-mechanism memory governance: export)."""
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return list(
            _memory_service(request).export(
                draft.manifest,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                actor_id=identity.user_id,
            )
        )
    except MemoryPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory/{entry_id}/audit", response_model=list[MemoryAuditRecord])
def memory_audit_trail(
    request: Request,
    logical_agent_id: str,
    entry_id: str,
    project_id: str,
) -> list[MemoryAuditRecord]:
    """Deletion/provenance audit trail for a single memory entry.

    Requires read/inspect ACL on the concrete entry (creator or ``read_acl``
    member) and the enclosing scope's ``allow_user_inspect`` control; entries
    are resolved through the caller's own logical agent so no cross-agent or
    cross-scope audit history can be enumerated.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    draft = _store(request).get_draft(scope, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return list(
            _memory_service(request).audit_trail(
                draft.manifest,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                entry_id=entry_id,
                actor_id=identity.user_id,
            )
        )
    except MemoryAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except MemoryPolicyError as exc:
        raise _not_found(str(exc)) from exc


# -- Builder Agent: stored proposals (propose -> researcher review -> apply) --
#
# The Builder Agent never mutates a draft, authorizes, attaches connections,
# approves, or deploys anything through these routes. ``/builder/messages``
# only ever produces a stored ``BuilderProposal``; applying/rejecting it is a
# separate, explicit, optimistic-concurrency-guarded researcher action. There
# is no patch-shaped request body anywhere below.


def _builder_error_response(exc: BuilderServiceError) -> HTTPException:
    if isinstance(exc, BuilderNotFoundError):
        return _not_found(str(exc))
    if isinstance(exc, BuilderConcurrencyError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, BuilderUnavailableError):
        return _unavailable(str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post(
    "/agents/{logical_agent_id}/builder/messages",
    response_model=BuilderProposal,
    status_code=status.HTTP_201_CREATED,
)
def create_builder_proposal(
    request: Request,
    logical_agent_id: str,
    payload: BuilderMessageRequest,
) -> BuilderProposal:
    """Produce a stored manifest-change proposal from a natural-language message.

    Never mutates the draft directly. The request body is a free-form
    ``message`` and a ``base_etag`` acknowledgement only -- never a patch.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    try:
        return _builder_service(request).propose(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            message=payload.message,
            base_etag=payload.base_etag,
            requested_by=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BuilderServiceError as exc:
        raise _builder_error_response(exc) from exc


@router.get("/agents/{logical_agent_id}/proposals", response_model=list[BuilderProposal])
def list_builder_proposals(request: Request, logical_agent_id: str, project_id: str) -> list[BuilderProposal]:
    """Proposal history for an agent (pending, applied, and rejected)."""
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    return list(_builder_service(request).list_proposals(scope.tenant_id, scope.project_id, logical_agent_id))


@router.get("/agents/{logical_agent_id}/proposals/{proposal_id}", response_model=BuilderProposal)
def get_builder_proposal(request: Request, logical_agent_id: str, proposal_id: str, project_id: str) -> BuilderProposal:
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    proposal = _builder_service(request).get_proposal(scope.tenant_id, scope.project_id, logical_agent_id, proposal_id)
    if proposal is None:
        raise _not_found(f"Proposal '{proposal_id}' was not found.")
    return proposal


@router.post("/agents/{logical_agent_id}/proposals/{proposal_id}/apply", response_model=AgentDraft)
def apply_builder_proposal(
    request: Request,
    logical_agent_id: str,
    proposal_id: str,
    payload: BuilderApplyRequest,
) -> AgentDraft:
    """Apply a stored proposal after researcher review.

    Never accepts a patch body: only a ``base_etag`` acknowledgement. Fails
    closed with a 409 if the draft changed since the proposal was generated,
    or since the caller last read it.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    _audit_service(request)  # fail closed before mutating if audit is unavailable
    try:
        draft = _builder_service(request).apply(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            proposal_id=proposal_id,
            base_etag=payload.base_etag,
            applied_by=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BuilderServiceError as exc:
        raise _builder_error_response(exc) from exc
    _audit(
        request,
        scope=scope,
        kind=AuditEventKind.BUILDER_PROPOSAL_APPLIED,
        actor_id=identity.user_id,
        subject_id=proposal_id,
        logical_agent_id=logical_agent_id,
        detail={"draft_etag": draft.etag},
    )
    return draft


@router.post("/agents/{logical_agent_id}/proposals/{proposal_id}/reject", response_model=BuilderProposal)
def reject_builder_proposal(
    request: Request,
    logical_agent_id: str,
    proposal_id: str,
    payload: BuilderRejectRequest,
) -> BuilderProposal:
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    role = _actor_role(request, identity, logical_agent_id, scope.project_id)
    try:
        return _builder_service(request).reject(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            logical_agent_id=logical_agent_id,
            proposal_id=proposal_id,
            rejected_by=identity.user_id,
            reason=payload.reason,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except BuilderServiceError as exc:
        raise _builder_error_response(exc) from exc


# --------------------------------------------------------------------------
# Durable idempotency (runtime double-execution / retry protection)
# --------------------------------------------------------------------------
# Every route below constructs ``IdempotencyKey`` server-side from the
# authenticated, membership-checked ``ScopeContext`` -- the client body never
# supplies ``tenant_id`` directly (see ``IdempotencyKeyFields``), exactly
# like every other scoped mutation in this router. ``actor_id`` is always
# ``identity.user_id``, never a client-supplied field.


def _idempotency_key(scope: ScopeContext, payload: IdempotencyKeyFields) -> IdempotencyKey:
    return IdempotencyKey(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        binding_digest=payload.binding_digest,
        operation_id=payload.operation_id,
        destination=payload.destination,
        caller_key=payload.caller_key,
        argument_hash=payload.argument_hash,
    )


@router.post("/idempotency/claim", response_model=IdempotencyClaim)
async def claim_idempotency_route(request: Request, payload: ClaimIdempotencyRequest) -> IdempotencyClaim:
    """Atomically claim (or observe the existing disposition of) a durable
    idempotency key before a runtime handler executes a possibly
    side-effecting operation.

    Never raises on an already-claimed/completed key -- the returned
    ``disposition`` (``ACQUIRED``/``IN_PROGRESS``/``COMPLETED``/
    ``RECONCILIATION_REQUIRED``) tells the caller exactly how to proceed;
    only a malformed ``lease_seconds`` value is rejected as a client error.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    key = _idempotency_key(scope, payload)
    try:
        return await _idempotency_port(request).claim(
            scope,
            key,
            actor_id=identity.user_id,
            release_id=payload.release_id,
            lease_seconds=payload.lease_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.post("/idempotency/mark-in-progress", response_model=IdempotencyRecord)
async def mark_idempotency_in_progress_route(
    request: Request, payload: MarkIdempotencyInProgressRequest
) -> IdempotencyRecord:
    """Transition a durably ``ACQUIRED`` claim to ``IN_PROGRESS`` immediately
    before the handler performs its (possibly irreversible) side effect.

    Requires the exact ``claim_token``/``expected_version`` pair returned by
    ``claim`` -- a stale or wrong pair is rejected as a 409 concurrency
    conflict rather than silently reapplied, since another instance may
    already have taken over this key.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    key = _idempotency_key(scope, payload)
    try:
        return await _idempotency_port(request).mark_in_progress(
            scope,
            key,
            claim_token=payload.claim_token,
            expected_version=payload.expected_version,
            irreversible=payload.irreversible,
        )
    except IdempotencyNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except IdempotencyConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/idempotency/complete", response_model=IdempotencyRecord)
async def complete_idempotency_route(request: Request, payload: CompleteIdempotencyRequest) -> IdempotencyRecord:
    """Durably record a successful completion and its result.

    The durably stored ``result_hash`` is always independently recomputed
    from ``result`` by the port itself; ``expected_result_hash`` (if
    supplied) is only checked as a caller-side sanity assertion and never
    trusted as the value to persist.
    """
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    key = _idempotency_key(scope, payload)
    try:
        return await _idempotency_port(request).complete(
            scope,
            key,
            claim_token=payload.claim_token,
            expected_version=payload.expected_version,
            result=payload.result,
            expected_result_hash=payload.expected_result_hash,
        )
    except IdempotencyResultMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except IdempotencyConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/idempotency/fail", response_model=IdempotencyRecord)
async def fail_idempotency_route(request: Request, payload: FailIdempotencyRequest) -> IdempotencyRecord:
    """Durably record that this attempt failed and requires reconciliation
    (the true side-effect outcome of the attempt is unknown and must never
    be silently retried as if it were fresh)."""
    identity = _identity(request)
    scope = _scope(request, identity, payload.project_id)
    key = _idempotency_key(scope, payload)
    try:
        return await _idempotency_port(request).fail(
            scope,
            key,
            claim_token=payload.claim_token,
            expected_version=payload.expected_version,
            failure_code=payload.failure_code,
        )
    except IdempotencyNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except IdempotencyConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/idempotency/results/{result_ref}")
async def load_idempotency_result_route(
    request: Request, result_ref: str, project_id: str
) -> dict[str, object] | None:
    """Replay a previously completed result.

    Always partitioned by the caller's *own authorized* scope -- never by
    anything decoded from ``result_ref`` -- so a caller cannot enumerate
    another tenant/project's results even by guessing a valid ``result_ref``
    string; an unauthorized/foreign scope simply misses (``None``/404),
    identical to a genuinely unknown ``result_ref``.
    """
    identity = _identity(request)
    scope = _scope(request, identity, project_id)
    result = await _idempotency_port(request).load_result(scope, result_ref)
    if result is None:
        raise _not_found(f"Idempotency result '{result_ref}' was not found.")
    return result
