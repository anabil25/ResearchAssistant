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
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status

from research_assistant_api.agent_studio.approvals import ApprovalError
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
from research_assistant_api.agent_studio.memory_service import (
    MemoryAccessError,
    MemoryPolicyError,
    MemoryService,
)
from research_assistant_api.agent_studio.model_discovery import ModelDiscovery, ModelDiscoveryError
from research_assistant_api.agent_studio.models import (
    AGENT_MANIFEST_SCHEMA_VERSION,
    AgentDraft,
    AgentManifest,
    AgentRole,
    AgentVersion,
    ApprovalKind,
    BuilderProposal,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityInstance,
    DeploymentEnvironment,
    DeploymentRecord,
    MemoryAuditRecord,
    MemoryEntry,
    MemoryScopeKind,
    ModelDeploymentRef,
    ReleaseGateReport,
    ResolvedAgentContract,
    StudioApprovalRecord,
    ToolRegistration,
)
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    ReleaseService,
    ReleaseServiceError,
    resolve_actor_role,
)
from research_assistant_api.agent_studio.schemas import (
    ApprovalDecisionRequest,
    AttachCapabilityRequest,
    BuilderApplyRequest,
    BuilderMessageRequest,
    BuilderRejectRequest,
    CapabilityDiscoverySnapshot,
    CorrectMemoryRequest,
    CreateAgentRequest,
    DeployRequest,
    EscalationRequest,
    ForgetMemoryRequest,
    ForkRequest,
    HealthUpdateRequest,
    PromotionRequest,
    RegisterToolRequest,
    RememberRequest,
    RollbackRequest,
    RunGatesRequest,
    UpdateDraftRequest,
)
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext, resolve_identity

PLATFORM_OWNER_GROUPS = {"research-admins", "agent-studio-admins"}

router = APIRouter(prefix="/v1/agent-studio", tags=["agent-studio"])


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


def _is_platform_owner(identity: IdentityContext) -> bool:
    return bool(PLATFORM_OWNER_GROUPS.intersection(identity.groups))


