from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from research_assistant_core import (
    WORKFLOW_BLUEPRINTS,
    Capability,
    CapabilitySpec,
    ResearchRequest,
)
from research_assistant_core.agent_surfaces import (
    AgentSurface,
    agent_surfaces,
    agents_for_capability,
    capability_specs,
)
from research_assistant_core.connector_catalog import connector_definition
from research_assistant_core.models import RunStatus
from research_assistant_core.studio_models import (
    AutomationStudioResult,
    StudioRunRequest,
)
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint

from research_assistant_api.agent_chat import build_agent_chat_gateway
from research_assistant_api.agent_chat import router as agent_chat_router
from research_assistant_api.agent_studio.approval_consumption import StoreBackedApprovalConsumptionPort
from research_assistant_api.agent_studio.approval_context import StoreBackedApprovalContextResolver
from research_assistant_api.agent_studio.artifact_bundle_store import build_artifact_bundle_store
from research_assistant_api.agent_studio.audit_service import (
    AuditService,
    AuditStoreUnavailableError,
    build_audit_store,
)
from research_assistant_api.agent_studio.authz import ClaimsGroupMembershipResolver
from research_assistant_api.agent_studio.builder_service import (
    BuilderService,
    build_manifest_proposal_generator,
)
from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoveryRequest,
    build_capability_discovery_source,
)
from research_assistant_api.agent_studio.capability_registry import build_registry_from_source
from research_assistant_api.agent_studio.cosmos_store import build_agent_studio_store
from research_assistant_api.agent_studio.deployment_service import DeploymentService
from research_assistant_api.agent_studio.foundry_agent_inventory import build_foundry_agent_inventory
from research_assistant_api.agent_studio.foundry_prompt_publisher import build_prompt_agent_publisher
from research_assistant_api.agent_studio.idempotency import StoreBackedIdempotencyPort
from research_assistant_api.agent_studio.memory_service import (
    MemoryService,
    MemoryStoreUnavailableError,
    build_memory_store,
)
from research_assistant_api.agent_studio.model_discovery import build_model_discovery
from research_assistant_api.agent_studio.observability_provider import build_observability_provider
from research_assistant_api.agent_studio.playground_invoker import build_playground_invoker
from research_assistant_api.agent_studio.release_attestation import StoreBackedReleaseAttestationPort
from research_assistant_api.agent_studio.release_service import ReleaseService
from research_assistant_api.agent_studio.router import router as agent_studio_router
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStoreError
from research_assistant_api.agent_studio.template_catalog import default_template_catalog
from research_assistant_api.blob_sources import (
    SourceBlobStore,
    build_source_blob_store,
)
from research_assistant_api.config import Settings, get_settings
from research_assistant_api.connector_credentials import (
    ConnectorCredentialError,
    ConnectorCredentialNotConfiguredError,
    set_connector_api_key,
)
from research_assistant_api.connector_gateway import (
    ConnectorGateway,
    ConnectorGatewayError,
    ConnectorGatewayNotConfiguredError,
    build_connector_gateway,
)
from research_assistant_api.cosmos_workspace import (
    WorkspaceProjectProvider,
    WorkspaceProjectUnavailableError,
    build_workspace_project_provider,
)
from research_assistant_api.foundry import (
    HostedAgentConfigurationError,
    HostedAgentGateway,
    HostedAgentInvocationError,
    HostedAgentNotReadyError,
    parse_hosted_agent_payload,
)
from research_assistant_api.identity import (
    IdentityContext,
    enforce_tenant_claim,
    resolve_identity,
)
from research_assistant_api.orchestration import execute_library_ingestion
from research_assistant_api.orchestration_studio import OrchestrationStudioService
from research_assistant_api.schemas import (
    AgentResearchResponse,
    AssistantRequest,
    AssistantResponse,
    HealthResponse,
    ProjectSummary,
)
from research_assistant_api.telemetry import (
    configure_telemetry,
)
from research_assistant_api.workspace import (
    AgentSetting,
    ApprovalDecision,
    ApprovalRecord,
    ConnectorCredentialUpdate,
    ConnectorSetting,
    ConnectorUpdate,
    DatasetApprovalDecisionRequest,
    DatasetApprovalDenialReason,
    DatasetApprovalError,
    DatasetApprovalRequest,
    DatasetApprovalRequestCreate,
    LibraryIngestRecord,
    LibraryIngestRequest,
    LibraryIngestResponse,
    LibraryItem,
    PersonalProjectCreate,
    PersonalProjectUpdate,
    ProjectSettings,
    RunStage,
    RunSummary,
    WorkspaceStore,
    WorkspaceSummary,
    compute_dataset_plan_fingerprint,
)

configure_telemetry("research-assistant-api")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    application.state.settings = settings
    application.state.studios = OrchestrationStudioService()
    application.state.hosted = HostedAgentGateway(settings)
    application.state.agent_chat = build_agent_chat_gateway(settings)
    application.state.workspace_projects = build_workspace_project_provider(settings)
    application.state.source_blobs = build_source_blob_store(settings)
    application.state.connector_gateway = build_connector_gateway(settings)
    await _init_agent_studio(application, settings)
    try:
        yield
    finally:
        await cast(ConnectorGateway, application.state.connector_gateway).close()
        # ``build_capability_discovery_source`` only returns a ``close``-able
        # adapter (``HttpCapabilityDiscoverySource``) when a real provider is
        # configured; ``NullCapabilityDiscoverySource`` -- the honest default
        # -- owns no HTTP client/credential and has no ``close`` method, so
        # this is looked up defensively rather than assumed to always exist.
        capability_discovery_source = getattr(application.state, "agent_studio_capability_discovery_source", None)
        close_capability_discovery_source = getattr(capability_discovery_source, "close", None)
        if close_capability_discovery_source is not None:
            await close_capability_discovery_source()


async def _init_agent_studio(application: FastAPI, settings: Settings) -> None:
    """Construct Agent Studio's stores/services for the app state.

    Metadata and memory persistence are Cosmos-backed in production and
    intentionally *raise* (rather than silently fall back to in-memory) when
    no Cosmos endpoint is configured — see ``cosmos_store.build_agent_studio_store``
    and ``memory_service.build_memory_store``. That explicit-unavailability is
    caught here so a missing Cosmos configuration degrades only the Agent
    Studio surface (its routes return 503) instead of preventing the entire
    API process from starting, which would break unrelated features (and
    local/dev environments that don't configure Cosmos) in one stroke.
    """
    capability_discovery_source = build_capability_discovery_source(settings)
    application.state.agent_studio_capability_discovery_source = capability_discovery_source
    # A single, startup-time discovery pass scoped to this deployment's one
    # tenant/project (``settings.workspace_tenant_id``/``workspace_project_id``
    # -- the same single-tenant-per-deployment boundary ``cosmos_workspace``/
    # ``identity`` already assume) is composed here via
    # ``build_capability_discovery_source``: the real, authenticated
    # ``HttpCapabilityDiscoverySource`` when a provider URL is configured, or
    # the honest ``NullCapabilityDiscoverySource`` otherwise. ``router.py``'s
    # capability routes read a single shared ``app.state.agent_studio_registry``
    # per request (not a per-request re-discovery), so one pass here -- never
    # a hard-coded seed catalog -- is the correct composition, not a
    # shortcut.
    registry = await build_registry_from_source(
        capability_discovery_source,
        CapabilityDiscoveryRequest(
            scope=ScopeContext(tenant_id=settings.workspace_tenant_id, project_id=settings.workspace_project_id),
            principal="system:agent-studio-startup-capability-discovery",
            correlation_id=str(uuid4()),
        ),
    )
    application.state.agent_studio_registry = registry
    # Application-owned adapter for the ``ProjectMembershipResolver`` domain
    # port (see ``agent_studio.authz``). Explicit here (rather than relying
    # on the router's fallback default) so swapping in a future Graph/
    # app-role-membership adapter is a one-line change at this composition
    # root, with no router/service code change required.
    application.state.agent_studio_membership_resolver = ClaimsGroupMembershipResolver()
    model_discovery = build_model_discovery(settings)
    application.state.agent_studio_model_discovery = model_discovery
    application.state.agent_studio_foundry_agent_inventory = build_foundry_agent_inventory(settings)
    application.state.agent_studio_prompt_agent_publisher = build_prompt_agent_publisher(settings)
    # Platform-owned governed template catalog (see ``template_catalog``
    # module docstring for why a built-in seed is legitimate here, unlike
    # the capability registry above).
    application.state.agent_studio_template_catalog = default_template_catalog()
    # Playground/test-run invocation requires the harness-owned runtime and
    # therefore uses an honest unavailable adapter when no invoker is configured.
    application.state.agent_studio_playground_invoker = build_playground_invoker(settings)
    # Deployment Observability/Monitor read surface. Unlike the playground
    # invoker above, this *is* wired to a real adapter
    # when configured (mirrors ``model_discovery`` above) -- querying
    # already-emitted Application Insights telemetry is this platform's own
    # ownership, not the harness-owned runtime invocation path.
    application.state.agent_studio_observability_provider = build_observability_provider(settings)
    try:
        store = build_agent_studio_store(settings)
    except AgentStudioStoreError as exc:
        logger.warning("Agent Studio metadata store unavailable: %s", exc)
        application.state.agent_studio_store = None
        application.state.agent_studio_release_service = None
        application.state.agent_studio_deployment_service = None
        application.state.agent_studio_builder_service = None
        application.state.agent_studio_approval_consumption_port = None
        application.state.agent_studio_idempotency_port = None
        application.state.agent_studio_approval_context_resolver = None
        application.state.agent_studio_release_attestation_port = None
    else:
        application.state.agent_studio_store = store
        release_service = ReleaseService(store, registry, model_discovery=model_discovery)
        application.state.agent_studio_release_service = release_service
        application.state.agent_studio_deployment_service = DeploymentService(
            store, capability_registry=registry, model_discovery=model_discovery
        )
        application.state.agent_studio_builder_service = BuilderService(
            store,
            build_manifest_proposal_generator(settings),
            build_artifact_bundle_store(settings),
            release_service,
        )
        # Default durable approval-consumption adapter, backed directly by
        # this package's own store. A future runtime/provider adapter can
        # wrap or replace this at composition root (e.g. to additionally
        # confirm the actual tool execution succeeded before durably
        # recording consumption) without any router/service change.
        application.state.agent_studio_approval_consumption_port = StoreBackedApprovalConsumptionPort(store)
        # Default durable idempotency adapter (see ``agent_studio.idempotency``
        # module docstring for the independent-design rationale relative to
        # the harness's own ``IdempotencyStore`` contract) -- backed directly
        # by this same store, with no external provider dependency, so it is
        # production-safe as-is.
        application.state.agent_studio_idempotency_port = StoreBackedIdempotencyPort(store)
        # Default approval-context resolver: given only the plan facts a
        # runtime invocation already knows (release/binding/operation), this
        # resolves the release's own currently-effectively-approved
        # CAPABILITY_OPERATION approval and mints a fresh invocation_id --
        # closing the "API never supplies trusted approval_id/invocation_id"
        # gap without requiring a caller to guess or invent either value.
        application.state.agent_studio_approval_context_resolver = StoreBackedApprovalContextResolver(store)
        # Read-derived projection of a release's own immutable ReleaseGateReport,
        # for harness/runtime startup to verify hard gates passed before trusting
        # a release.
        application.state.agent_studio_release_attestation_port = StoreBackedReleaseAttestationPort(store)
    try:
        memory_store = build_memory_store(settings)
    except MemoryStoreUnavailableError as exc:
        logger.warning("Agent Studio memory store unavailable: %s", exc)
        application.state.agent_studio_memory_service = None
    else:
        application.state.agent_studio_memory_service = MemoryService(memory_store)
    try:
        audit_store = build_audit_store(settings)
    except AuditStoreUnavailableError as exc:
        logger.warning("Agent Studio audit store unavailable: %s", exc)
        application.state.agent_studio_audit_service = None
    else:
        # Wired into every consequential platform mutation route (draft,
        # version, release, deploy, health, rollback, approval, revocation,
        # ownership, capability, tool registration, artifact, builder-apply)
        # -- see ``router._audit_service``. Memory mutations are audited
        # separately via ``MemoryAuditAction`` (see ``audit_service`` module
        # docstring), not through this service.
        application.state.agent_studio_audit_service = AuditService(audit_store)