def _actor_role(request: Request, identity: IdentityContext, logical_agent_id: str) -> AgentRole:
    return resolve_actor_role(
        _store(request),
        tenant_id=identity.tenant_id,
        logical_agent_id=logical_agent_id,
        principal_id=identity.user_id,
    )


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
    registry = _registry(request)
    return CapabilityDiscoverySnapshot(
        descriptors=registry.catalog(),
        instances=registry.instances_for(tenant_id=identity.tenant_id, project_id=project_id),
        warnings=registry.warnings,
        refreshed_at=registry.refreshed_at,
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
    try:
        return _release_service(request).create_agent(
            tenant_id=identity.tenant_id,
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


@router.get("/agents/{logical_agent_id}/draft", response_model=AgentDraft)
def get_draft(request: Request, logical_agent_id: str) -> AgentDraft:
    identity = _identity(request)
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    return draft


@router.put("/agents/{logical_agent_id}/draft", response_model=AgentDraft)
def update_draft(request: Request, logical_agent_id: str, payload: UpdateDraftRequest) -> AgentDraft:
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _release_service(request).update_draft(
            tenant_id=identity.tenant_id,
            logical_agent_id=logical_agent_id,
            manifest=payload.manifest,
            updated_by=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/agents/{logical_agent_id}/fork", response_model=AgentDraft, status_code=status.HTTP_201_CREATED)
def fork_agent(request: Request, logical_agent_id: str, payload: ForkRequest) -> AgentDraft:
    identity = _identity(request)
    try:
        return _release_service(request).fork(
            tenant_id=identity.tenant_id,
            source_logical_agent_id=logical_agent_id,
            source_version_id=payload.source_version_id,
            new_logical_agent_id=payload.new_logical_agent_id,
            requested_by=identity.user_id,
        )
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/agents/{logical_agent_id}/tool-registrations",
    response_model=ToolRegistration,
    status_code=status.HTTP_201_CREATED,
)
def register_tool(
    request: Request,
    logical_agent_id: str,
    payload: RegisterToolRequest,
) -> ToolRegistration:
    """Register the runtime handler for a GA capability operation.

    Rejects operations that are not GA-attachable with the same honest
    reason as ``/capabilities/attach`` (never silently registers a handler
    for a preview/unavailable operation).
    """
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _release_service(request).register_tool(
            tenant_id=identity.tenant_id,
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


@router.get("/agents/{logical_agent_id}/tool-registrations", response_model=list[ToolRegistration])
def list_tool_registrations(request: Request, logical_agent_id: str) -> list[ToolRegistration]:
    identity = _identity(request)
    return list(_release_service(request).list_tool_registrations(identity.tenant_id, logical_agent_id))


@router.post(
    "/agents/{logical_agent_id}/versions",
    response_model=AgentVersion,
    status_code=status.HTTP_201_CREATED,
)
def cut_version(request: Request, logical_agent_id: str) -> AgentVersion:
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _release_service(request).cut_version(
            tenant_id=identity.tenant_id,
            logical_agent_id=logical_agent_id,
            actor_id=identity.user_id,
            actor_role=role,
        )
    except AuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/versions", response_model=list[AgentVersion])
def list_versions(request: Request, logical_agent_id: str) -> list[AgentVersion]:
    identity = _identity(request)
    return list(_store(request).list_versions(identity.tenant_id, logical_agent_id))


@router.get("/agents/{logical_agent_id}/lineage")
def list_lineage(request: Request, logical_agent_id: str) -> list[dict[str, object]]:
    identity = _identity(request)
    edges = _store(request).list_lineage(identity.tenant_id, logical_agent_id)
    return [edge.model_dump(mode="json") for edge in edges]


@router.post("/versions/{version_id}/gates", response_model=ReleaseGateReport)
def run_gates(request: Request, version_id: str, payload: RunGatesRequest) -> ReleaseGateReport:
    identity = _identity(request)
    try:
        return _release_service(request).run_release_gates(
            tenant_id=identity.tenant_id,
            version_id=version_id,
            evidence=payload.evidence,
        )
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/versions/{version_id}/promote")
def request_promotion(
    request: Request,
    version_id: str,
    payload: PromotionRequest,
) -> StudioApprovalRecord | AgentVersion:
    identity = _identity(request)
    version = _store(request).get_version(identity.tenant_id, version_id)
    if version is None:
        raise _not_found(f"Version '{version_id}' was not found.")
    role = _actor_role(request, identity, version.logical_agent_id)
    try:
        return _release_service(request).request_promotion(
            tenant_id=identity.tenant_id,
            version_id=version_id,
            actor_id=identity.user_id,
            actor_role=role,
            destination=payload.destination,
            evidence_summary=payload.evidence_summary,
            risk=payload.risk,
        )
    except ReleaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/approvals/{approval_id}/decision", response_model=StudioApprovalRecord)
def decide_approval_route(
    request: Request,
    approval_id: str,
    payload: ApprovalDecisionRequest,
) -> StudioApprovalRecord:
    identity = _identity(request)
    service = _release_service(request)
    store = _store(request)
    record = store.get_approval(identity.tenant_id, approval_id)
    if record is None:
        raise _not_found(f"Approval '{approval_id}' was not found.")
    # Admin escalation records reuse ``version_id`` as the logical_agent_id
    # (there is no version yet to escalate against); promotion records carry
    # a real ``version_id`` that must be dereferenced to find the agent.
    if record.kind is ApprovalKind.ADMIN_ESCALATION:
        logical_agent_id = record.version_id
    else:
        version = store.get_version(identity.tenant_id, record.version_id)
        if version is None:
            raise _not_found(f"Version '{record.version_id}' was not found.")
        logical_agent_id = version.logical_agent_id
    approver_role = (
        AgentRole.OWNER
        if _is_platform_owner(identity)
        else _actor_role(request, identity, logical_agent_id)
    )
    try:
        if record.kind is ApprovalKind.ADMIN_ESCALATION:
            return service.decide_role_escalation(
                tenant_id=identity.tenant_id,
                approval_id=approval_id,
                approver_id=identity.user_id,
                approver_role=approver_role,
                approve=payload.approve,
                rationale=payload.rationale,
            )
        return service.decide_promotion(
            tenant_id=identity.tenant_id,
            approval_id=approval_id,
            approver_id=identity.user_id,
            approver_role=approver_role,
            approve=payload.approve,
            rationale=payload.rationale,
        )
    except (ReleaseServiceError, ApprovalError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
    return _release_service(request).request_role_escalation(
        tenant_id=identity.tenant_id,
        logical_agent_id=logical_agent_id,
        requested_by=identity.user_id,
        requested_role=payload.requested_role,
        evidence_summary=payload.evidence_summary,
        risk=payload.risk,
    )


@router.post(
    "/agents/{logical_agent_id}/deployments",
    response_model=DeploymentRecord,
    status_code=status.HTTP_201_CREATED,
)
def deploy(request: Request, logical_agent_id: str, payload: DeployRequest) -> DeploymentRecord:
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _deployment_service(request).deploy(
            tenant_id=identity.tenant_id,
            logical_agent_id=logical_agent_id,
            version_id=payload.version_id,
            deployed_by=identity.user_id,
            actor_role=role,
            trace_ref=payload.trace_ref,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/deployments", response_model=list[DeploymentRecord])
def list_deployments(request: Request, logical_agent_id: str) -> list[DeploymentRecord]:
    identity = _identity(request)
    return list(_store(request).list_deployments(identity.tenant_id, logical_agent_id))


@router.post("/deployments/{deployment_id}/health", response_model=DeploymentRecord)
def record_health(request: Request, deployment_id: str, payload: HealthUpdateRequest) -> DeploymentRecord:
    identity = _identity(request)
    try:
        return _deployment_service(request).record_health(
            tenant_id=identity.tenant_id,
            deployment_id=deployment_id,
            status=payload.status,
            detail=payload.detail,
            trace_ref=payload.trace_ref,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/agents/{logical_agent_id}/rollback",
    response_model=DeploymentRecord,
    status_code=status.HTTP_201_CREATED,
)
def rollback(request: Request, logical_agent_id: str, payload: RollbackRequest) -> DeploymentRecord:
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _deployment_service(request).rollback(
            tenant_id=identity.tenant_id,
            logical_agent_id=logical_agent_id,
            deployment_id=payload.deployment_id,
            target_version_id=payload.target_version_id,
            deployed_by=identity.user_id,
            actor_role=role,
        )
    except DeploymentServiceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/resolve", response_model=ResolvedAgentContract)
def resolve_logical_agent(
    request: Request,
    logical_agent_id: str,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> ResolvedAgentContract:
    """Composition/resolution contract for the future typed workflow compiler.

    Resolves a stable logical agent ID (within this tenant/workspace and the
    requested ``environment``) to the exact pinned version/release, its
    manifest hash, runtime endpoint, typed I/O schema refs, and capability
    versions. A published workflow must pin this response's ``version_id``/
    ``release_id``/``manifest_hash`` at compose time; execution must never
    silently re-resolve to "whatever is latest" later.
    """
    identity = _identity(request)
    contract = _deployment_service(request).resolve(
        tenant_id=identity.tenant_id,
        logical_agent_id=logical_agent_id,
        environment=environment,
    )
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
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> ResolvedAgentContract:
    """Exact-version contract lookup for the future node palette/compiler.

    Unlike ``/resolve`` (which follows the *current* environment binding for
    a logical agent), this looks up one already-known ``version_id``
    directly - for re-validating a previously composed/pinned workflow node
    without depending on whatever is currently bound to an environment.
    """
    identity = _identity(request)
    contract = _deployment_service(request).contract_for_version(
        tenant_id=identity.tenant_id,
        version_id=version_id,
        environment=environment,
    )
    if contract is None:
        raise _not_found(f"Version '{version_id}' has no released contract.")
    return contract


@router.get("/catalog", response_model=list[ResolvedAgentContract])
def get_released_agent_catalog(
    request: Request,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> list[ResolvedAgentContract]:
    """Released-agent catalog for the future node palette/compiler.

    Lists the exact, pinned contract currently bound to ``environment`` for
    every logical agent this tenant owns a draft for. Agents with no
    environment binding yet are omitted (there is nothing to pin).
    """
    identity = _identity(request)
    return list(
        _deployment_service(request).catalog(tenant_id=identity.tenant_id, environment=environment)
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
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    entry = MemoryEntry(
        id=str(uuid4()),
        tenant_id=identity.tenant_id,
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
    scope_kind: MemoryScopeKind,
    scope_id: str,
    limit: int = 100,
) -> list[MemoryEntry]:
    identity = _identity(request)
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return list(
            _memory_service(request).recall(
                draft.manifest,
                tenant_id=identity.tenant_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                actor_id=identity.user_id,
                limit=limit,
            )
        )
    except MemoryPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory/{entry_id}", response_model=MemoryEntry)
def inspect_memory_entry(request: Request, logical_agent_id: str, entry_id: str) -> MemoryEntry:
    """Inspect a single memory entry (GA-mechanism memory governance: inspect)."""
    identity = _identity(request)
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).inspect(
            draft.manifest,
            tenant_id=identity.tenant_id,
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
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).correct(
            draft.manifest,
            tenant_id=identity.tenant_id,
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
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return _memory_service(request).forget(
            draft.manifest,
            tenant_id=identity.tenant_id,
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
    scope_kind: MemoryScopeKind,
    scope_id: str,
) -> list[MemoryEntry]:
    """Export all readable memory entries for a scope (GA-mechanism memory governance: export)."""
    identity = _identity(request)
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    try:
        return list(
            _memory_service(request).export(
                draft.manifest,
                tenant_id=identity.tenant_id,
                scope_kind=scope_kind,
                scope_id=scope_id,
                actor_id=identity.user_id,
            )
        )
    except MemoryPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/agents/{logical_agent_id}/memory/{entry_id}/audit", response_model=list[MemoryAuditRecord])
def memory_audit_trail(request: Request, logical_agent_id: str, entry_id: str) -> list[MemoryAuditRecord]:
    """Deletion/provenance audit trail for a single memory entry."""
    identity = _identity(request)
    draft = _store(request).get_draft(identity.tenant_id, logical_agent_id)
    if draft is None:
        raise _not_found(f"Agent '{logical_agent_id}' was not found.")
    return list(_memory_service(request).audit_trail(tenant_id=identity.tenant_id, entry_id=entry_id))


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
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _builder_service(request).propose(
            tenant_id=identity.tenant_id,
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
def list_builder_proposals(request: Request, logical_agent_id: str) -> list[BuilderProposal]:
    """Proposal history for an agent (pending, applied, and rejected)."""
    identity = _identity(request)
    return list(_builder_service(request).list_proposals(identity.tenant_id, logical_agent_id))


@router.get("/agents/{logical_agent_id}/proposals/{proposal_id}", response_model=BuilderProposal)
def get_builder_proposal(request: Request, logical_agent_id: str, proposal_id: str) -> BuilderProposal:
    identity = _identity(request)
    proposal = _builder_service(request).get_proposal(identity.tenant_id, logical_agent_id, proposal_id)
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
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _builder_service(request).apply(
            tenant_id=identity.tenant_id,
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


@router.post("/agents/{logical_agent_id}/proposals/{proposal_id}/reject", response_model=BuilderProposal)
def reject_builder_proposal(
    request: Request,
    logical_agent_id: str,
    proposal_id: str,
    payload: BuilderRejectRequest,
) -> BuilderProposal:
    identity = _identity(request)
    role = _actor_role(request, identity, logical_agent_id)
    try:
        return _builder_service(request).reject(
            tenant_id=identity.tenant_id,
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