app = FastAPI(
    title="Research Assistant API",
    version="0.1.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "X-Request-ID", "X-MS-CLIENT-PRINCIPAL"],
)

app.include_router(agent_studio_router)
app.include_router(agent_chat_router)


def custom_openapi() -> dict[str, Any]:
    """Declare the Entra ID / Container Apps EasyAuth bearer-token boundary.

    This process does not itself validate the incoming ``Authorization``
    bearer token -- see ``research_assistant_api.identity.resolve_identity``
    and ``config.Settings.entra_auth_enforced`` -- it trusts the
    platform-injected ``x-ms-client-principal`` header once Azure Container
    Apps' built-in authentication (EasyAuth / ``authConfigs``) has already
    validated that token. Declaring the security scheme here is honest
    documentation of that boundary for API consumers/tooling (e.g. the
    harness's hosted-agent HTTP adapter) that must supply a bearer token
    satisfying the deployed Container Apps ``authConfigs`` audience; it is
    not an assertion that this process performs the validation itself.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {}).setdefault("securitySchemes", {})["entraManagedIdentity"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "Azure Entra ID bearer token, validated by Azure Container Apps' built-in "
            "authentication (EasyAuth, Microsoft.App/containerApps/authConfigs) before the "
            "request reaches this API. This process trusts only the resulting "
            "x-ms-client-principal header injected by that platform-level validation (see "
            "research_assistant_api.identity.resolve_identity); it does not independently "
            "re-parse or validate the Authorization header itself."
        ),
    }
    schema["security"] = [{"entraManagedIdentity": []}]
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]

CAPABILITY_AGENTS = agents_for_capability()

#: One agent per capability now reaches public sources through the shared
#: toolbox, so opting into online research no longer selects a different agent.
CAPABILITY_ONLINE_AGENTS = CAPABILITY_AGENTS

STUDIO_RESULT = AutomationStudioResult

ONLINE_ALLOWED = {
    Capability.LITERATURE,
    Capability.GRANT,
    Capability.MATCHING,
}


@app.middleware("http")
async def add_request_context(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid4().hex[:16]}"
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def _workspace_access(
    request: Request,
    *,
    required_groups: set[str] | None = None,
) -> tuple[WorkspaceStore, IdentityContext]:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    requested_project_id = request.headers.get("X-Research-Project-ID")
    try:
        store = provider.workspace_for(identity, requested_project_id)
    except WorkspaceProjectUnavailableError as exc:
        raise HTTPException(
            status_code=404,
            detail="The requested project is unavailable.",
        ) from exc
    if required_groups and not required_groups.intersection(identity.groups):
        raise HTTPException(
            status_code=403,
            detail="The authenticated identity lacks the required workspace role.",
        )
    return store, identity


@app.get("/health", response_model=HealthResponse, tags=["operations"])
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="healthy",
        service="research-assistant-api",
    )


@app.get("/ready", response_model=HealthResponse, tags=["operations"])
def ready(request: Request) -> HealthResponse:
    current = request.app.state.settings
    if not current.foundry_project_endpoint:
        raise HTTPException(
            status_code=503,
            detail="FOUNDRY_PROJECT_ENDPOINT is required",
        )
    return HealthResponse(
        status="ready",
        service="research-assistant-api",
    )


@app.get("/api/capabilities", tags=["research"])
def capabilities(request: Request) -> tuple[CapabilitySpec, ...]:
    return capability_specs()


@app.get("/api/agent-surfaces", tags=["research"])
def agent_surface_catalog() -> tuple[AgentSurface, ...]:
    """Everything the browser needs to render a studio, so it hardcodes nothing."""
    return agent_surfaces()


@app.get("/api/workflows", tags=["research"])
def workflows() -> list[dict[str, Any]]:
    return [
        {
            "capability": blueprint.capability,
            "title": blueprint.title,
            "purpose": blueprint.purpose,
            "primary_artifact": blueprint.primary_artifact,
            "online_research_policy": blueprint.online_research_policy,
            "stages": [
                {
                    "id": stage.id,
                    "label": stage.label,
                    "description": stage.description,
                    "owner": stage.owner,
                    "human_checkpoint": stage.human_checkpoint,
                }
                for stage in blueprint.stages
            ],
        }
        for blueprint in WORKFLOW_BLUEPRINTS.values()
    ]


@app.get(
    "/api/projects",
    response_model=list[ProjectSummary],
    tags=["projects"],
)
def projects(request: Request) -> list[ProjectSummary]:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    active_project_id = provider.active_project_id(identity)
    summaries: list[ProjectSummary] = []
    for project in provider.list_projects(identity):
        summary = provider.workspace_for(identity, project.project_id).summary()
        summaries.append(
            ProjectSummary(
                id=project.project_id,
                name=project.name,
                description=project.description,
                active_runs=summary.active_runs,
                source_count=summary.library_items,
                is_active=project.project_id == active_project_id,
            )
        )
    return summaries


def _project_summary(
    provider: WorkspaceProjectProvider,
    identity: IdentityContext,
    project_id: str,
) -> ProjectSummary:
    workspace_store = provider.workspace_for(identity, project_id)
    summary = workspace_store.summary()
    return ProjectSummary(
        id=summary.project.project_id,
        name=summary.project.name,
        description=summary.project.description,
        active_runs=summary.active_runs,
        source_count=summary.library_items,
        is_active=provider.active_project_id(identity) == project_id,
    )


@app.post(
    "/api/projects",
    response_model=ProjectSummary,
    status_code=201,
    tags=["projects"],
)
def create_project(payload: PersonalProjectCreate, request: Request) -> ProjectSummary:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    project = provider.create_project(identity, payload)
    return _project_summary(provider, identity, project.project_id)


@app.post(
    "/api/projects/{project_id}/activate",
    response_model=ProjectSummary,
    tags=["projects"],
)
def activate_project(project_id: str, request: Request) -> ProjectSummary:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    try:
        project = provider.select_project(identity, project_id)
    except WorkspaceProjectUnavailableError as exc:
        raise HTTPException(status_code=404, detail="The requested project is unavailable.") from exc
    return _project_summary(provider, identity, project.project_id)


@app.patch(
    "/api/projects/{project_id}",
    response_model=ProjectSummary,
    tags=["projects"],
)
def update_project(
    project_id: str,
    payload: PersonalProjectUpdate,
    request: Request,
) -> ProjectSummary:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    try:
        project = provider.update_project(identity, project_id, payload)
    except WorkspaceProjectUnavailableError as exc:
        raise HTTPException(status_code=404, detail="The requested project is unavailable.") from exc
    if payload.archive:
        return ProjectSummary(
            id=project.project_id,
            name=project.name,
            description=project.description,
            active_runs=0,
            source_count=0,
            is_active=False,
        )
    return _project_summary(provider, identity, project.project_id)


@app.get("/api/workspace", response_model=WorkspaceSummary, tags=["workspace"])
def workspace(request: Request) -> WorkspaceSummary:
    store, _ = _workspace_access(request)
    return store.summary()


@app.get("/api/library", response_model=list[LibraryItem], tags=["library"])
def library(request: Request) -> list[LibraryItem]:
    store, _ = _workspace_access(request)
    return store.library()


def _start_ingestion(
    record: LibraryIngestRecord,
    store: WorkspaceStore,
    identity: IdentityContext,
    background_tasks: BackgroundTasks,
) -> LibraryIngestResponse:
    if record.access == "public" and "research-admins" not in identity.groups:
        raise HTTPException(
            status_code=403,
            detail="Only a research administrator can classify a source as public.",
        )
    response = store.ingest(
        record,
        identity,
    )
    ingestion_input = {
        "run_id": response.run.id,
        "source_id": response.item.id,
        "query": f"Ingest and index {response.item.title}",
        "tenant_id": identity.tenant_id,
        "project_id": response.run.project_id,
        "capability": Capability.ORCHESTRATION.value,
        "require_approval": False,
        "workflow_kind": "library_ingestion",
        "blob_uri": response.item.blob_uri,
        "content_type": response.item.content_type,
        "checksum": response.item.checksum,
        "kind": response.item.kind,
        "title": response.item.title,
        "access": response.item.access,
        "license": response.item.license,
        "provider": response.item.provider,
        "year": response.item.publication_year,
        "group_ids": list(identity.groups),
        "ui_status": response.run.status.value,
        "ui_progress": response.run.progress,
        "ui_current_stage": response.run.current_stage,
    }
    background_tasks.add_task(execute_library_ingestion, store, ingestion_input)
    return response


@app.post(
    "/api/library/ingest",
    response_model=LibraryIngestResponse,
    tags=["library"],
)
def ingest_library_item(
    payload: LibraryIngestRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> LibraryIngestResponse:
    store, identity = _workspace_access(request)
    return _start_ingestion(
        LibraryIngestRecord(
            source_id=f"source-{uuid4().hex[:12]}",
            **payload.model_dump(),
        ),
        store,
        identity,
        background_tasks,
    )


@app.post(
    "/api/library/upload",
    response_model=LibraryIngestResponse,
    tags=["library"],
)
async def upload_library_item(
    request: Request,
    background_tasks: BackgroundTasks,
    title: Annotated[str, Form(min_length=3, max_length=240)],
    kind: Annotated[str, Form(min_length=2, max_length=80)],
    license_name: Annotated[
        str,
        Form(alias="license", min_length=2, max_length=120),
    ],
    description: Annotated[str, Form(min_length=3, max_length=1000)],
    file: Annotated[UploadFile, File()],
    source: Annotated[str, Form(min_length=2, max_length=120)] = "Workspace upload",
    publication_year: Annotated[
        int | None,
        Form(ge=1000, le=2100),
    ] = None,
    access: Annotated[
        Literal["public", "internal", "restricted"],
        Form(),
    ] = "internal",
) -> LibraryIngestResponse:
    store, identity = _workspace_access(request)
    if file.content_type not in {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }:
        raise HTTPException(
            status_code=415,
            detail="Supported uploads are PDF, plain text, Markdown, CSV, and JSON.",
        )
    content = await file.read(20_000_001)
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded source is empty.")
    if len(content) > 20_000_000:
        raise HTTPException(
            status_code=413,
            detail="The runtime ingestion limit is 20 MB per source.",
        )
    source_id = f"source-{uuid4().hex[:12]}"
    blob_store = cast(SourceBlobStore, request.app.state.source_blobs)
    stored = await run_in_threadpool(
        blob_store.put,
        tenant_id=identity.tenant_id,
        project_id=store.project_id,
        source_id=source_id,
        filename=file.filename or "source.bin",
        content_type=file.content_type,
        content=content,
    )
    return await run_in_threadpool(
        _start_ingestion,
        LibraryIngestRecord(
            source_id=source_id,
            title=title,
            kind=kind,
            source=source,
            publication_year=publication_year,
            access=access,
            license=license_name,
            description=description,
            blob_uri=stored.uri,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum=stored.checksum,
        ),
        store,
        identity,
        background_tasks,
    )


@app.get("/api/runs", response_model=list[RunSummary], tags=["runs"])
def runs(request: Request) -> list[RunSummary]:
    store, _ = _workspace_access(request)
    return store.runs()


@app.get("/api/runs/{run_id}", response_model=RunSummary, tags=["runs"])
def run_detail(run_id: str, request: Request) -> RunSummary:
    store, _ = _workspace_access(request)
    record = store.run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return record


@app.get(
    "/api/approvals",
    response_model=list[ApprovalRecord],
    tags=["approvals"],
)
def approvals(request: Request) -> list[ApprovalRecord]:
    store, _ = _workspace_access(request)
    return store.approvals()


@app.post(
    "/api/approvals/{approval_id}/decision",
    response_model=ApprovalRecord,
    tags=["approvals"],
)
def decide_approval(
    approval_id: str,
    payload: ApprovalDecision,
    request: Request,
) -> ApprovalRecord:
    store, identity = _workspace_access(
        request,
        required_groups={"grant-reviewers", "research-reviewers"},
    )
    approval = store.approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    try:
        record = store.decide_approval(approval_id, payload, identity)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    if record.event_delivery in {"delivered", "not_required"}:
        return record
    run = store.run(record.run_id)
    if run is None:
        raise HTTPException(status_code=409, detail="Approval run no longer exists.")
    return store.mark_approval_delivery(approval_id, "not_required") or record


@app.get(
    "/api/studios/dataset/approval-requests",
    response_model=list[DatasetApprovalRequest],
    tags=["studios"],
)
def dataset_approval_requests(request: Request) -> list[DatasetApprovalRequest]:
    store, _ = _workspace_access(request)
    return store.dataset_approval_requests()


@app.post(
    "/api/studios/dataset/approval-requests",
    response_model=DatasetApprovalRequest,
    status_code=201,
    tags=["studios"],
)
def request_dataset_approval(
    payload: DatasetApprovalRequestCreate,
    request: Request,
) -> DatasetApprovalRequest:
    store, identity = _workspace_access(request)
    fingerprint = compute_dataset_plan_fingerprint(
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        objective=payload.objective,
        filename=payload.filename,
        csv_text=payload.csv_text,
    )
    return store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename=payload.filename,
        objective=payload.objective,
        requested_by=identity.display_name,
        ttl_minutes=payload.ttl_minutes,
        requested_by_principal_id=identity.user_id,
    )


@app.post(
    "/api/studios/dataset/approval-requests/{request_id}/decision",
    response_model=DatasetApprovalRequest,
    tags=["studios"],
)
def decide_dataset_approval(
    request_id: str,
    payload: DatasetApprovalDecisionRequest,
    request: Request,
) -> DatasetApprovalRequest:
    store, identity = _workspace_access(
        request,
        required_groups={"grant-reviewers", "research-reviewers"},
    )
    existing = store.dataset_approval_request(request_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Dataset approval request not found.")
    try:
        record = store.decide_dataset_approval_request(request_id, payload, identity)
    except DatasetApprovalError as exc:
        raise _dataset_denial(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Dataset approval request not found.")
    return record


@app.get(
    "/api/connectors",
    response_model=list[ConnectorSetting],
    tags=["connectors"],
)
def connectors(request: Request) -> list[ConnectorSetting]:
    store, _ = _workspace_access(request)
    return store.connectors()


@app.put(
    "/api/connectors/{connector_id}",
    response_model=ConnectorSetting,
    tags=["connectors"],
)
def update_connector(
    connector_id: str,
    payload: ConnectorUpdate,
    request: Request,
) -> ConnectorSetting:
    store, _ = _workspace_access(
        request,
        required_groups={"research-admins"},
    )
    try:
        connector = store.update_connector(connector_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    return connector


@app.put(
    "/api/connectors/{connector_id}/credential",
    response_model=ConnectorSetting,
    tags=["connectors"],
)
def update_connector_credential(
    connector_id: str,
    payload: ConnectorCredentialUpdate,
    request: Request,
) -> ConnectorSetting:
    store, _ = _workspace_access(
        request,
        required_groups={"research-admins"},
    )
    connector = next(
        (item for item in store.connectors() if item.id == connector_id),
        None,
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    definition = connector_definition(connector_id)
    if definition.credential.kind == "none":
        raise HTTPException(
            status_code=422,
            detail="This connector does not accept a credential.",
        )
    try:
        set_connector_api_key(definition.credential.named_value, payload.api_key)
    except ConnectorCredentialNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConnectorCredentialError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return connector


async def _probe_connector(
    gateway: ConnectorGateway,
    capability: Capability,
    connector_id: str,
) -> str:
    try:
        probe_query = connector_definition(connector_id).probe_query
        result = await gateway.search(
            capability,
            connector_id,
            probe_query,
            limit=1,
        )
        return "ready_with_key" if result.warnings else "ready"
    except ConnectorGatewayNotConfiguredError as exc:
        logger.info("Connector %s test requires gateway setup: %s", connector_id, exc)
        return "configuration_required"
    except ConnectorGatewayError as exc:
        logger.warning("Connector %s test failed: %s", connector_id, exc)
        return "unavailable"


@app.post(
    "/api/connectors/{connector_id}/test",
    response_model=ConnectorSetting,
    tags=["connectors"],
)
async def test_connector(connector_id: str, request: Request) -> ConnectorSetting:
    store, _ = _workspace_access(
        request,
        required_groups={"research-admins"},
    )
    connector = next(
        (item for item in store.connectors() if item.id == connector_id),
        None,
    )
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    capability = next(
        (Capability(agent) for agent in connector.assigned_agents if agent in {item.value for item in ONLINE_ALLOWED}),
        Capability.LITERATURE,
    )
    status_result = await _probe_connector(
        cast(ConnectorGateway, request.app.state.connector_gateway),
        capability,
        connector_id,
    )
    try:
        connector = store.record_connector_test(connector_id, status_result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    return connector


@app.get(
    "/api/settings",
    response_model=ProjectSettings,
    tags=["settings"],
)
def project_settings(request: Request) -> ProjectSettings:
    store, _ = _workspace_access(request)
    return store.settings()


@app.put(
    "/api/settings",
    response_model=ProjectSettings,
    tags=["settings"],
)
def update_project_settings(
    payload: ProjectSettings,
    request: Request,
) -> ProjectSettings:
    store, _ = _workspace_access(
        request,
        required_groups={"research-admins"},
    )
    try:
        return store.update_settings(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/agents", response_model=list[AgentSetting], tags=["agents"])
def agents(request: Request) -> list[AgentSetting]:
    store, _ = _workspace_access(request)
    return store.agents()


def _raw_dataset_csv(payload: StudioRunRequest) -> str:
    value = payload.inputs.get("csv_text")
    return "" if value is None else str(value)


def _record_studio_result(
    result: STUDIO_RESULT,
    store: WorkspaceStore,
) -> None:
    blueprint = WORKFLOW_BLUEPRINTS[result.run.capability]
    current_index = next(
        (index for index, stage in enumerate(blueprint.stages) if stage.label == result.run.current_stage),
        len(blueprint.stages) - 1,
    )
    stages = [
        RunStage(
            id=stage.id,
            label=stage.label,
            status=(
                "completed"
                if result.run.progress == 100 or index < current_index
                else "waiting_for_approval"
                if index == current_index and result.run.status == RunStatus.WAITING_FOR_APPROVAL
                else "failed"
                if index == current_index and result.run.status in {RunStatus.BLOCKED, RunStatus.FAILED}
                else "running"
                if index == current_index
                else "planned"
            ),
            owner=stage.owner,
        )
        for index, stage in enumerate(blueprint.stages)
    ]
    store.add_run(
        run_id=result.run.id,
        capability=result.run.capability,
        title=result.run.title,
        owner=result.run.owner,
        status=result.run.status,
        progress=result.run.progress,
        current_stage=result.run.current_stage,
        stages=stages,
        artifact_count=1,
    )
    if result.run.status != RunStatus.WAITING_FOR_APPROVAL:
        return
    approval_details = {
        Capability.GRANT: (
            "Approve grant package release",
            "Release this exact grant package for institutional review.",
            "SharePoint research site / Grant reviews",
            "grant-agent",
            "Requirements and unsupported fact checks completed.",
            "High",
        ),
        Capability.DATASET: (
            "Approve external compute",
            "Submit the referenced read-only dataset job with its estimate.",
            "Approved Azure Machine Learning compute adapter",
            "dataset-agent",
            "Estimate and data-boundary checks completed.",
            "Medium",
        ),
        Capability.ORCHESTRATION: (
            "Activate workflow",
            (
                f"Enable the exact validated workflow graph {result.graph_hash[:12]} and configured trigger."
                if isinstance(result, AutomationStudioResult)
                else "Enable the exact validated workflow graph."
            ),
            "Research Assistant workflow",
            "research-coordinator",
            (
                f"Dry run passed for graph {result.graph_hash}; external steps remain blocked."
                if isinstance(result, AutomationStudioResult)
                else "Dry run passed; external steps remain blocked."
            ),
            "High",
        ),
    }
    details = approval_details[result.run.capability]
    store.add_approval(
        run_id=result.run.id,
        title=details[0],
        gated_action=details[1],
        destination=details[2],
        requested_by=details[3],
        evidence_summary=details[4],
        risk=details[5],
    )


@dataclass(frozen=True, slots=True)
class DatasetSendGrant:
    """A server-minted receipt proving that exactly one durable, reviewer-decided
    dataset approval was atomically consumed for a specific dataset plan.

    It is the *only* thing that authorizes embedding raw ``csv_text`` in a
    hosted-agent message: :func:`_agent_message` refuses to build a dataset
    message without a grant whose ``plan_fingerprint`` matches the CSV it is
    about to send. Because a grant can only be produced by consuming an
    approval, no route (studios, research, or any other ``_agent_message``
    caller) can send dataset bytes to Foundry without that single-use
    consumption having already happened.

    Every fact the grant authorizes is carried explicitly -- tenant, project,
    capability, the consuming principal, and the plan fingerprint -- rather
    than inferred from ambient request state, so a grant can never be
    cross-used for a different tenant, project, or capability even if a future
    caller reorders or reuses it.
    """

    approval_request_id: str
    tenant_id: str
    project_id: str
    capability: Capability
    plan_fingerprint: str
    invocation_id: str
    consumed_by_principal_id: str


#: Deterministic mapping from a fail-closed dataset-approval denial reason to the
#: HTTP status the API returns. Every consumption-time denial is a 409 (the
#: request is well-formed but cannot be authorized in the resource's current
#: state); a separation-of-duties violation at decision time is a 403.
_DATASET_DENIAL_STATUS: dict[DatasetApprovalDenialReason, int] = {
    DatasetApprovalDenialReason.NOT_FOUND: 409,
    DatasetApprovalDenialReason.FINGERPRINT_MISMATCH: 409,
    DatasetApprovalDenialReason.ALREADY_CONSUMED: 409,
    DatasetApprovalDenialReason.REJECTED: 409,
    DatasetApprovalDenialReason.PENDING: 409,
    DatasetApprovalDenialReason.EXPIRED: 409,
    DatasetApprovalDenialReason.CONCURRENT_CONFLICT: 409,
    DatasetApprovalDenialReason.ALREADY_DECIDED: 409,
    DatasetApprovalDenialReason.MISSING_APPROVAL_REFERENCE: 409,
    DatasetApprovalDenialReason.SEPARATION_OF_DUTIES: 403,
    DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER: 403,
    DatasetApprovalDenialReason.PRINCIPAL_MISMATCH: 403,
    DatasetApprovalDenialReason.GRANT_INVARIANT: 409,
}


def _dataset_denial(exc: DatasetApprovalError) -> HTTPException:
    """Translate a typed dataset-approval denial into an ``HTTPException``.

    The wire ``detail`` stays a human-readable string (the web client renders
    it directly) while the stable, machine-readable reason code is surfaced in
    the ``X-Dataset-Approval-Denial`` response header for programmatic callers.
    """
    status = _DATASET_DENIAL_STATUS.get(exc.reason, 409)
    return HTTPException(
        status_code=status,
        detail=str(exc),
        headers={"X-Dataset-Approval-Denial": exc.reason.value},
    )


def _dataset_approval_plan(
    capability: Capability,
    payload: StudioRunRequest,
    store: WorkspaceStore,
) -> tuple[str, str] | None:
    """Resolve ``(approval_request_id, plan_fingerprint)`` for a dataset run, or
    ``None`` when there is no client-supplied CSV to authorize.

    A client-supplied ``analysis_approved``/``compute_adapter_configured``
    boolean grants nothing -- neither is ever inspected. The only thing that can
    authorize dataset material is a server-resolved, single-use consumption of a
    reviewer-decided approval request, matching how ``ApprovalContextResolver``/
    ``ApprovalConsumptionPort`` already gate Agent Studio capability operations.
    """
    if capability != Capability.DATASET:
        return None
    csv_text = _raw_dataset_csv(payload)
    if not csv_text:
        return None
    approval_request_id = payload.inputs.get("approval_request_id")
    if not isinstance(approval_request_id, str) or not approval_request_id:
        raise _dataset_denial(
            DatasetApprovalError(
                DatasetApprovalDenialReason.MISSING_APPROVAL_REFERENCE,
                "Dataset analysis requires a decided dataset approval request "
                "referenced by 'approval_request_id'; client-supplied approval "
                "flags are not accepted.",
            )
        )
    fingerprint = compute_dataset_plan_fingerprint(
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        objective=payload.objective,
        filename=str(payload.inputs.get("filename", "dataset.csv")),
        csv_text=csv_text,
    )
    return approval_request_id, fingerprint


def _validate_dataset_analysis(
    capability: Capability,
    payload: StudioRunRequest,
    store: WorkspaceStore,
    identity: IdentityContext,
) -> None:
    """Early, NON-MUTATING fail-fast, run before any local processing of dataset
    content.

    ``research.run`` parses and profiles the supplied CSV locally
    (``research_core.service._dataset`` -> ``profile_csv``), so unapproved
    client CSV must be rejected *before* that happens -- otherwise moving
    authorization later would trade an availability/audit defect for a
    confidentiality-adjacent one.

    This is deliberately NOT the authorization gate and must never become
    load-bearing: between this check and the real transition the approval can be
    consumed by a concurrent request, revoked, or expire. It spends nothing, so
    a losing race here costs only a rejected request, never a burned approval.
    :func:`_consume_dataset_analysis` re-verifies every condition and remains
    the sole authority.
    """
    plan = _dataset_approval_plan(capability, payload, store)
    if plan is None:
        return
    approval_request_id, fingerprint = plan
    try:
        store.validate_dataset_approval_request(
            approval_request_id,
            plan_fingerprint=fingerprint,
            consumed_by_principal_id=identity.user_id,
        )
    except DatasetApprovalError as exc:
        raise _dataset_denial(exc) from exc


def _consume_dataset_analysis(
    capability: Capability,
    payload: StudioRunRequest,
    store: WorkspaceStore,
    identity: IdentityContext,
) -> DatasetSendGrant | None:
    """The authoritative, atomic, single-use state transition. Call this
    immediately before the hosted send and nowhere else.

    Placement is deliberate on both sides:

    * Not earlier -- consuming before it is known that a send will occur burns a
      reviewer-decided approval when nothing is sent, which is both an
      availability lever on the approval workflow and an audit-fidelity defect
      (``action="consumed"`` must imply data really left).
    * Not later -- consuming *after* the send would risk authorizing a second
      send for one approval.

    The residual is therefore inherent and accepted: if ``gateway.invoke``
    fails, the approval has already been spent even though no bytes arrived.
    At-most-once and never-burn-on-failure cannot both hold, and burning is the
    fail-closed direction; the requester re-submits. This is documented rather
    than "fixed", because every fix for it reintroduces a double-send risk.

    The grant is minted here, by this transition, so it can never outlive or
    precede the consumption that authorized it.
    """
    plan = _dataset_approval_plan(capability, payload, store)
    if plan is None:
        return None
    approval_request_id, fingerprint = plan
    try:
        record = store.consume_dataset_approval_request(
            approval_request_id,
            plan_fingerprint=fingerprint,
            invocation_id=f"inv-{uuid4().hex}",
            consumed_by_principal_id=identity.user_id,
        )
    except DatasetApprovalError as exc:
        raise _dataset_denial(exc) from exc
    return DatasetSendGrant(
        approval_request_id=record.id,
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        capability=capability,
        plan_fingerprint=record.plan_fingerprint,
        invocation_id=str(record.consumed_invocation_id),
        consumed_by_principal_id=identity.user_id,
    )


def _record_dataset_send_outcome(
    store: WorkspaceStore,
    grant: DatasetSendGrant | None,
    identity: IdentityContext,
    *,
    delivered: bool,
) -> None:
    """Close the audit loop on a consumed dataset approval.

    Consumption necessarily precedes the send, so ``action="consumed"`` can only
    ever mean a send was ATTEMPTED -- ``gateway.invoke`` may still raise after
    the single-use approval has been spent. This records the second entry that
    makes the trail unambiguous. A no-op when nothing was consumed (non-dataset
    capability, or no client-supplied CSV).
    """
    if grant is None:
        return
    store.record_dataset_send_outcome(
        grant.approval_request_id,
        invocation_id=grant.invocation_id,
        plan_fingerprint=grant.plan_fingerprint,
        delivered=delivered,
        actor_principal_id=identity.user_id,
    )


def _hosted_request_message(
    capability: Capability,
    payload: StudioRunRequest,
    settings: Settings,
    identity: IdentityContext,
    run_id: str,
    *,
    dataset_grant: DatasetSendGrant | None = None,
) -> str:
    envelope: dict[str, Any] = {
        "query": payload.objective,
        "tenant_id": settings.workspace_tenant_id,
        "project_id": settings.workspace_project_id,
        "principal_id": identity.user_id,
        "session_id": run_id,
        "sensitivity": "internal",
    }
    if capability == Capability.LITERATURE:
        envelope["review_question"] = payload.objective
    elif capability == Capability.GRANT:
        opportunity_id = payload.inputs.get("opportunity_id")
        if isinstance(opportunity_id, str) and opportunity_id.strip():
            envelope["opportunity_id"] = opportunity_id.strip()
    elif capability == Capability.MATCHING:
        required_facets = payload.inputs.get("required_facets", [])
        if not isinstance(required_facets, list) or not all(isinstance(facet, str) for facet in required_facets):
            raise ValueError("Matching required_facets must be a list of strings.")
        envelope["required_facets"] = required_facets
    elif capability == Capability.DATASET:
        dataset_id = payload.inputs.get("dataset_id") or payload.inputs.get("filename")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("Dataset research requires a governed dataset_id.")
        if payload.inputs.get("csv_text"):
            raise ValueError("Upload dataset files through the live agent chat session.")
        envelope["dataset_id"] = dataset_id.strip()
        if dataset_grant is not None:
            envelope.update(
                approval_decision_id=dataset_grant.approval_request_id,
                invocation_id=dataset_grant.invocation_id,
                idempotency_key=dataset_grant.plan_fingerprint,
            )
    elif capability == Capability.INSTITUTIONAL_QA:
        policy_scope = payload.inputs.get("policy_scope")
        if isinstance(policy_scope, str) and policy_scope.strip():
            envelope["policy_scope"] = policy_scope.strip()
    elif capability == Capability.SCREENING:
        for key in ("inclusion_criteria", "exclusion_criteria", "evidence"):
            value = payload.inputs.get(key)
            if value is not None:
                envelope[key] = value
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))


def _agent_research_response(
    capability: Capability,
    run_id: str,
    reply: Any,
) -> AgentResearchResponse:
    payload = parse_hosted_agent_payload(reply.content)
    fields = {"summary", "claims", "limitations", "evidence"}
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise HostedAgentInvocationError(f"Hosted Agent {reply.agent_name} returned no valid summary.")
    try:
        return AgentResearchResponse(
            capability=capability,
            run_id=run_id,
            agent_name=reply.agent_name,
            response_id=reply.response_id,
            summary=summary,
            claims=payload.get("claims", []),
            limitations=payload.get("limitations", []),
            evidence=payload.get("evidence", []),
            details={key: value for key, value in payload.items() if key not in fields},
        )
    except ValueError as exc:
        raise HostedAgentInvocationError(
            f"Hosted Agent {reply.agent_name} returned an invalid research contract."
        ) from exc


@app.post(
    "/api/studios/{capability}/run",
    response_model=STUDIO_RESULT,
    tags=["studios"],
)
async def run_studio(
    capability: Capability,
    payload: StudioRunRequest,
    request: Request,
) -> STUDIO_RESULT:
    if capability != Capability.ORCHESTRATION:
        raise HTTPException(
            status_code=503,
            detail="This capability runs through the live agent-chat surface.",
        )
    store, identity = _workspace_access(request)
    service = cast(OrchestrationStudioService, request.app.state.studios)
    try:
        result = service.run(
            payload,
            owner=identity.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _record_studio_result(result, store)
    return result


@app.post(
    "/api/research/{capability}",
    response_model=AgentResearchResponse,
    tags=["research"],
)
async def run_capability(
    capability: Capability,
    payload: ResearchRequest,
    request: Request,
) -> AgentResearchResponse:
    current = cast(Settings, request.app.state.settings)
    store, identity = _workspace_access(request)
    enforce_tenant_claim(identity, payload.tenant_id)
    if payload.project_id != store.project_id:
        raise HTTPException(
            status_code=403,
            detail="Request project is not authorized for this workspace.",
        )
    if capability == Capability.ORCHESTRATION:
        raise HTTPException(
            status_code=422,
            detail="Use the workflow studio for orchestration validation.",
        )
    studio_request = StudioRunRequest(
        objective=payload.query,
        inputs=payload.context,
    )
    _validate_dataset_analysis(capability, studio_request, store, identity)
    gateway = cast(HostedAgentGateway, request.app.state.hosted)
    dataset_grant = _consume_dataset_analysis(capability, studio_request, store, identity)
    run_id = f"run-{uuid4().hex[:12]}"
    try:
        reply = await run_in_threadpool(
            gateway.invoke,
            _hosted_request_message(
                capability,
                studio_request,
                current,
                identity,
                run_id,
                dataset_grant=dataset_grant,
            ),
            agent_name=CAPABILITY_AGENTS[capability],
        )
        result = _agent_research_response(capability, run_id, reply)
    except (KeyError, ValueError) as exc:
        _record_dataset_send_outcome(store, dataset_grant, identity, delivered=False)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DatasetApprovalError as exc:
        _record_dataset_send_outcome(store, dataset_grant, identity, delivered=False)
        raise _dataset_denial(exc) from exc
    except HostedAgentConfigurationError as exc:
        _record_dataset_send_outcome(store, dataset_grant, identity, delivered=False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HostedAgentNotReadyError as exc:
        _record_dataset_send_outcome(store, dataset_grant, identity, delivered=False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HostedAgentInvocationError as exc:
        _record_dataset_send_outcome(store, dataset_grant, identity, delivered=False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    _record_dataset_send_outcome(store, dataset_grant, identity, delivered=True)
    return result


@app.post(
    "/api/assistant",
    response_model=AssistantResponse,
    tags=["agents"],
)
async def invoke_assistant(
    payload: AssistantRequest,
    request: Request,
) -> AssistantResponse:
    _, identity = _workspace_access(request)
    capability = payload.capability or Capability.LITERATURE
    if capability in {Capability.DATASET, Capability.ORCHESTRATION}:
        raise HTTPException(
            status_code=422,
            detail="Use the capability-specific live surface for this request.",
        )
    settings = cast(Settings, request.app.state.settings)
    run_id = f"run-{uuid4().hex[:12]}"
    gateway = cast(HostedAgentGateway, request.app.state.hosted)
    try:
        reply = await run_in_threadpool(
            gateway.invoke,
            _hosted_request_message(
                capability,
                StudioRunRequest(objective=payload.message),
                settings,
                identity,
                run_id,
            ),
            agent_name=CAPABILITY_AGENTS[capability],
        )
        result = _agent_research_response(capability, run_id, reply)
    except (HostedAgentConfigurationError, HostedAgentNotReadyError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HostedAgentInvocationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return AssistantResponse(
        mode="hosted",
        agent_name=result.agent_name,
        content=result.summary,
        response_id=result.response_id,
    )
