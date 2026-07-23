"""Integration tests for the Agent Studio FastAPI router."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.approval_consumption import StoreBackedApprovalConsumptionPort
from research_assistant_api.agent_studio.approval_context import StoreBackedApprovalContextResolver
from research_assistant_api.agent_studio.artifact_bundle_store import InMemoryArtifactBundleStore
from research_assistant_api.agent_studio.audit_service import AuditService, InMemoryAuditStore
from research_assistant_api.agent_studio.authz import (
    MembershipCheckRequest,
    MembershipDecision,
    MembershipOutcome,
    ProjectMembershipResolver,
)
from research_assistant_api.agent_studio.builder_service import (
    BuilderService,
    InMemoryManifestProposalGenerator,
    ProposedManifestChange,
    UnavailableManifestProposalGenerator,
)
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityRegistry,
    seeded_test_registry,
)
from research_assistant_api.agent_studio.deployment_service import DeploymentService
from research_assistant_api.agent_studio.idempotency import StoreBackedIdempotencyPort
from research_assistant_api.agent_studio.memory_service import InMemoryMemoryStore, MemoryService
from research_assistant_api.agent_studio.model_discovery import (
    InMemoryModelDiscovery,
    ModelDiscovery,
    UnavailableModelDiscovery,
)
from research_assistant_api.agent_studio.models import (
    AGENT_MANIFEST_SCHEMA_VERSION,
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    ApprovalKind,
    AuditEventKind,
    CapabilityInstance,
    DeploymentEnvironment,
    HealthStatus,
    InstanceReadiness,
    MemoryMechanism,
    MemoryScopeKind,
    ModelDeploymentRef,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence
from research_assistant_api.agent_studio.release_attestation import StoreBackedReleaseAttestationPort
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    ReleaseService,
    ReleaseServiceError,
)
from research_assistant_api.agent_studio.router import claim_idempotency_route
from research_assistant_api.agent_studio.router import router as agent_studio_router
from research_assistant_api.agent_studio.schemas import ClaimIdempotencyRequest
from research_assistant_api.agent_studio.scope import PLATFORM_PROJECT_ID, ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings
from research_assistant_api.identity import project_group_name

DEFAULT_PROJECT_ID = "default"
OTHER_PROJECT_ID = "other-project"

GATED_EVIDENCE = {
    "evidence": {
        "build_succeeded": True,
        "tests_passed": True,
        "smoke_passed": True,
    }
}


def _principal(
    *,
    tenant_id: str,
    user_id: str,
    groups: tuple[str, ...] = (),
    groups_overage: bool = False,
) -> str:
    claims = [
        {"typ": "tid", "val": tenant_id},
        *({"typ": "groups", "val": group} for group in groups),
    ]
    if groups_overage:
        claims.append({"typ": "hasgroups", "val": "true"})
    payload = {
        "userId": user_id,
        "userDetails": user_id,
        "claims": claims,
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _headers(
    *,
    tenant_id: str,
    user_id: str,
    groups: tuple[str, ...] = (),
    groups_overage: bool = False,
) -> dict[str, str]:
    return {
        "x-ms-client-principal": _principal(
            tenant_id=tenant_id,
            user_id=user_id,
            groups=groups,
            groups_overage=groups_overage,
        )
    }


def _project_headers(
    *,
    tenant_id: str,
    user_id: str,
    project_ids: tuple[str, ...],
    extra_groups: tuple[str, ...] = (),
    groups_overage: bool = False,
) -> dict[str, str]:
    return _headers(
        tenant_id=tenant_id,
        user_id=user_id,
        groups=(*extra_groups, *(project_group_name(project_id) for project_id in project_ids)),
        groups_overage=groups_overage,
    )


def _scope(project_id: str = DEFAULT_PROJECT_ID, tenant_id: str = "demo") -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, project_id=project_id)


def _params(project_id: str = DEFAULT_PROJECT_ID, /, **kwargs: Any) -> dict[str, Any]:
    return {"project_id": project_id, **kwargs}


def _body(project_id: str = DEFAULT_PROJECT_ID, /, **kwargs: Any) -> dict[str, Any]:
    return {"project_id": project_id, **kwargs}


PLATFORM_OWNER_HEADERS = _project_headers(
    tenant_id="demo",
    user_id="platform-owner",
    project_ids=(DEFAULT_PROJECT_ID, OTHER_PROJECT_ID),
    extra_groups=("research-admins",),
)
USER_HEADERS = _project_headers(tenant_id="demo", user_id="user-1", project_ids=(DEFAULT_PROJECT_ID,))
MULTI_PROJECT_USER_HEADERS = _project_headers(
    tenant_id="demo",
    user_id="user-1",
    project_ids=(DEFAULT_PROJECT_ID, OTHER_PROJECT_ID),
)
VIEWER_HEADERS = _project_headers(tenant_id="demo", user_id="viewer-1", project_ids=(DEFAULT_PROJECT_ID,))
READER_HEADERS = _project_headers(tenant_id="demo", user_id="reader-1", project_ids=(DEFAULT_PROJECT_ID,))
OTHER_TENANT_HEADERS = _project_headers(
    tenant_id="other-tenant",
    user_id="other-user",
    project_ids=(DEFAULT_PROJECT_ID, OTHER_PROJECT_ID),
    extra_groups=("research-admins",),
)


def _build_app(
    settings: Settings,
    *,
    store: AgentStudioStore | None,
    registry: CapabilityRegistry,
    model_discovery: ModelDiscovery,
    release_service: ReleaseService | None,
    deployment_service: DeploymentService | None,
    memory_service: MemoryService | None,
    builder_service: BuilderService | None,
    audit_service: AuditService | None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(agent_studio_router)
    app.state.settings = settings
    app.state.agent_studio_store = store
    app.state.agent_studio_registry = registry
    app.state.agent_studio_model_discovery = model_discovery
    app.state.agent_studio_release_service = release_service
    app.state.agent_studio_deployment_service = deployment_service
    app.state.agent_studio_memory_service = memory_service
    app.state.agent_studio_builder_service = builder_service
    app.state.agent_studio_audit_service = audit_service
    # Mirrors app.py's composition root: the durable approval-consumption
    # port is only available when backed by real persistence.
    app.state.agent_studio_approval_consumption_port = (
        StoreBackedApprovalConsumptionPort(store) if store is not None else None
    )
    # Mirrors app.py's composition root: the durable idempotency port is
    # only available when backed by real persistence.
    app.state.agent_studio_idempotency_port = StoreBackedIdempotencyPort(store) if store is not None else None
    # Mirrors app.py's composition root: the approval-context resolver and
    # release-attestation port are only available when backed by real
    # persistence.
    app.state.agent_studio_approval_context_resolver = (
        StoreBackedApprovalContextResolver(store) if store is not None else None
    )
    app.state.agent_studio_release_attestation_port = (
        StoreBackedReleaseAttestationPort(store) if store is not None else None
    )
    return app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        trust_platform_identity_headers=True,
        allow_demo_identity=True,
        workspace_tenant_id="demo",
    )


@pytest.fixture
def locked_settings() -> Settings:
    return Settings(
        trust_platform_identity_headers=False,
        allow_demo_identity=False,
        workspace_tenant_id="demo",
    )


@pytest.fixture
def store() -> AgentStudioStore:
    return AgentStudioStore()


@pytest.fixture
def registry() -> CapabilityRegistry:
    return seeded_test_registry()


@pytest.fixture
def release_service(store: AgentStudioStore, registry: CapabilityRegistry) -> ReleaseService:
    return ReleaseService(store, registry)


@pytest.fixture
def deployment_service(store: AgentStudioStore) -> DeploymentService:
    return DeploymentService(store)


@pytest.fixture
def memory_service() -> MemoryService:
    return MemoryService(InMemoryMemoryStore())


@pytest.fixture
def builder_service(store: AgentStudioStore, release_service: ReleaseService) -> BuilderService:
    def _transform(manifest: AgentManifest, message: str) -> ProposedManifestChange:
        return ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"description": message}),
            generator="test-builder-generator",
        )

    return BuilderService(
        store, InMemoryManifestProposalGenerator(_transform), InMemoryArtifactBundleStore(), release_service
    )


@pytest.fixture
def audit_service() -> AuditService:
    return AuditService(InMemoryAuditStore())


@pytest.fixture
def model_discovery() -> InMemoryModelDiscovery:
    return InMemoryModelDiscovery(
        (
            ModelDeploymentRef(
                deployment_name="gpt-4o-prod",
                model_name="gpt-4o",
                model_format="OpenAI",
                capacity=2,
            ),
        )
    )


@pytest.fixture
def client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unavailable_client(settings: Settings, registry: CapabilityRegistry) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=None,
        registry=registry,
        model_discovery=UnavailableModelDiscovery(),
        release_service=None,
        deployment_service=None,
        memory_service=None,
        builder_service=None,
        audit_service=None,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def memory_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=None,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def approval_consumption_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    # Simulates a composition root where Cosmos-backed persistence exists
    # but the approval-consumption adapter itself was not wired -- distinct
    # from ``unavailable_client`` (no persistence at all), since ``_store``
    # succeeds here and the 503 must come specifically from
    # ``_approval_consumption_port``.
    app.state.agent_studio_approval_consumption_port = None
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def idempotency_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    # Simulates a composition root where Cosmos-backed persistence exists
    # but the idempotency adapter itself was not wired -- distinct from
    # ``unavailable_client`` (no persistence at all), since ``_store``
    # succeeds here and the 503 must come specifically from
    # ``_idempotency_port``.
    app.state.agent_studio_idempotency_port = None
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def approval_context_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    # Simulates a composition root where Cosmos-backed persistence exists
    # but the approval-context resolver itself was not wired -- distinct
    # from ``unavailable_client``, since ``_store`` succeeds here and the
    # 503 must come specifically from ``_approval_context_resolver``.
    app.state.agent_studio_approval_context_resolver = None
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def release_attestation_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    # Simulates a composition root where Cosmos-backed persistence exists
    # but the release-attestation port itself was not wired -- distinct
    # from ``unavailable_client``, since ``_store`` succeeds here and the
    # 503 must come specifically from ``_release_attestation_port``.
    app.state.agent_studio_release_attestation_port = None
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def audit_unavailable_client(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=None,
    )
    # Simulates a composition root where Cosmos-backed persistence exists
    # but the audit store itself was not wired -- distinct from
    # ``unavailable_client``, since ``_store`` succeeds here and the 503
    # must come specifically from ``_audit_service``. Exercises the
    # fail-closed policy: every consequential mutation route must refuse
    # to proceed (503) rather than mutate state with no audit trail.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unauthenticated_client(
    locked_settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> Iterator[TestClient]:
    app = _build_app(
        locked_settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(app) as test_client:
        yield test_client


def _minimal_manifest(
    *,
    logical_agent_id: str,
    tenant_id: str = "demo",
    owner_id: str = "platform-owner",
    owner_kind: AgentOwnerKind = AgentOwnerKind.USER,
    display_name: str = "Minimal Agent",
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    manifest = AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        display_name=display_name,
        owner_kind=owner_kind,
        owner_id=owner_id,
    )
    return manifest.model_dump(mode="json")


def _create_agent(
    client: TestClient,
    *,
    logical_agent_id: str,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
    owner_kind: str = "user",
    display_name: str = "Router Test Agent",
    description: str = "",
) -> dict[str, Any]:
    response = client.post(
        "/v1/agent-studio/agents",
        json={
            "logical_agent_id": logical_agent_id,
            "project_id": project_id,
            "display_name": display_name,
            "description": description,
            "owner_kind": owner_kind,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def _get_draft(
    client: TestClient,
    logical_agent_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    """Fetch the raw draft, unwrapping the ``{draft, capability_views}`` sidecar.

    Most tests only need the raw draft body; see
    ``test_draft_view_includes_capability_views_sidecar`` for a test that
    inspects the ``capability_views`` sidecar itself.
    """
    response = client.get(
        f"/v1/agent-studio/agents/{logical_agent_id}/draft",
        params={"project_id": project_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json()["draft"])


def _update_manifest(
    client: TestClient,
    logical_agent_id: str,
    manifest: dict[str, Any],
    *,
    headers: dict[str, str] = USER_HEADERS,
    if_match: str | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    if if_match is None:
        current = client.get(
            f"/v1/agent-studio/agents/{logical_agent_id}/draft",
            params={"project_id": manifest.get("project_id", DEFAULT_PROJECT_ID)},
            headers=headers,
        )
        if_match = cast("str", current.json()["draft"]["etag"]) if current.status_code == 200 else "missing-draft-etag"
    response = client.put(
        f"/v1/agent-studio/agents/{logical_agent_id}/draft",
        json={"manifest": manifest},
        headers={**headers, "If-Match": if_match},
    )
    assert response.status_code == expected_status, response.text
    return cast("dict[str, Any]", response.json())


def _grant_role(
    store: AgentStudioStore,
    *,
    logical_agent_id: str,
    principal_id: str,
    role: AgentRole,
    project_id: str = DEFAULT_PROJECT_ID,
    tenant_id: str = "demo",
) -> None:
    store.grant_ownership(
        _scope(project_id, tenant_id),
        OwnershipGrant(
            tenant_id=tenant_id,
            logical_agent_id=logical_agent_id,
            principal_id=principal_id,
            role=role,
            granted_by="platform-owner",
            project_id=project_id,
        )
    )


def _cut_version(
    client: TestClient,
    logical_agent_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    response = client.post(
        f"/v1/agent-studio/agents/{logical_agent_id}/versions",
        params={"project_id": project_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def _run_gates(
    client: TestClient,
    version_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/v1/agent-studio/versions/{version_id}/gates",
        json={"project_id": project_id, **(evidence or GATED_EVIDENCE)},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def _cut_gated_version(
    client: TestClient,
    logical_agent_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    version = _cut_version(client, logical_agent_id, headers=headers, project_id=project_id)
    report = _run_gates(client, version["id"], headers=headers, project_id=project_id, evidence=evidence)
    assert all(
        result["status"] in {"passed", "not_applicable"}
        for result in report["results"]
    ), report
    return version


def _deploy_version(
    client: TestClient,
    *,
    logical_agent_id: str,
    version_id: str,
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
    trace_ref: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"project_id": project_id, "version_id": version_id}
    if trace_ref is not None:
        payload["trace_ref"] = trace_ref
    response = client.post(
        f"/v1/agent-studio/agents/{logical_agent_id}/deployments",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def _enable_memory_scope(
    client: TestClient,
    logical_agent_id: str,
    *,
    scope_kind: str = "conversation",
    mechanism: str = "application_memory_store",
    headers: dict[str, str] = USER_HEADERS,
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    draft = _get_draft(client, logical_agent_id, headers=headers, project_id=project_id)
    draft["manifest"]["memory_policy"] = {
        "scopes": [{"kind": scope_kind, "enabled": True, "mechanism": mechanism}],
    }
    return _update_manifest(
        client,
        logical_agent_id,
        draft["manifest"],
        headers=headers,
    )


def test_router_requires_authentication_when_demo_identity_is_disabled(
    unauthenticated_client: TestClient,
) -> None:
    response = unauthenticated_client.get("/v1/agent-studio/capabilities/descriptors")
    assert response.status_code == 401
    assert "authenticated platform identity" in response.json()["detail"]


def test_list_capabilities_and_attach_cover_catalog_and_new_request_shape(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    list_response = client.get(
        "/v1/agent-studio/capabilities/descriptors",
        headers=USER_HEADERS,
    )
    assert list_response.status_code == 200
    ids = {item["id"] for item in list_response.json()}
    assert {"foundry.web_search", "foundry.memory"}.issubset(ids)

    registry.register_instance(
        CapabilityInstance(
            id="search-instance-1",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            discovered_provider_version="2026-07-01",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )

    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_ai_search",
            "operation": "search",
            "instance_id": "search-instance-1",
            "connection_ref": "conn://azure-ai-search",
            "policy_ref": "policy://grounding",
            "config": {"top_k": 5},
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    body = attach_response.json()
    assert body["descriptor_ref"]["id"] == "foundry.azure_ai_search"
    assert body["operation_ref"]["id"] == "search"
    assert body["instance_ref"]["id"] == "search-instance-1"
    assert body["connection_ref"]["id"] == "conn://azure-ai-search"
    assert body["policy_ref"]["id"] == "policy://grounding"
    assert body["instance_ref"]["discovered_version"] == "2026-07-01"
    assert body["attached_by"] == "user-1"


def test_list_capability_instances_is_tenant_and_project_scoped(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    registry.register_instance(
        CapabilityInstance(
            id="demo-default-instance",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )
    registry.register_instance(
        CapabilityInstance(
            id="demo-other-project-instance",
            tenant_id="demo",
            project_id="other-project",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )
    registry.register_instance(
        CapabilityInstance(
            id="other-tenant-instance",
            tenant_id="other-tenant",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )

    all_for_tenant = client.get("/v1/agent-studio/capabilities/instances", headers=USER_HEADERS)
    assert all_for_tenant.status_code == 200
    ids = {item["id"] for item in all_for_tenant.json()}
    assert ids == {"demo-default-instance", "demo-other-project-instance"}

    scoped_to_project = client.get(
        "/v1/agent-studio/capabilities/instances",
        params={"project_id": "default"},
        headers=USER_HEADERS,
    )
    assert scoped_to_project.status_code == 200
    assert {item["id"] for item in scoped_to_project.json()} == {"demo-default-instance"}

    other_tenant = client.get("/v1/agent-studio/capabilities/instances", headers=OTHER_TENANT_HEADERS)
    assert other_tenant.status_code == 200
    assert {item["id"] for item in other_tenant.json()} == {"other-tenant-instance"}


def test_capability_discovery_combines_descriptors_instances_and_warnings(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    registry.register_instance(
        CapabilityInstance(
            id="discovery-instance-1",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )

    response = client.get("/v1/agent-studio/capabilities/discovery", headers=USER_HEADERS)
    assert response.status_code == 200
    body = response.json()
    descriptor_ids = {item["id"] for item in body["descriptors"]}
    assert {"foundry.web_search", "foundry.memory"}.issubset(descriptor_ids)
    assert {item["id"] for item in body["instances"]} == {"discovery-instance-1"}
    assert body["warnings"] == list(registry.warnings)
    assert body["refreshed_at"]

    scoped_response = client.get(
        "/v1/agent-studio/capabilities/discovery",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert scoped_response.status_code == 200
    assert {item["id"] for item in scoped_response.json()["instances"]} == {"discovery-instance-1"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "descriptor_id": "foundry.web_search",
                "operation": "search",
                "workspace_connection_id": "legacy-connection",
            },
            "extra_forbidden",
        ),
        (
            {"descriptor_id": "missing.capability", "operation": "search"},
            "not in the catalog",
        ),
        (
            {"descriptor_id": "foundry.web_search", "operation": "missing"},
            "has no operation",
        ),
        (
            {"descriptor_id": "foundry.memory", "operation": "recall"},
            "preview",
        ),
        (
            {
                "descriptor_id": "foundry.web_search",
                "operation": "search",
                "instance_id": "missing-instance",
            },
            "not registered",
        ),
    ],
)
def test_attach_capability_rejects_invalid_payloads(
    client: TestClient,
    payload: dict[str, Any],
    message: str,
) -> None:
    response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json=payload,
        headers=USER_HEADERS,
    )
    assert response.status_code == 422
    assert message in response.text


def test_attach_capability_rejects_unavailable_registered_instance(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    registry.register_instance(
        CapabilityInstance(
            id="offline-instance",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.UNAVAILABLE,
            unavailable_reason="index deleted",
            registered_by="platform-owner",
        )
    )
    response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_ai_search",
            "operation": "search",
            "instance_id": "offline-instance",
        },
        headers=USER_HEADERS,
    )
    assert response.status_code == 422
    assert "index deleted" in response.json()["detail"]


def test_get_agent_manifest_schema_returns_canonical_digest(client: TestClient) -> None:
    response = client.get(
        "/v1/agent-studio/schemas/agent-manifest",
        headers=USER_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    canonical = json.dumps(
        AgentManifest.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert body["schema_version"] == AGENT_MANIFEST_SCHEMA_VERSION
    assert body["digest"] == f"sha256:{expected_digest}"
    assert body["json_schema"]["title"] == "AgentManifest"


def test_list_deployed_models_and_unavailable_discovery(
    client: TestClient,
    unavailable_client: TestClient,
) -> None:
    ok_response = client.get("/v1/agent-studio/models", headers=USER_HEADERS)
    assert ok_response.status_code == 200
    assert ok_response.json() == [
        {
            "deployment_name": "gpt-4o-prod",
            "model_name": "gpt-4o",
            "model_format": "OpenAI",
            "capacity": 2,
        }
    ]

    unavailable_response = unavailable_client.get(
        "/v1/agent-studio/models",
        headers=USER_HEADERS,
    )
    assert unavailable_response.status_code == 503
    assert "model discovery is unavailable" in unavailable_response.json()["detail"]


def test_create_agent_covers_validation_authz_and_duplicates(client: TestClient) -> None:
    bad_response = client.post(
        "/v1/agent-studio/agents",
        json={"logical_agent_id": "bad", "display_name": "Bad"},
        headers=USER_HEADERS,
    )
    assert bad_response.status_code == 422

    user_agent = _create_agent(client, logical_agent_id="agent-create-user", headers=USER_HEADERS)
    assert user_agent["manifest"]["owner_kind"] == "user"
    assert user_agent["manifest"]["owner_id"] == "user-1"
    assert user_agent["manifest"]["project_id"] == "default"

    system_response = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            PLATFORM_PROJECT_ID,
            logical_agent_id="agent-create-system",
            display_name="System Agent",
            owner_kind="system",
        ),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert system_response.status_code == 201
    assert system_response.json()["manifest"]["owner_kind"] == "system"

    forbidden_response = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            PLATFORM_PROJECT_ID,
            logical_agent_id="agent-forbidden-system",
            display_name="Forbidden",
            owner_kind="system",
        ),
        headers=VIEWER_HEADERS,
    )
    assert forbidden_response.status_code == 403

    duplicate_response = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            logical_agent_id="agent-create-user",
            display_name="Duplicate",
            owner_kind="user",
        ),
        headers=USER_HEADERS,
    )
    assert duplicate_response.status_code == 409


def test_create_agent_maps_release_service_authorization_errors(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> None:
    class RejectingReleaseService:
        def create_agent(self, **_: Any) -> Any:
            raise AuthorizationError("blocked by test")

    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=cast("ReleaseService", RejectingReleaseService()),
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/agent-studio/agents",
            json=_body(logical_agent_id="agent-create-rejected", display_name="Rejected"),
            headers=USER_HEADERS,
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "blocked by test"


def test_draft_routes_cover_get_update_and_missing_paths(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    create_body = _create_agent(client, logical_agent_id="agent-draft", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-draft", headers=USER_HEADERS)
    assert draft["etag"] == create_body["etag"]

    draft["manifest"]["display_name"] = "Updated Draft Name"
    draft["manifest"]["project_id"] = DEFAULT_PROJECT_ID
    draft["manifest"]["input_schema_ref"] = {
        "ref": "schema://input",
        "digest": "sha256:" + "1" * 64,
    }
    draft["manifest"]["output_schema_ref"] = {
        "ref": "schema://output",
        "digest": "sha256:" + "2" * 64,
    }
    updated = _update_manifest(
        client,
        "agent-draft",
        draft["manifest"],
        headers=USER_HEADERS,
    )
    assert updated["manifest"]["display_name"] == "Updated Draft Name"
    assert updated["manifest"]["project_id"] == DEFAULT_PROJECT_ID
    assert updated["manifest"]["input_schema_ref"]["ref"] == "schema://input"

    viewer_response = client.put(
        "/v1/agent-studio/agents/agent-draft/draft",
        json={"manifest": draft["manifest"]},
        headers={**VIEWER_HEADERS, "If-Match": updated["etag"]},
    )
    assert viewer_response.status_code == 403

    mismatch_manifest = dict(draft["manifest"])
    mismatch_manifest["logical_agent_id"] = "agent-other"
    mismatch_response = client.put(
        "/v1/agent-studio/agents/agent-draft/draft",
        json={"manifest": mismatch_manifest},
        headers={**USER_HEADERS, "If-Match": updated["etag"]},
    )
    assert mismatch_response.status_code == 404

    missing_draft_response = client.get(
        "/v1/agent-studio/agents/agent-missing/draft",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing_draft_response.status_code == 404

    _grant_role(
        store,
        logical_agent_id="agent-no-draft",
        principal_id="user-1",
        role=AgentRole.OWNER,
    )
    no_draft_response = client.put(
        "/v1/agent-studio/agents/agent-no-draft/draft",
        json={
            "manifest": _minimal_manifest(
                logical_agent_id="agent-no-draft",
                owner_id="user-1",
            )
        },
        headers={**USER_HEADERS, "If-Match": "irrelevant-etag"},
    )
    assert no_draft_response.status_code == 404


def test_update_draft_enforces_if_match_optimistic_concurrency(client: TestClient) -> None:
    """Review finding #6: PUT draft requires ``If-Match`` and rejects a
    stale/mismatched etag with 412, without mutating the stored draft."""
    _create_agent(client, logical_agent_id="agent-draft-etag", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-draft-etag", headers=USER_HEADERS)

    missing_header_response = client.put(
        "/v1/agent-studio/agents/agent-draft-etag/draft",
        json={"manifest": {**draft["manifest"], "display_name": "No Header"}},
        headers=USER_HEADERS,
    )
    assert missing_header_response.status_code == 422

    stale_response = client.put(
        "/v1/agent-studio/agents/agent-draft-etag/draft",
        json={"manifest": {**draft["manifest"], "display_name": "Stale Update"}},
        headers={**USER_HEADERS, "If-Match": "not-the-real-etag"},
    )
    assert stale_response.status_code == 412

    unchanged = _get_draft(client, "agent-draft-etag", headers=USER_HEADERS)
    assert unchanged["etag"] == draft["etag"]
    assert unchanged["manifest"]["display_name"] == draft["manifest"]["display_name"]

    first_editor_update = client.put(
        "/v1/agent-studio/agents/agent-draft-etag/draft",
        json={"manifest": {**draft["manifest"], "display_name": "First Editor"}},
        headers={**USER_HEADERS, "If-Match": draft["etag"]},
    )
    assert first_editor_update.status_code == 200
    first_editor_body = first_editor_update.json()
    assert first_editor_body["etag"] != draft["etag"]
    assert first_editor_body["manifest"]["display_name"] == "First Editor"

    second_editor_stale_update = client.put(
        "/v1/agent-studio/agents/agent-draft-etag/draft",
        json={"manifest": {**draft["manifest"], "display_name": "Second Editor Lost"}},
        headers={**USER_HEADERS, "If-Match": draft["etag"]},
    )
    assert second_editor_stale_update.status_code == 412

    final = _get_draft(client, "agent-draft-etag", headers=USER_HEADERS)
    assert final["manifest"]["display_name"] == "First Editor"


def test_create_agent_rejects_reserved_platform_project_id_for_non_system_owner(client: TestClient) -> None:
    # A platform owner cannot smuggle a USER-owned agent into the reserved
    # platform-wide project, even though they otherwise pass the platform
    # scope's authorization check -- the reserved id is not requestable
    # client-side unless the agent is truly system-owned.
    explicit_user_response = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            PLATFORM_PROJECT_ID,
            logical_agent_id="agent-reserved-user",
            display_name="Reserved User",
            owner_kind="user",
        ),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert explicit_user_response.status_code == 422
    assert PLATFORM_PROJECT_ID in explicit_user_response.json()["detail"]

    default_owner_kind_response = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            PLATFORM_PROJECT_ID,
            logical_agent_id="agent-reserved-default",
            display_name="Reserved Default",
        ),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert default_owner_kind_response.status_code == 422


def test_fork_agent_and_lineage_routes_cover_success_and_conflicts(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-fork-source", headers=USER_HEADERS)
    source_version = _cut_gated_version(client, "agent-fork-source", headers=USER_HEADERS)

    fork_response = client.post(
        "/v1/agent-studio/agents/agent-fork-source/fork",
        json=_body(
            source_version_id=source_version["id"],
            new_logical_agent_id="agent-fork-child",
        ),
        headers=VIEWER_HEADERS,
    )
    assert fork_response.status_code == 201, fork_response.text
    assert fork_response.json()["based_on_version_id"] == source_version["id"]
    assert fork_response.json()["manifest"]["visibility"] == "private"

    child_version = _cut_version(client, "agent-fork-child", headers=VIEWER_HEADERS)
    assert child_version["fork_of_version_id"] == source_version["id"]

    lineage_response = client.get(
        "/v1/agent-studio/agents/agent-fork-child/lineage",
        params=_params(),
        headers=VIEWER_HEADERS,
    )
    assert lineage_response.status_code == 200
    assert lineage_response.json() == [
        {
            "tenant_id": "demo",
            "project_id": DEFAULT_PROJECT_ID,
            "child_logical_agent_id": "agent-fork-child",
            "child_version_id": child_version["id"],
            "parent_logical_agent_id": "agent-fork-source",
            "parent_version_id": source_version["id"],
            "relationship": "fork",
        }
    ]

    missing_source_response = client.post(
        "/v1/agent-studio/agents/agent-fork-source/fork",
        json=_body(
            source_version_id="missing-version",
            new_logical_agent_id="agent-fork-missing",
        ),
        headers=USER_HEADERS,
    )
    assert missing_source_response.status_code == 409

    _create_agent(client, logical_agent_id="agent-fork-existing", headers=USER_HEADERS)
    duplicate_response = client.post(
        "/v1/agent-studio/agents/agent-fork-source/fork",
        json=_body(
            source_version_id=source_version["id"],
            new_logical_agent_id="agent-fork-existing",
        ),
        headers=USER_HEADERS,
    )
    assert duplicate_response.status_code == 409


def test_fork_agent_rejects_reserved_platform_project_id(client: TestClient) -> None:
    # ``fork`` always produces a USER-owned draft, so the reserved
    # platform-wide project must never be an acceptable fork target --
    # even for a platform owner who would otherwise pass ``_scope``'s
    # platform-owner authorization check.
    _create_agent(client, logical_agent_id="agent-fork-reserved-source", headers=USER_HEADERS)
    source_version = _cut_gated_version(client, "agent-fork-reserved-source", headers=USER_HEADERS)

    forbidden_response = client.post(
        "/v1/agent-studio/agents/agent-fork-reserved-source/fork",
        json=_body(
            PLATFORM_PROJECT_ID,
            source_version_id=source_version["id"],
            new_logical_agent_id="agent-fork-reserved-child",
        ),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert forbidden_response.status_code == 422
    assert PLATFORM_PROJECT_ID in forbidden_response.json()["detail"]


def test_tool_registration_routes_cover_success_role_and_maturity(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tools", headers=USER_HEADERS)

    create_response = client.post(
        "/v1/agent-studio/agents/agent-tools/tool-registrations",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            kind="managed_foundry_native",
            handler_ref="builtin://web-search",
        ),
        headers=USER_HEADERS,
    )
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["logical_agent_id"] == "agent-tools"

    list_response = client.get(
        "/v1/agent-studio/agents/agent-tools/tool-registrations",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    preview_response = client.post(
        "/v1/agent-studio/agents/agent-tools/tool-registrations",
        json=_body(
            descriptor_id="foundry.memory",
            operation="recall",
            kind="custom_handler",
            handler_ref="custom://memory",
        ),
        headers=USER_HEADERS,
    )
    assert preview_response.status_code == 422

    viewer_response = client.post(
        "/v1/agent-studio/agents/agent-tools/tool-registrations",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            kind="managed_foundry_native",
            handler_ref="builtin://web-search",
        ),
        headers=VIEWER_HEADERS,
    )
    assert viewer_response.status_code == 403

    empty_agent = _create_agent(client, logical_agent_id="agent-tools-empty", headers=USER_HEADERS)
    assert empty_agent["logical_agent_id"] == "agent-tools-empty"
    empty_list_response = client.get(
        "/v1/agent-studio/agents/agent-tools-empty/tool-registrations",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert empty_list_response.status_code == 200
    assert empty_list_response.json() == []


def test_list_tool_registrations_route_fails_closed_when_release_service_unavailable(
    unavailable_client: TestClient,
) -> None:
    """``list_tool_registrations`` resolves ``_release_service`` directly
    (it has no draft/version precondition to check via ``_store`` first), so
    it must independently fail closed with 503 when persistence is
    unavailable, exercising that branch of ``_release_service`` on its own.
    """
    response = unavailable_client.get(
        "/v1/agent-studio/agents/agent-tools/tool-registrations",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_version_and_gate_routes_cover_success_and_error_branches(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-versioned", headers=USER_HEADERS)

    version = _cut_version(client, "agent-versioned", headers=USER_HEADERS)
    assert version["sequence"] == 1
    assert version["protocol_version"]
    assert version["runtime_target"] == "managed_foundry"

    list_response = client.get(
        "/v1/agent-studio/agents/agent-versioned/versions",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [version["id"]]

    lineage_response = client.get(
        "/v1/agent-studio/agents/agent-versioned/lineage",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert lineage_response.status_code == 200
    assert lineage_response.json() == []

    missing_gates_response = client.post(
        "/v1/agent-studio/versions/missing/gates",
        json=_body(evidence={}),
        headers=USER_HEADERS,
    )
    assert missing_gates_response.status_code == 404

    failed_report = _run_gates(
        client,
        version["id"],
        headers=USER_HEADERS,
        evidence={"evidence": {}},
    )
    assert any(result["status"] != "passed" for result in failed_report["results"])

    passing_report = _run_gates(client, version["id"], headers=USER_HEADERS)
    assert all(
        result["status"] in {"passed", "not_applicable"}
        for result in passing_report["results"]
    )

    viewer_gates_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/gates",
        json=_body(**GATED_EVIDENCE),
        headers=VIEWER_HEADERS,
    )
    assert viewer_gates_response.status_code == 403

    viewer_cut_response = client.post(
        "/v1/agent-studio/agents/agent-versioned/versions",
        params=_params(),
        headers=VIEWER_HEADERS,
    )
    assert viewer_cut_response.status_code == 403


def test_run_gates_maps_service_release_error_to_404(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> None:
    """The route's own existence check makes the service's identical
    ``ReleaseServiceError`` (raised inside ``run_release_gates`` when the
    version is missing) unreachable in the ordinary case. Simulate the
    version disappearing between the route's check and the service call
    (a true TOCTOU race) to confirm the handler still maps that error to
    404 instead of letting it bubble up as an unhandled 500."""

    setup_app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=ReleaseService(store, registry),
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(setup_app) as setup_client:
        _create_agent(setup_client, logical_agent_id="agent-gate-race", headers=USER_HEADERS)
        version = _cut_version(setup_client, "agent-gate-race", headers=USER_HEADERS)

    class _RacingReleaseService(ReleaseService):
        def run_release_gates(
            self,
            *,
            tenant_id: str,
            project_id: str,
            version_id: str,
            actor_id: str,
            actor_role: AgentRole,
            evidence: GateEvidence,
        ) -> ReleaseGateReport:
            raise ReleaseServiceError(f"Version '{version_id}' not found.")

    racing_app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=_RacingReleaseService(store, registry),
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(racing_app) as racing_client:
        response = racing_client.post(
            f"/v1/agent-studio/versions/{version['id']}/gates",
            json=_body(evidence={}),
            headers=USER_HEADERS,
        )
    assert response.status_code == 404


def test_cut_version_returns_404_when_granted_actor_has_no_draft(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _grant_role(
        store,
        logical_agent_id="agent-cut-no-draft",
        principal_id="user-1",
        role=AgentRole.OWNER,
    )
    response = client.post(
        "/v1/agent-studio/agents/agent-cut-no-draft/versions",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


def test_promotion_and_approval_routes_cover_auto_and_pending_paths(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-promotion", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-promotion", headers=USER_HEADERS)

    missing_response = client.post(
        "/v1/agent-studio/versions/missing/promote",
        json=_body(destination="dev", evidence_summary="ok"),
        headers=USER_HEADERS,
    )
    assert missing_response.status_code == 404

    viewer_promote_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="viewers cannot promote"),
        headers=VIEWER_HEADERS,
    )
    assert viewer_promote_response.status_code == 403

    auto_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="ship it"),
        headers=USER_HEADERS,
    )
    assert auto_response.status_code == 200
    assert auto_response.json()["id"] == version["id"]
    # Promotion stops at APPROVED; ACTIVE requires a separate explicit
    # /activate call gated on a healthy deploy+smoke record.
    approved_release = store.latest_release_for_version(_scope(), version["id"])
    assert approved_release is not None
    assert approved_release.status is ReleaseStatus.APPROVED

    _create_agent(client, logical_agent_id="agent-promotion-pending", headers=USER_HEADERS)
    pending_version = _cut_gated_version(
        client,
        "agent-promotion-pending",
        headers=USER_HEADERS,
    )
    _grant_role(
        store,
        logical_agent_id="agent-promotion-pending",
        principal_id="contributor-1",
        role=AgentRole.CONTRIBUTOR,
    )
    contributor_headers = _project_headers(
        tenant_id="demo",
        user_id="contributor-1",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    pending_response = client.post(
        f"/v1/agent-studio/versions/{pending_version['id']}/promote",
        json=_body(
            destination="dev",
            evidence_summary="needs maintainer approval",
            risk="medium",
        ),
        headers=contributor_headers,
    )
    assert pending_response.status_code == 200
    pending_body = pending_response.json()
    assert pending_body["kind"] == "release_promotion"
    assert pending_body["state"] == "pending"
    assert pending_body["environment"] == "development"

    viewer_decision = client.post(
        f"/v1/agent-studio/approvals/{pending_body['id']}/decision",
        json=_body(approve=True),
        headers=VIEWER_HEADERS,
    )
    assert viewer_decision.status_code == 409

    decision = client.post(
        f"/v1/agent-studio/approvals/{pending_body['id']}/decision",
        json=_body(approve=True, rationale="approved"),
        headers=USER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"
    # decide_promotion (approval path) also only reaches APPROVED.
    approved_via_decision = store.latest_release_for_version(_scope(), pending_version["id"])
    assert approved_via_decision is not None
    assert approved_via_decision.status is ReleaseStatus.APPROVED

    decided_again = client.post(
        f"/v1/agent-studio/approvals/{pending_body['id']}/decision",
        json=_body(approve=False),
        headers=USER_HEADERS,
    )
    assert decided_again.status_code == 409

    ghost_response = client.post(
        "/v1/agent-studio/approvals/missing-approval/decision",
        json=_body(approve=True),
        headers=USER_HEADERS,
    )
    assert ghost_response.status_code == 404


def test_promotion_routes_cover_ungated_and_missing_version_decision_path(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-ungated", headers=USER_HEADERS)
    ungated_version = _cut_version(client, "agent-ungated", headers=USER_HEADERS)
    ungated_response = client.post(
        f"/v1/agent-studio/versions/{ungated_version['id']}/promote",
        json=_body(destination="dev", evidence_summary="not gated"),
        headers=USER_HEADERS,
    )
    assert ungated_response.status_code == 409

    store.create_approval(
        _scope(),
        StudioApprovalRecord(
            id="ghost-approval",
            version_id="ghost-version",
            tenant_id="demo",
            project_id=DEFAULT_PROJECT_ID,
            kind=ApprovalKind.RELEASE_PROMOTION,
            gated_action="promote_version",
            destination="dev",
            requested_by="user-1",
            evidence_summary="ghost",
            risk="medium",
            idempotency_key="ghost-key",
        )
    )
    response = client.post(
        "/v1/agent-studio/approvals/ghost-approval/decision",
        json=_body(approve=True),
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


def test_capability_approval_routes_gate_release_until_approved(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-capability-approval", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-capability-approval",
        principal_id="requester-1",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo",
        user_id="requester-1",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_functions",
            "operation": "invoke",
            "connection_ref": "conn-azure-functions",
            "policy_ref": "policy.capability-approval.write-irreversible.v1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()

    draft = _get_draft(client, "agent-capability-approval", headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, "agent-capability-approval", draft["manifest"], headers=USER_HEADERS)

    version = _cut_version(client, "agent-capability-approval", headers=USER_HEADERS)

    missing_version_request = client.post(
        "/v1/agent-studio/versions/missing/capability-approvals",
        json=_body(
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            evidence_summary="reviewed",
        ),
        headers=USER_HEADERS,
    )
    assert missing_version_request.status_code == 404

    gated_before_approval = _run_gates(
        client,
        version["id"],
        headers=USER_HEADERS,
        evidence=GATED_EVIDENCE,
    )
    approval_gate = next(r for r in gated_before_approval["results"] if r["name"] == "approval")
    assert approval_gate["status"] == "failed"

    request_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/capability-approvals",
        json=_body(
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            evidence_summary="Reviewed the destination and scopes.",
        ),
        headers=requester_headers,
    )
    assert request_response.status_code == 200, request_response.text
    approval = request_response.json()
    assert approval["kind"] == "capability_operation"
    assert approval["state"] == "pending"
    assert approval["destination"] == "foundry.azure_functions.invoke"

    bad_descriptor_request = client.post(
        f"/v1/agent-studio/versions/{version['id']}/capability-approvals",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            evidence_summary="no such binding on this version",
        ),
        headers=USER_HEADERS,
    )
    assert bad_descriptor_request.status_code == 409

    viewer_decision = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True),
        headers=VIEWER_HEADERS,
    )
    assert viewer_decision.status_code == 409

    decision = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True, rationale="approved for release"),
        headers=USER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"

    gated_after_approval = _run_gates(
        client,
        version["id"],
        headers=USER_HEADERS,
        evidence=GATED_EVIDENCE,
    )
    approval_gate_after = next(r for r in gated_after_approval["results"] if r["name"] == "approval")
    assert approval_gate_after["status"] == "passed"

    decided_again = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=False),
        headers=USER_HEADERS,
    )
    assert decided_again.status_code == 409


def _setup_approved_capability_approval(
    client: TestClient,
    store: AgentStudioStore,
    *,
    logical_agent_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create an agent with an attached capability, cut a version, request a
    ``CAPABILITY_OPERATION`` approval for it, and approve it. Returns
    ``(binding, approval)`` as decoded JSON bodies.
    """
    _create_agent(client, logical_agent_id=logical_agent_id, headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id=logical_agent_id,
        principal_id="consume-requester",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo",
        user_id="consume-requester",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_functions",
            "operation": "invoke",
            "connection_ref": "conn-azure-functions",
            "policy_ref": "policy.capability-approval.write-irreversible.v1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()

    draft = _get_draft(client, logical_agent_id, headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, logical_agent_id, draft["manifest"], headers=USER_HEADERS)

    version = _cut_version(client, logical_agent_id, headers=USER_HEADERS)

    request_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/capability-approvals",
        json=_body(
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            evidence_summary="Reviewed the destination and scopes.",
        ),
        headers=requester_headers,
    )
    assert request_response.status_code == 200, request_response.text
    approval = request_response.json()

    decision = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True, rationale="approved for consumption"),
        headers=USER_HEADERS,
    )
    assert decision.status_code == 200, decision.text

    return binding, approval


def _consume_body(
    project_id: str = DEFAULT_PROJECT_ID,
    /,
    *,
    binding_id: str,
    operation_id: str = "invoke",
    invocation_id: str = "invocation-1",
    idempotency_key: str = "idem-1",
    **kwargs: Any,
) -> dict[str, Any]:
    return _body(
        project_id,
        binding_id=binding_id,
        operation_id=operation_id,
        args_hash="hash-args-1",
        destination_hash="hash-dest-1",
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        **kwargs,
    )


def test_consume_approval_route_consumes_once_then_reconciles_then_exhausts(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    binding, approval = _setup_approved_capability_approval(
        client, store, logical_agent_id="agent-consume-approval"
    )

    first = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["outcome"] == "consumed"
    assert first_body["record"] is not None
    assert first_body["record"]["binding_id"] == binding["binding_id"]
    assert first_body["record"]["invocation_id"] == "invocation-1"
    assert first_body["record"]["principal_id"] == "user-1"

    # Same invocation retrying (identical idempotency_key) reconciles to the
    # original durable record rather than re-consuming or being denied.
    retry = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert retry.status_code == 200, retry.text
    retry_body = retry.json()
    assert retry_body["outcome"] == "already_consumed"
    assert retry_body["record"]["id"] == first_body["record"]["id"]

    # A different invocation attempting to reuse the same single-use
    # approval is denied even though the approval is still "approved".
    reused = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(
            binding_id=binding["binding_id"],
            invocation_id="invocation-2",
            idempotency_key="idem-2",
        ),
        headers=USER_HEADERS,
    )
    assert reused.status_code == 200, reused.text
    reused_body = reused.json()
    assert reused_body["outcome"] == "exhausted"
    assert reused_body["record"]["id"] == first_body["record"]["id"]
    assert reused_body["reason"] is not None


def test_consume_approval_route_denies_when_not_approved_or_binding_mismatch(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-consume-pending", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-consume-pending",
        principal_id="pending-requester",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo",
        user_id="pending-requester",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_functions",
            "operation": "invoke",
            "connection_ref": "conn-azure-functions",
            "policy_ref": "policy.capability-approval.write-irreversible.v1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()

    draft = _get_draft(client, "agent-consume-pending", headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, "agent-consume-pending", draft["manifest"], headers=USER_HEADERS)
    version = _cut_version(client, "agent-consume-pending", headers=USER_HEADERS)

    request_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/capability-approvals",
        json=_body(
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            evidence_summary="still under review",
        ),
        headers=requester_headers,
    )
    assert request_response.status_code == 200, request_response.text
    approval = request_response.json()

    still_pending = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert still_pending.status_code == 200, still_pending.text
    still_pending_body = still_pending.json()
    assert still_pending_body["outcome"] == "denied"
    assert still_pending_body["record"] is None

    decision = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True, rationale="approved"),
        headers=USER_HEADERS,
    )
    assert decision.status_code == 200, decision.text

    wrong_binding = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id="not-a-real-binding-id"),
        headers=USER_HEADERS,
    )
    assert wrong_binding.status_code == 200, wrong_binding.text
    wrong_binding_body = wrong_binding.json()
    assert wrong_binding_body["outcome"] == "denied"
    assert wrong_binding_body["record"] is None


def test_consume_approval_route_is_not_found_for_missing_or_cross_scope_approval(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    missing = client.post(
        "/v1/agent-studio/approvals/missing-approval/consume",
        json=_consume_body(binding_id="whatever"),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404

    binding, approval = _setup_approved_capability_approval(
        client, store, logical_agent_id="agent-consume-cross-scope"
    )

    cross_project = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(OTHER_PROJECT_ID, binding_id=binding["binding_id"]),
        headers=MULTI_PROJECT_USER_HEADERS,
    )
    assert cross_project.status_code == 404

    cross_tenant = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant.status_code == 404


def test_consume_approval_route_returns_503_when_persistence_unavailable(
    unavailable_client: TestClient,
) -> None:
    response = unavailable_client.post(
        "/v1/agent-studio/approvals/missing-approval/consume",
        json=_consume_body(binding_id="whatever"),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_consume_approval_route_returns_503_when_consumption_port_unavailable(
    client: TestClient,
    approval_consumption_unavailable_client: TestClient,
    store: AgentStudioStore,
) -> None:
    binding, approval = _setup_approved_capability_approval(
        client, store, logical_agent_id="agent-consume-port-unavailable"
    )
    response = approval_consumption_unavailable_client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


# --- approval context resolution & release attestation routes --------------


def _setup_gated_release_with_capability_approval(
    client: TestClient,
    store: AgentStudioStore,
    *,
    logical_agent_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Like ``_setup_approved_capability_approval`` but additionally runs
    release gates so a real ``AgentRelease`` (GATED) exists.

    Returns ``(binding, approval, release)`` as decoded JSON /
    store-serialized bodies.
    """
    binding, approval = _setup_approved_capability_approval(client, store, logical_agent_id=logical_agent_id)
    version_id = approval["version_id"]
    report = _run_gates(client, version_id, headers=USER_HEADERS, evidence=GATED_EVIDENCE)
    assert all(
        result["status"] in {"passed", "not_applicable"} for result in report["results"]
    ), report
    release = store.latest_release_for_version(_scope(), version_id)
    assert release is not None
    return binding, approval, json.loads(release.model_dump_json())


def test_resolve_approval_context_route_resolves_and_can_be_consumed(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    binding, approval, release = _setup_gated_release_with_capability_approval(
        client, store, logical_agent_id="agent-context-resolve"
    )

    response = client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(
            release_id=release["id"],
            binding_id=binding["binding_id"],
            operation_id="invoke",
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "resolved"
    assert body["approval_id"] == approval["id"]
    assert body["invocation_id"] is not None
    assert body["invocation_id"].startswith("inv-")

    # The resolved context can actually be consumed -- proving there is no
    # new trust dependency: ``consume_approval_route`` independently
    # revalidates everything regardless of what ``resolve`` returned.
    consume_response = client.post(
        f"/v1/agent-studio/approvals/{body['approval_id']}/consume",
        json=_consume_body(
            binding_id=binding["binding_id"],
            invocation_id=body["invocation_id"],
            idempotency_key=f"idem-{body['invocation_id']}",
            release_id=release["id"],
        ),
        headers=USER_HEADERS,
    )
    assert consume_response.status_code == 200, consume_response.text
    assert consume_response.json()["outcome"] == "consumed"


def test_resolve_approval_context_route_returns_not_approved_before_decision(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-context-not-approved", headers=USER_HEADERS)
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_functions",
            "operation": "invoke",
            "connection_ref": "conn-azure-functions",
            "policy_ref": "policy.capability-approval.write-irreversible.v1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()
    draft = _get_draft(client, "agent-context-not-approved", headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, "agent-context-not-approved", draft["manifest"], headers=USER_HEADERS)
    version = _cut_version(client, "agent-context-not-approved", headers=USER_HEADERS)

    response = client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(
            release_id="no-release-yet",
            binding_id=binding["binding_id"],
            operation_id="invoke",
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "not_found"
    assert body["approval_id"] is None
    assert body["invocation_id"] is None
    assert version["id"]  # keep version referenced; no release exists yet


def test_resolve_approval_context_route_scopes_by_project_and_tenant(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    binding, _approval, release = _setup_gated_release_with_capability_approval(
        client, store, logical_agent_id="agent-context-cross-scope"
    )

    cross_project = client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(
            OTHER_PROJECT_ID,
            release_id=release["id"],
            binding_id=binding["binding_id"],
            operation_id="invoke",
        ),
        headers=MULTI_PROJECT_USER_HEADERS,
    )
    assert cross_project.status_code == 200, cross_project.text
    assert cross_project.json()["outcome"] == "not_found"

    cross_tenant = client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(
            release_id=release["id"],
            binding_id=binding["binding_id"],
            operation_id="invoke",
        ),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant.status_code == 200, cross_tenant.text
    assert cross_tenant.json()["outcome"] == "not_found"


def test_resolve_approval_context_route_rejects_unknown_fields() -> None:
    # approval_id/invocation_id/principal_id must never be accepted as
    # request input -- this is a schema-level contract test, not a route
    # test, so it does not need a client.
    from pydantic import ValidationError
    from research_assistant_api.agent_studio.schemas import ResolveApprovalContextRequest

    forged_payload: dict[str, Any] = {
        "project_id": DEFAULT_PROJECT_ID,
        "release_id": "release-1",
        "binding_id": "binding-1",
        "operation_id": "invoke",
        "approval_id": "forged-approval",
    }
    with pytest.raises(ValidationError):
        ResolveApprovalContextRequest(**forged_payload)


def test_resolve_approval_context_route_returns_503_when_persistence_unavailable(
    unavailable_client: TestClient,
) -> None:
    response = unavailable_client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(release_id="release-1", binding_id="binding-1", operation_id="invoke"),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_resolve_approval_context_route_returns_503_when_resolver_unavailable(
    approval_context_unavailable_client: TestClient,
) -> None:
    response = approval_context_unavailable_client.post(
        "/v1/agent-studio/approvals/context",
        json=_body(release_id="release-1", binding_id="binding-1", operation_id="invoke"),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_get_release_attestation_route_returns_signed_attestation(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _binding, _approval, release = _setup_gated_release_with_capability_approval(
        client, store, logical_agent_id="agent-attestation-happy"
    )

    response = client.get(
        f"/v1/agent-studio/releases/{release['id']}/attestation",
        params={"project_id": DEFAULT_PROJECT_ID},
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["release_id"] == release["id"]
    assert body["status"] == "attested"
    assert body["signature_algorithm"] == "sha256-digest"
    assert body["signature"].startswith("attestation:v1:sha256-digest:")


def test_get_release_attestation_route_is_not_found_for_missing_or_ungated_release(
    client: TestClient,
) -> None:
    missing = client.get(
        "/v1/agent-studio/releases/missing-release/attestation",
        params={"project_id": DEFAULT_PROJECT_ID},
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404


def test_get_release_attestation_route_scopes_by_project_and_tenant(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _binding, _approval, release = _setup_gated_release_with_capability_approval(
        client, store, logical_agent_id="agent-attestation-cross-scope"
    )

    cross_project = client.get(
        f"/v1/agent-studio/releases/{release['id']}/attestation",
        params={"project_id": OTHER_PROJECT_ID},
        headers=MULTI_PROJECT_USER_HEADERS,
    )
    assert cross_project.status_code == 404

    cross_tenant = client.get(
        f"/v1/agent-studio/releases/{release['id']}/attestation",
        params={"project_id": DEFAULT_PROJECT_ID},
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant.status_code == 404


def test_get_release_attestation_route_returns_503_when_persistence_unavailable(
    unavailable_client: TestClient,
) -> None:
    response = unavailable_client.get(
        "/v1/agent-studio/releases/release-1/attestation",
        params={"project_id": DEFAULT_PROJECT_ID},
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_get_release_attestation_route_returns_503_when_port_unavailable(
    release_attestation_unavailable_client: TestClient,
) -> None:
    response = release_attestation_unavailable_client.get(
        "/v1/agent-studio/releases/release-1/attestation",
        params={"project_id": DEFAULT_PROJECT_ID},
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


# --- durable idempotency routes --------------------------------------------


def _idempotency_body(
    project_id: str = DEFAULT_PROJECT_ID,
    /,
    *,
    binding_digest: str = "a" * 64,
    operation_id: str = "search",
    destination: str = "descriptor-1.search",
    caller_key: str = "caller-1",
    argument_hash: str = "b" * 64,
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "binding_digest": binding_digest,
        "operation_id": operation_id,
        "destination": destination,
        "caller_key": caller_key,
        "argument_hash": argument_hash,
        **kwargs,
    }


def test_idempotency_full_http_lifecycle_claim_progress_complete_and_load(
    client: TestClient,
) -> None:
    claim_response = client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(release_id="release-1", lease_seconds=300),
        headers=USER_HEADERS,
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    assert claim["disposition"] == "acquired"
    assert claim["claim_token"] is not None
    assert claim["record"]["actor_id"] == "user-1"

    progress_response = client.post(
        "/v1/agent-studio/idempotency/mark-in-progress",
        json=_idempotency_body(
            claim_token=claim["claim_token"], expected_version=claim["record"]["version"], irreversible=True
        ),
        headers=USER_HEADERS,
    )
    assert progress_response.status_code == 200, progress_response.text
    in_progress = progress_response.json()
    assert in_progress["state"] == "in_progress"
    assert in_progress["irreversible_started"] is True

    complete_response = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            claim_token=claim["claim_token"],
            expected_version=in_progress["version"],
            result={"status": "ok", "value": 42},
        ),
        headers=USER_HEADERS,
    )
    assert complete_response.status_code == 200, complete_response.text
    completed = complete_response.json()
    assert completed["state"] == "completed"
    result_ref = completed["result_ref"]
    assert result_ref is not None

    load_response = client.get(
        f"/v1/agent-studio/idempotency/results/{result_ref}",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert load_response.status_code == 200, load_response.text
    assert load_response.json() == {"status": "ok", "value": 42}


def test_idempotency_second_claim_against_same_key_reports_in_progress_without_new_token(
    client: TestClient,
) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-dup")
    first = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["disposition"] == "acquired"

    second = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["disposition"] == "in_progress"
    assert second_body["claim_token"] is None


def test_idempotency_claim_route_rejects_out_of_bounds_lease_seconds(client: TestClient) -> None:
    response = client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(release_id="release-1", lease_seconds=0),
        headers=USER_HEADERS,
    )
    assert response.status_code == 422


def test_idempotency_mark_in_progress_route_returns_404_for_never_claimed_key(client: TestClient) -> None:
    response = client.post(
        "/v1/agent-studio/idempotency/mark-in-progress",
        json=_idempotency_body(caller_key="never-claimed", claim_token="x" * 32, expected_version="1"),
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


def test_idempotency_mark_in_progress_route_returns_409_for_wrong_token_or_version(
    client: TestClient,
) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-conflict")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()

    wrong_token = client.post(
        "/v1/agent-studio/idempotency/mark-in-progress",
        json=_idempotency_body(
            caller_key="caller-conflict", claim_token="x" * 32, expected_version=claim["record"]["version"]
        ),
        headers=USER_HEADERS,
    )
    assert wrong_token.status_code == 409

    wrong_version = client.post(
        "/v1/agent-studio/idempotency/mark-in-progress",
        json=_idempotency_body(
            caller_key="caller-conflict", claim_token=claim["claim_token"], expected_version="99"
        ),
        headers=USER_HEADERS,
    )
    assert wrong_version.status_code == 409


def test_idempotency_complete_route_returns_409_when_result_mismatch(client: TestClient) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-mismatch")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()

    response = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            caller_key="caller-mismatch",
            claim_token=claim["claim_token"],
            expected_version=claim["record"]["version"],
            result={"status": "ok"},
            expected_result_hash="0" * 64,
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 409


def test_idempotency_complete_route_returns_409_for_unclaimed_and_stale_transition(
    client: TestClient,
) -> None:
    unclaimed = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            caller_key="never-claimed-complete", claim_token="x" * 32, expected_version="1", result={}
        ),
        headers=USER_HEADERS,
    )
    assert unclaimed.status_code == 404

    body = _idempotency_body(release_id="release-1", caller_key="caller-stale-complete")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()
    first_complete = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            caller_key="caller-stale-complete",
            claim_token=claim["claim_token"],
            expected_version=claim["record"]["version"],
            result={"status": "ok"},
        ),
        headers=USER_HEADERS,
    )
    assert first_complete.status_code == 200, first_complete.text

    stale_retry = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            caller_key="caller-stale-complete",
            claim_token=claim["claim_token"],
            expected_version=claim["record"]["version"],
            result={"status": "ok"},
        ),
        headers=USER_HEADERS,
    )
    assert stale_retry.status_code == 409


def test_idempotency_fail_route_marks_reconciliation_required(client: TestClient) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-fail")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()

    response = client.post(
        "/v1/agent-studio/idempotency/fail",
        json=_idempotency_body(
            caller_key="caller-fail",
            claim_token=claim["claim_token"],
            expected_version=claim["record"]["version"],
            failure_code="downstream-timeout",
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    failed = response.json()
    assert failed["state"] == "failed"
    assert failed["failure_code"] == "downstream-timeout"
    assert failed["reconciliation_required"] is True


def test_idempotency_fail_route_returns_404_for_never_claimed_key(client: TestClient) -> None:
    response = client.post(
        "/v1/agent-studio/idempotency/fail",
        json=_idempotency_body(
            caller_key="never-claimed-fail", claim_token="x" * 32, expected_version="1", failure_code="boom"
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


def test_idempotency_fail_route_returns_409_for_wrong_token_or_version(client: TestClient) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-fail-conflict")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()

    response = client.post(
        "/v1/agent-studio/idempotency/fail",
        json=_idempotency_body(
            caller_key="caller-fail-conflict",
            claim_token=claim["claim_token"],
            expected_version="stale-version",
            failure_code="boom",
        ),
        headers=USER_HEADERS,
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_claim_route_wraps_port_value_error_as_422(client: TestClient) -> None:
    """A malformed ``lease_seconds`` rejected by the port itself (not just the
    request schema's own field bounds) must still surface as a 422 -- this
    exercises the route's own ``except ValueError`` translation directly,
    since the schema's ``Field(gt=0, le=3600)`` already matches the port's
    bounds and would otherwise make this branch unreachable over HTTP."""

    class _FakeRequest:
        def __init__(self, app: FastAPI, headers: dict[str, str]) -> None:
            self.app = app
            self.headers = headers

    payload = ClaimIdempotencyRequest.model_construct(
        project_id=DEFAULT_PROJECT_ID,
        binding_digest="a" * 64,
        operation_id="search",
        destination="descriptor-1.search",
        caller_key="caller-direct-value-error",
        argument_hash="b" * 64,
        release_id="release-1",
        lease_seconds=0.0,
    )
    request = cast(Request, _FakeRequest(cast(FastAPI, client.app), USER_HEADERS))
    with pytest.raises(HTTPException) as exc_info:
        await claim_idempotency_route(request, payload)
    assert exc_info.value.status_code == 422


def test_idempotency_load_result_route_returns_404_for_unknown_ref(client: TestClient) -> None:
    response = client.get(
        "/v1/agent-studio/idempotency/results/idempotency-result::unknown",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


def test_idempotency_routes_never_leak_across_project_or_tenant_scope(
    client: TestClient,
) -> None:
    body = _idempotency_body(release_id="release-1", caller_key="caller-scope")
    claim = client.post("/v1/agent-studio/idempotency/claim", json=body, headers=USER_HEADERS).json()
    complete = client.post(
        "/v1/agent-studio/idempotency/complete",
        json=_idempotency_body(
            caller_key="caller-scope",
            claim_token=claim["claim_token"],
            expected_version=claim["record"]["version"],
            result={"status": "ok"},
        ),
        headers=USER_HEADERS,
    ).json()
    result_ref = complete["result_ref"]

    # A different project the caller *is* a member of, under the same
    # tenant, must see neither the claim state nor the completed result --
    # the exact same key fields and result_ref string resolve to a
    # completely different, empty durable partition.
    cross_project_claim = client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(OTHER_PROJECT_ID, release_id="release-1", caller_key="caller-scope"),
        headers=MULTI_PROJECT_USER_HEADERS,
    )
    assert cross_project_claim.status_code == 200, cross_project_claim.text
    assert cross_project_claim.json()["disposition"] == "acquired"

    cross_project_load = client.get(
        f"/v1/agent-studio/idempotency/results/{result_ref}",
        params=_params(OTHER_PROJECT_ID),
        headers=MULTI_PROJECT_USER_HEADERS,
    )
    assert cross_project_load.status_code == 404

    # A caller who is not a member of the target project at all is denied
    # before ever reaching the idempotency store.
    non_member_response = client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(OTHER_PROJECT_ID, release_id="release-1", caller_key="caller-scope-2"),
        headers=USER_HEADERS,
    )
    assert non_member_response.status_code == 403

    # Cross-tenant (same project group name, different authenticated
    # tenant): the scope resolves but the underlying tenant partition is
    # different, so the result must not be found.
    cross_tenant_load = client.get(
        f"/v1/agent-studio/idempotency/results/{result_ref}",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant_load.status_code == 404


def test_idempotency_routes_return_503_when_persistence_unavailable(
    unavailable_client: TestClient,
) -> None:
    claim_response = unavailable_client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(release_id="release-1"),
        headers=USER_HEADERS,
    )
    assert claim_response.status_code == 503

    load_response = unavailable_client.get(
        "/v1/agent-studio/idempotency/results/idempotency-result::unknown",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert load_response.status_code == 503


def test_idempotency_routes_return_503_when_idempotency_port_unavailable(
    idempotency_unavailable_client: TestClient,
) -> None:
    response = idempotency_unavailable_client.post(
        "/v1/agent-studio/idempotency/claim",
        json=_idempotency_body(release_id="release-1"),
        headers=USER_HEADERS,
    )
    assert response.status_code == 503


def test_escalation_routes_cover_pending_approval_and_owner_only_decision(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-escalation", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-escalation",
        principal_id="maintainer-1",
        role=AgentRole.MAINTAINER,
    )

    request_response = client.post(
        "/v1/agent-studio/agents/agent-escalation/escalations",
        json=_body(requested_role="maintainer", evidence_summary="need write access"),
        headers=VIEWER_HEADERS,
    )
    assert request_response.status_code == 201
    approval = request_response.json()
    assert approval["kind"] == "admin_escalation"
    assert approval["state"] == "pending"

    maintainer_headers = _project_headers(
        tenant_id="demo",
        user_id="maintainer-1",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    maintainer_decision = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True),
        headers=maintainer_headers,
    )
    assert maintainer_decision.status_code == 409

    approved = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True, rationale="approved"),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert (
        store.role_for(_scope(), "agent-escalation", "viewer-1")
        is AgentRole.MAINTAINER
    )

    draft = _get_draft(client, "agent-escalation", headers=USER_HEADERS)
    update_response = client.put(
        "/v1/agent-studio/agents/agent-escalation/draft",
        json={"manifest": draft["manifest"]},
        headers={**VIEWER_HEADERS, "If-Match": draft["etag"]},
    )
    assert update_response.status_code == 200


def test_deployment_routes_cover_deploy_health_rollback_and_errors(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-deploy", headers=USER_HEADERS)
    first_version = _cut_gated_version(client, "agent-deploy", headers=USER_HEADERS)

    viewer_deploy = client.post(
        "/v1/agent-studio/agents/agent-deploy/deployments",
        json=_body(version_id=first_version["id"]),
        headers=VIEWER_HEADERS,
    )
    assert viewer_deploy.status_code == 409

    first_deployment = _deploy_version(
        client,
        logical_agent_id="agent-deploy",
        version_id=first_version["id"],
        headers=USER_HEADERS,
        trace_ref="trace://first",
    )
    assert first_deployment["trace_ref"] == "trace://first"

    health_response = client.post(
        f"/v1/agent-studio/deployments/{first_deployment['id']}/health",
        json=_body(status="degraded", detail="slow response", trace_ref="trace://health"),
        headers=USER_HEADERS,
    )
    assert health_response.status_code == 200
    assert health_response.json()["health"]["status"] == HealthStatus.DEGRADED.value
    assert health_response.json()["trace_ref"] == "trace://health"

    missing_health = client.post(
        "/v1/agent-studio/deployments/missing/health",
        json=_body(status="healthy"),
        headers=USER_HEADERS,
    )
    assert missing_health.status_code == 404

    deploy_list = client.get(
        "/v1/agent-studio/agents/agent-deploy/deployments",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert deploy_list.status_code == 200
    assert [item["id"] for item in deploy_list.json()] == [first_deployment["id"]]

    draft = _get_draft(client, "agent-deploy", headers=USER_HEADERS)
    draft["manifest"]["description"] = "second version"
    _update_manifest(client, "agent-deploy", draft["manifest"], headers=USER_HEADERS)
    second_version = _cut_gated_version(client, "agent-deploy", headers=USER_HEADERS)
    second_deployment = _deploy_version(
        client,
        logical_agent_id="agent-deploy",
        version_id=second_version["id"],
        headers=USER_HEADERS,
    )

    _grant_role(
        store,
        logical_agent_id="agent-deploy",
        principal_id="maintainer-2",
        role=AgentRole.MAINTAINER,
    )
    maintainer_headers = _project_headers(
        tenant_id="demo",
        user_id="maintainer-2",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    rollback = client.post(
        "/v1/agent-studio/agents/agent-deploy/rollback",
        json=_body(
            deployment_id=second_deployment["id"],
            target_version_id=first_version["id"],
        ),
        headers=maintainer_headers,
    )
    assert rollback.status_code == 201
    assert rollback.json()["version_id"] == first_version["id"]
    assert rollback.json()["rollback_of_deployment_id"] == second_deployment["id"]

    bad_rollback = client.post(
        "/v1/agent-studio/agents/agent-deploy/rollback",
        json=_body(
            deployment_id="missing",
            target_version_id=first_version["id"],
        ),
        headers=maintainer_headers,
    )
    assert bad_rollback.status_code == 409


def test_record_health_requires_maintainer_role_at_router_and_service(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    """A viewer or contributor must never be able to record deployment
    health: the health status recorded here is the sole gate
    ``activate_release`` relies on, so anything less than MAINTAINER must
    be refused before the mutation is attempted, and the role must be
    resolved against the *deployment's own* logical agent (not merely a
    project-scoped identity check)."""
    _create_agent(client, logical_agent_id="agent-health-authz", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-health-authz", headers=USER_HEADERS)
    deployment = _deploy_version(
        client,
        logical_agent_id="agent-health-authz",
        version_id=version["id"],
        headers=USER_HEADERS,
    )

    contributor_headers = _project_headers(
        tenant_id="demo",
        user_id="contributor-1",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    _grant_role(
        store,
        logical_agent_id="agent-health-authz",
        principal_id="contributor-1",
        role=AgentRole.CONTRIBUTOR,
    )

    viewer_attempt = client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy"),
        headers=VIEWER_HEADERS,
    )
    assert viewer_attempt.status_code == 403

    contributor_attempt = client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy"),
        headers=contributor_headers,
    )
    assert contributor_attempt.status_code == 403

    # No health update was ever persisted by either denied attempt.
    unchanged = client.get(
        "/v1/agent-studio/agents/agent-health-authz/deployments",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert unchanged.json()[0]["health"]["status"] == HealthStatus.UNKNOWN.value

    maintainer_headers = _project_headers(
        tenant_id="demo",
        user_id="maintainer-health",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    _grant_role(
        store,
        logical_agent_id="agent-health-authz",
        principal_id="maintainer-health",
        role=AgentRole.MAINTAINER,
    )
    maintainer_attempt = client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy", detail="verified by maintainer"),
        headers=maintainer_headers,
    )
    assert maintainer_attempt.status_code == 200
    assert maintainer_attempt.json()["health"]["status"] == HealthStatus.HEALTHY.value


def test_resolve_contract_and_catalog_routes_cover_full_happy_path(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-contract", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-contract", headers=USER_HEADERS)
    draft["manifest"]["input_schema_ref"] = {
        "ref": "schema://contract-input",
        "digest": "sha256:f77bc2fc512ca730ea20e2430db5ef5c916f09991900625b84002bbbd9947b69",
        "inline_schema": {"type": "object", "title": "contract-input"},
    }
    draft["manifest"]["output_schema_ref"] = {
        "ref": "schema://contract-output",
        "digest": "sha256:8f52ff8dd5564f348111007b76a61a2edeb97600048b040cd109468db67fc4ca",
        "inline_schema": {"type": "object", "title": "contract-output"},
    }
    draft["manifest"]["capabilities"] = [
        client.post(
            "/v1/agent-studio/capabilities/attach",
            json={"descriptor_id": "foundry.web_search", "operation": "search"},
            headers=USER_HEADERS,
        ).json()
    ]
    _update_manifest(client, "agent-contract", draft["manifest"], headers=USER_HEADERS)

    contract_version = _cut_version(client, "agent-contract", headers=USER_HEADERS)
    pre_release_contract = client.get(
        f"/v1/agent-studio/versions/{contract_version['id']}/contract",
        params=_params(environment="development"),
        headers=USER_HEADERS,
    )
    assert pre_release_contract.status_code == 404

    _run_gates(client, contract_version["id"], headers=USER_HEADERS)
    promoted = client.post(
        f"/v1/agent-studio/versions/{contract_version['id']}/promote",
        json=_body(destination="dev", evidence_summary="release candidate"),
        headers=USER_HEADERS,
    )
    assert promoted.status_code == 200

    deployment = _deploy_version(
        client,
        logical_agent_id="agent-contract",
        version_id=contract_version["id"],
        headers=USER_HEADERS,
    )
    assert deployment["version_id"] == contract_version["id"]

    health_response = client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy", detail="smoke ok"),
        headers=USER_HEADERS,
    )
    assert health_response.status_code == 200

    activate_response = client.post(
        f"/v1/agent-studio/versions/{contract_version['id']}/activate",
        json=_body(),
        headers=USER_HEADERS,
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["status"] == ReleaseStatus.ACTIVE.value
    assert activate_response.json()["deployment_id"] == deployment["id"]

    resolve_response = client.get(
        "/v1/agent-studio/agents/agent-contract/resolve",
        params=_params(environment="development"),
        headers=USER_HEADERS,
    )
    assert resolve_response.status_code == 200
    resolved = resolve_response.json()
    assert resolved["logical_agent_id"] == "agent-contract"
    assert resolved["environment"] == DeploymentEnvironment.DEVELOPMENT.value
    assert resolved["version_id"] == contract_version["id"]
    assert resolved["release_status"] == ReleaseStatus.ACTIVE.value
    assert resolved["runtime_target"] == "managed_foundry"
    assert resolved["capability_versions"][0]["descriptor_ref"]["id"] == "foundry.web_search"
    assert resolved["capability_versions"][0]["operation_ref"]["id"] == "search"
    assert resolved["capability_versions"][0]["binding_id"]
    assert resolved["input_schema_ref"]["ref"] == "schema://contract-input"

    exact_contract_response = client.get(
        f"/v1/agent-studio/versions/{contract_version['id']}/contract",
        params=_params(environment="development"),
        headers=USER_HEADERS,
    )
    assert exact_contract_response.status_code == 200
    assert exact_contract_response.json()["release_status"] == ReleaseStatus.ACTIVE.value

    _create_agent(client, logical_agent_id="agent-catalog-empty", headers=USER_HEADERS)
    catalog_response = client.get(
        "/v1/agent-studio/catalog",
        params=_params(environment="development"),
        headers=USER_HEADERS,
    )
    assert catalog_response.status_code == 200
    assert [item["logical_agent_id"] for item in catalog_response.json()] == ["agent-contract"]

    unresolved_response = client.get(
        "/v1/agent-studio/agents/agent-catalog-empty/resolve",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert unresolved_response.status_code == 404

    missing_contract = client.get(
        "/v1/agent-studio/versions/missing-version/contract",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing_contract.status_code == 404

    assert store.get_binding(
        _scope(),
        "agent-contract",
        DeploymentEnvironment.DEVELOPMENT,
    ) is not None


def test_resolve_and_contract_routes_fail_closed_on_stale_capability_binding(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    memory_service: MemoryService,
    builder_service: BuilderService,
    audit_service: AuditService,
) -> None:
    """Resolve/invoke must fail closed (409), never silently return a
    contract, when the capability binding backing the active release has
    gone stale since deploy (e.g. the provider descriptor was removed)."""
    live_app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=DeploymentService(store, capability_registry=registry),
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(live_app) as live_client:
        _create_agent(live_client, logical_agent_id="agent-stale-resolve", headers=USER_HEADERS)
        draft = _get_draft(live_client, "agent-stale-resolve", headers=USER_HEADERS)
        draft["manifest"]["capabilities"] = [
            live_client.post(
                "/v1/agent-studio/capabilities/attach",
                json={"descriptor_id": "foundry.web_search", "operation": "search"},
                headers=USER_HEADERS,
            ).json()
        ]
        _update_manifest(live_client, "agent-stale-resolve", draft["manifest"], headers=USER_HEADERS)
        version = _cut_gated_version(live_client, "agent-stale-resolve", headers=USER_HEADERS)
        deployment = _deploy_version(
            live_client,
            logical_agent_id="agent-stale-resolve",
            version_id=version["id"],
            headers=USER_HEADERS,
        )
        assert deployment["version_id"] == version["id"]

    # A second app instance, sharing the same store, whose registry no longer
    # recognizes the attached descriptor -- simulating drift discovered after
    # deploy, at resolve/invoke time.
    stale_app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=DeploymentService(store, capability_registry=CapabilityRegistry(descriptors=())),
        memory_service=memory_service,
        builder_service=builder_service,
        audit_service=audit_service,
    )
    with TestClient(stale_app) as stale_client:
        resolve_response = stale_client.get(
            "/v1/agent-studio/agents/agent-stale-resolve/resolve",
            params=_params(environment="development"),
            headers=USER_HEADERS,
        )
        assert resolve_response.status_code == 409
        assert "stale" in resolve_response.json()["detail"]

        contract_response = stale_client.get(
            f"/v1/agent-studio/versions/{version['id']}/contract",
            params=_params(environment="development"),
            headers=USER_HEADERS,
        )
        assert contract_response.status_code == 409
        assert "stale" in contract_response.json()["detail"]

        # The bulk catalog listing must omit the stale agent rather than
        # 500ing the entire response for this tenant/project.
        catalog_response = stale_client.get(
            "/v1/agent-studio/catalog",
            params=_params(environment="development"),
            headers=USER_HEADERS,
        )
        assert catalog_response.status_code == 200
        assert catalog_response.json() == []


def test_activate_route_covers_missing_version_authorization_and_conflict(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    missing_activate = client.post(
        "/v1/agent-studio/versions/missing-version/activate",
        json=_body(),
        headers=USER_HEADERS,
    )
    assert missing_activate.status_code == 404

    _create_agent(client, logical_agent_id="agent-activate-route", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-activate-route", headers=USER_HEADERS)
    promoted = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="release candidate"),
        headers=USER_HEADERS,
    )
    assert promoted.status_code == 200

    _grant_role(
        store,
        logical_agent_id="agent-activate-route",
        principal_id="activate-contributor",
        role=AgentRole.CONTRIBUTOR,
    )
    contributor_headers = _project_headers(
        tenant_id="demo",
        user_id="activate-contributor",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    forbidden_activate = client.post(
        f"/v1/agent-studio/versions/{version['id']}/activate",
        json=_body(),
        headers=contributor_headers,
    )
    assert forbidden_activate.status_code == 403

    conflict_activate = client.post(
        f"/v1/agent-studio/versions/{version['id']}/activate",
        json=_body(),
        headers=USER_HEADERS,
    )
    assert conflict_activate.status_code == 409


def test_memory_lifecycle_covers_remember_recall_inspect_correct_forget_export_and_audit(
    client: TestClient,
) -> None:
    _create_agent(client, logical_agent_id="agent-memory", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory", headers=USER_HEADERS)

    first_entry = client.post(
        "/v1/agent-studio/agents/agent-memory/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            role="fact",
            content="first memory",
            ttl_days=7,
            read_acl=["reader-1"],
            write_acl=["writer-1"],
        ),
        headers=USER_HEADERS,
    )
    assert first_entry.status_code == 201, first_entry.text
    first_body = first_entry.json()
    assert first_body["scope_kind"] == MemoryScopeKind.CONVERSATION.value
    assert first_body["created_by"] == "user-1"
    assert first_body["ttl_days"] == 7
    assert first_body["expires_at"] is not None
    assert first_body["read_acl"] == ["reader-1"]
    assert first_body["write_acl"] == ["writer-1"]

    creator_recall = client.get(
        "/v1/agent-studio/agents/agent-memory/memory",
        params=_params(scope_kind="conversation", scope_id="thread-1", limit=10),
        headers=USER_HEADERS,
    )
    assert creator_recall.status_code == 200
    assert [entry["id"] for entry in creator_recall.json()] == [first_body["id"]]

    denied_recall = client.get(
        "/v1/agent-studio/agents/agent-memory/memory",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=VIEWER_HEADERS,
    )
    assert denied_recall.status_code == 200
    assert denied_recall.json() == []

    inspect_response = client.get(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert inspect_response.status_code == 200
    assert inspect_response.json()["content"] == "first memory"

    denied_inspect = client.get(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        params=_params(),
        headers=VIEWER_HEADERS,
    )
    assert denied_inspect.status_code == 403

    correct_response = client.put(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json=_body(content="corrected memory"),
        headers=USER_HEADERS,
    )
    assert correct_response.status_code == 200
    assert correct_response.json()["content"] == "corrected memory"
    assert correct_response.json()["provenance"] == "operator_correction"

    denied_correct = client.put(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json=_body(content="should fail"),
        headers=VIEWER_HEADERS,
    )
    assert denied_correct.status_code == 403

    second_entry = client.post(
        "/v1/agent-studio/agents/agent-memory/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            content="second memory",
        ),
        headers=USER_HEADERS,
    )
    assert second_entry.status_code == 201

    export_response = client.get(
        "/v1/agent-studio/agents/agent-memory/memory-export",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert export_response.status_code == 200
    assert {entry["id"] for entry in export_response.json()} == {
        first_body["id"],
        second_entry.json()["id"],
    }

    forget_response = client.request(
        "DELETE",
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json=_body(reason="superseded"),
        headers=USER_HEADERS,
    )
    assert forget_response.status_code == 200
    assert forget_response.json()["deleted_at"] is not None

    denied_forget = client.request(
        "DELETE",
        f"/v1/agent-studio/agents/agent-memory/memory/{second_entry.json()['id']}",
        json=_body(reason="should fail"),
        headers=VIEWER_HEADERS,
    )
    assert denied_forget.status_code == 403

    post_forget_recall = client.get(
        "/v1/agent-studio/agents/agent-memory/memory",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert post_forget_recall.status_code == 200
    assert [entry["id"] for entry in post_forget_recall.json()] == [second_entry.json()["id"]]

    post_forget_export = client.get(
        "/v1/agent-studio/agents/agent-memory/memory-export",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert post_forget_export.status_code == 200
    assert [entry["id"] for entry in post_forget_export.json()] == [second_entry.json()["id"]]

    audit_response = client.get(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}/audit",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert audit_response.status_code == 200
    assert [record["action"] for record in audit_response.json()] == [
        "remember",
        "inspect",
        "correct",
        "forget",
    ]

    denied_audit = client.get(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}/audit",
        params=_params(),
        headers=VIEWER_HEADERS,
    )
    assert denied_audit.status_code == 403

    reader_audit = client.get(
        f"/v1/agent-studio/agents/agent-memory/memory/{first_body['id']}/audit",
        params=_params(),
        headers=READER_HEADERS,
    )
    assert reader_audit.status_code == 200
    assert [record["action"] for record in reader_audit.json()] == [
        "remember",
        "inspect",
        "correct",
        "forget",
    ]

    _create_agent(client, logical_agent_id="agent-memory-other", headers=USER_HEADERS)
    cross_agent_audit = client.get(
        f"/v1/agent-studio/agents/agent-memory-other/memory/{first_body['id']}/audit",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert cross_agent_audit.status_code == 404

    scope_pseudo_id_audit = client.get(
        "/v1/agent-studio/agents/agent-memory/memory/scope:conversation:thread-1/audit",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert scope_pseudo_id_audit.status_code == 403


def test_memory_routes_cover_policy_errors_missing_records_and_unavailability(
    client: TestClient,
    memory_unavailable_client: TestClient,
) -> None:
    missing_agent_remember = client.post(
        "/v1/agent-studio/agents/agent-missing/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            content="missing agent",
        ),
        headers=USER_HEADERS,
    )
    assert missing_agent_remember.status_code == 404

    _create_agent(client, logical_agent_id="agent-memory-disabled", headers=USER_HEADERS)
    disabled_remember = client.post(
        "/v1/agent-studio/agents/agent-memory-disabled/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            content="not allowed",
        ),
        headers=USER_HEADERS,
    )
    assert disabled_remember.status_code == 422

    disabled_recall = client.get(
        "/v1/agent-studio/agents/agent-memory-disabled/memory",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert disabled_recall.status_code == 422

    disabled_export = client.get(
        "/v1/agent-studio/agents/agent-memory-disabled/memory-export",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert disabled_export.status_code == 422

    _create_agent(client, logical_agent_id="agent-memory-scope", headers=USER_HEADERS)
    _enable_memory_scope(
        client,
        "agent-memory-scope",
        scope_kind="conversation",
        mechanism=MemoryMechanism.APPLICATION_MEMORY_STORE.value,
        headers=USER_HEADERS,
    )
    undeclared_scope = client.post(
        "/v1/agent-studio/agents/agent-memory-scope/memory",
        json=_body(
            scope_kind="project",
            scope_id="project-1",
            content="wrong scope",
        ),
        headers=USER_HEADERS,
    )
    assert undeclared_scope.status_code == 422

    _create_agent(client, logical_agent_id="agent-memory-preview", headers=USER_HEADERS)
    _enable_memory_scope(
        client,
        "agent-memory-preview",
        mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE.value,
        headers=USER_HEADERS,
    )
    preview_recall = client.get(
        "/v1/agent-studio/agents/agent-memory-preview/memory",
        params=_params(scope_kind="conversation", scope_id="thread-1"),
        headers=USER_HEADERS,
    )
    assert preview_recall.status_code == 422

    _create_agent(client, logical_agent_id="agent-memory-missing-entry", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory-missing-entry", headers=USER_HEADERS)
    for method, path, payload in [
    (
        "GET",
        f"/v1/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry?project_id={DEFAULT_PROJECT_ID}",
        None,
    ),
        (
            "PUT",
            "/v1/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry",
            _body(content="update"),
        ),
        (
            "DELETE",
            "/v1/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry",
            _body(reason="forget"),
        ),
    ]:
        response = client.request(
            method,
            path,
            json=payload,
            headers=USER_HEADERS,
        )
        assert response.status_code == 404, (method, response.text)

    memory_unavailable = memory_unavailable_client.post(
        "/v1/agent-studio/agents/agent-memory-missing-entry/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            content="blocked by unavailable store",
        ),
        headers=USER_HEADERS,
    )
    assert memory_unavailable.status_code == 503

    audit_missing_draft = client.get(
        "/v1/agent-studio/agents/missing-agent/memory/entry-1/audit",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert audit_missing_draft.status_code == 404


def test_builder_propose_apply_reject_flow_and_history(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-builder", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-builder", headers=USER_HEADERS)

    propose = client.post(
        "/v1/agent-studio/agents/agent-builder/builder/messages",
        json=_body(message="Add a helpful description.", base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert propose.status_code == 201, propose.text
    proposal = propose.json()
    assert proposal["state"] == "pending"
    assert proposal["logical_agent_id"] == "agent-builder"
    assert proposal["draft_base_etag"] == draft["etag"]
    assert proposal["after_manifest"]["description"] == "Add a helpful description."
    assert proposal["provenance"]["message"] == "Add a helpful description."
    assert proposal["provenance"]["requested_by"] == "user-1"
    assert any(change["field"] == "description" for change in proposal["changes"])

    history = client.get(
        "/v1/agent-studio/agents/agent-builder/proposals",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [proposal["id"]]

    fetched = client.get(
        f"/v1/agent-studio/agents/agent-builder/proposals/{proposal['id']}",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert fetched.status_code == 200
    assert fetched.json() == proposal

    apply_response = client.post(
        f"/v1/agent-studio/agents/agent-builder/proposals/{proposal['id']}/apply",
        json=_body(base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert apply_response.status_code == 200, apply_response.text
    updated_draft = apply_response.json()
    assert updated_draft["manifest"]["description"] == "Add a helpful description."
    assert updated_draft["etag"] != draft["etag"]

    applied_proposal = client.get(
        f"/v1/agent-studio/agents/agent-builder/proposals/{proposal['id']}",
        params=_params(),
        headers=USER_HEADERS,
    ).json()
    assert applied_proposal["state"] == "applied"
    assert applied_proposal["decided_by"] == "user-1"
    assert applied_proposal["applied_draft_etag"] == updated_draft["etag"]

    second_propose = client.post(
        "/v1/agent-studio/agents/agent-builder/builder/messages",
        json=_body(message="Add another change.", base_etag=updated_draft["etag"]),
        headers=USER_HEADERS,
    ).json()
    reject_response = client.post(
        f"/v1/agent-studio/agents/agent-builder/proposals/{second_propose['id']}/reject",
        json=_body(reason="Not needed right now."),
        headers=USER_HEADERS,
    )
    assert reject_response.status_code == 200, reject_response.text
    rejected = reject_response.json()
    assert rejected["state"] == "rejected"
    assert rejected["rejection_reason"] == "Not needed right now."

    # Rejection must not have mutated the draft.
    unchanged_draft = _get_draft(client, "agent-builder", headers=USER_HEADERS)
    assert unchanged_draft["etag"] == updated_draft["etag"]

    full_history = client.get(
        "/v1/agent-studio/agents/agent-builder/proposals",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert {item["id"] for item in full_history.json()} == {proposal["id"], second_propose["id"]}


def test_builder_propose_rejects_stale_etag_and_insufficient_role(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-builder-guard", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-builder-guard", headers=USER_HEADERS)

    stale = client.post(
        "/v1/agent-studio/agents/agent-builder-guard/builder/messages",
        json=_body(message="hello", base_etag="stale-etag"),
        headers=USER_HEADERS,
    )
    assert stale.status_code == 409

    forbidden = client.post(
        "/v1/agent-studio/agents/agent-builder-guard/builder/messages",
        json=_body(message="hello", base_etag=draft["etag"]),
        headers=VIEWER_HEADERS,
    )
    assert forbidden.status_code == 403

    # Role resolution runs before existence checks, and there is no grant for
    # this unknown agent, so the actor resolves to a role below CONTRIBUTOR.
    missing_agent = client.post(
        "/v1/agent-studio/agents/agent-does-not-exist/builder/messages",
        json=_body(message="hello", base_etag="any-etag"),
        headers=USER_HEADERS,
    )
    assert missing_agent.status_code == 403


def test_builder_apply_and_reject_cover_not_found_conflict_and_role_errors(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-builder-errors", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-builder-errors", headers=USER_HEADERS)

    missing_apply = client.post(
        "/v1/agent-studio/agents/agent-builder-errors/proposals/missing-proposal/apply",
        json=_body(base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert missing_apply.status_code == 404

    missing_reject = client.post(
        "/v1/agent-studio/agents/agent-builder-errors/proposals/missing-proposal/reject",
        json=_body(reason="n/a"),
        headers=USER_HEADERS,
    )
    assert missing_reject.status_code == 404

    missing_get = client.get(
        "/v1/agent-studio/agents/agent-builder-errors/proposals/missing-proposal",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing_get.status_code == 404

    proposal = client.post(
        "/v1/agent-studio/agents/agent-builder-errors/builder/messages",
        json=_body(message="Change it.", base_etag=draft["etag"]),
        headers=USER_HEADERS,
    ).json()

    forbidden_apply = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{proposal['id']}/apply",
        json=_body(base_etag=draft["etag"]),
        headers=VIEWER_HEADERS,
    )
    assert forbidden_apply.status_code == 403

    stale_apply = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{proposal['id']}/apply",
        json=_body(base_etag="stale-etag"),
        headers=USER_HEADERS,
    )
    assert stale_apply.status_code == 409

    apply_response = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{proposal['id']}/apply",
        json=_body(base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert apply_response.status_code == 200, apply_response.text

    already_decided = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{proposal['id']}/apply",
        json=_body(base_etag=apply_response.json()["etag"]),
        headers=USER_HEADERS,
    )
    assert already_decided.status_code == 409

    already_decided_reject = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{proposal['id']}/reject",
        json=_body(reason="too late"),
        headers=USER_HEADERS,
    )
    assert already_decided_reject.status_code == 409

    forbidden_reject_headers_proposal = client.post(
        "/v1/agent-studio/agents/agent-builder-errors/builder/messages",
        json=_body(message="Another change.", base_etag=apply_response.json()["etag"]),
        headers=USER_HEADERS,
    ).json()
    forbidden_reject = client.post(
        f"/v1/agent-studio/agents/agent-builder-errors/proposals/{forbidden_reject_headers_proposal['id']}/reject",
        json=_body(reason="n/a"),
        headers=VIEWER_HEADERS,
    )
    assert forbidden_reject.status_code == 403


def test_builder_routes_return_503_when_unavailable(unavailable_client: TestClient) -> None:
    propose = unavailable_client.post(
        "/v1/agent-studio/agents/agent-builder-unavailable/builder/messages",
        json=_body(message="hello", base_etag="etag-1"),
        headers=USER_HEADERS,
    )
    assert propose.status_code == 503

    history = unavailable_client.get(
        "/v1/agent-studio/agents/agent-builder-unavailable/proposals",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert history.status_code == 503

    fetch = unavailable_client.get(
        "/v1/agent-studio/agents/agent-builder-unavailable/proposals/missing",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert fetch.status_code == 503

    apply_response = unavailable_client.post(
        "/v1/agent-studio/agents/agent-builder-unavailable/proposals/missing/apply",
        json=_body(base_etag="etag-1"),
        headers=USER_HEADERS,
    )
    assert apply_response.status_code == 503

    reject_response = unavailable_client.post(
        "/v1/agent-studio/agents/agent-builder-unavailable/proposals/missing/reject",
        json=_body(reason="n/a"),
        headers=USER_HEADERS,
    )
    assert reject_response.status_code == 503


def test_builder_proposals_are_tenant_isolated(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-builder-tenant", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-builder-tenant", headers=USER_HEADERS)
    proposal = client.post(
        "/v1/agent-studio/agents/agent-builder-tenant/builder/messages",
        json=_body(message="hello", base_etag=draft["etag"]),
        headers=USER_HEADERS,
    ).json()

    other_tenant_history = client.get(
        "/v1/agent-studio/agents/agent-builder-tenant/proposals",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert other_tenant_history.status_code == 200
    assert other_tenant_history.json() == []

    other_tenant_fetch = client.get(
        f"/v1/agent-studio/agents/agent-builder-tenant/proposals/{proposal['id']}",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert other_tenant_fetch.status_code == 404


def test_builder_propose_returns_503_when_generator_unavailable(
    settings: Settings,
    store: AgentStudioStore,
    registry: CapabilityRegistry,
    model_discovery: InMemoryModelDiscovery,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    memory_service: MemoryService,
    audit_service: AuditService,
) -> None:
    unavailable_generator_service = BuilderService(
        store,
        UnavailableManifestProposalGenerator(),
        InMemoryArtifactBundleStore(),
        release_service,
    )
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
        builder_service=unavailable_generator_service,
        audit_service=audit_service,
    )
    with TestClient(app) as no_generator_client:
        _create_agent(no_generator_client, logical_agent_id="agent-builder-no-generator", headers=USER_HEADERS)
        draft = _get_draft(no_generator_client, "agent-builder-no-generator", headers=USER_HEADERS)

        response = no_generator_client.post(
            "/v1/agent-studio/agents/agent-builder-no-generator/builder/messages",
            json=_body(message="hello", base_etag=draft["etag"]),
            headers=USER_HEADERS,
        )
        assert response.status_code == 503


def test_tenant_isolation_holds_for_tenant_scoped_routes(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-isolation", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-isolation", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-isolation", headers=USER_HEADERS)
    pending_headers = _project_headers(
        tenant_id="demo",
        user_id="contributor-2",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    _grant_role(
        store,
        logical_agent_id="agent-isolation",
        principal_id="contributor-2",
        role=AgentRole.CONTRIBUTOR,
    )
    approval = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=pending_headers,
    ).json()
    deployment = _deploy_version(
        client,
        logical_agent_id="agent-isolation",
        version_id=version["id"],
        headers=USER_HEADERS,
    )
    memory_entry = client.post(
        "/v1/agent-studio/agents/agent-isolation/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-iso",
            content="tenant scoped",
        ),
        headers=USER_HEADERS,
    ).json()

    cases: list[tuple[str, str, dict[str, Any] | None, int, dict[str, str]]] = [
        ("GET", f"/v1/agent-studio/agents/agent-isolation/draft?project_id={DEFAULT_PROJECT_ID}", None, 404, {}),
        (
            "PUT",
            "/v1/agent-studio/agents/agent-isolation/draft",
            {"manifest": _minimal_manifest(logical_agent_id="agent-isolation", tenant_id="other-tenant")},
            403,
            {"If-Match": "irrelevant-etag"},
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/fork",
            _body(source_version_id=version["id"], new_logical_agent_id="agent-isolation-fork"),
            409,
            {},
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/tool-registrations",
            _body(
                descriptor_id="foundry.web_search",
                operation="search",
                kind="managed_foundry_native",
                handler_ref="builtin://web-search",
            ),
            403,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/tool-registrations?project_id={DEFAULT_PROJECT_ID}",
            None,
            200,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/versions?project_id={DEFAULT_PROJECT_ID}",
            None,
            200,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/lineage?project_id={DEFAULT_PROJECT_ID}",
            None,
            200,
            {},
        ),
        ("POST", f"/v1/agent-studio/versions/{version['id']}/gates", _body(**GATED_EVIDENCE), 404, {}),
        (
            "POST",
            f"/v1/agent-studio/versions/{version['id']}/promote",
            _body(destination="dev", evidence_summary="no access"),
            404,
            {},
        ),
        (
            "POST",
            f"/v1/agent-studio/approvals/{approval['id']}/decision",
            _body(approve=True),
            404,
            {},
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/deployments",
            _body(version_id=version["id"]),
            409,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/deployments?project_id={DEFAULT_PROJECT_ID}",
            None,
            200,
            {},
        ),
        (
            "POST",
            f"/v1/agent-studio/deployments/{deployment['id']}/health",
            _body(status="healthy"),
            404,
            {},
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/rollback",
            _body(deployment_id=deployment["id"], target_version_id=version["id"]),
            409,
            {},
        ),
        ("GET", f"/v1/agent-studio/agents/agent-isolation/resolve?project_id={DEFAULT_PROJECT_ID}", None, 404, {}),
        (
            "GET",
            f"/v1/agent-studio/versions/{version['id']}/contract?project_id={DEFAULT_PROJECT_ID}",
            None,
            404,
            {},
        ),
        ("GET", f"/v1/agent-studio/catalog?project_id={DEFAULT_PROJECT_ID}", None, 200, {}),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/memory",
            _body(scope_kind="conversation", scope_id="thread-iso", content="no access"),
            404,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory?project_id={DEFAULT_PROJECT_ID}&scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}?project_id={DEFAULT_PROJECT_ID}",
            None,
            404,
            {},
        ),
        (
            "PUT",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            _body(content="blocked"),
            404,
            {},
        ),
        (
            "DELETE",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            _body(reason="blocked"),
            404,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory-export?project_id={DEFAULT_PROJECT_ID}&scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
            {},
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}/audit?project_id={DEFAULT_PROJECT_ID}",
            None,
            404,
            {},
        ),
    ]

    for method, path, payload, expected_status, extra_headers in cases:
        response = client.request(
            method,
            path,
            json=payload,
            headers={**OTHER_TENANT_HEADERS, **extra_headers},
        )
        assert response.status_code == expected_status, (method, path, response.text)

    assert client.get(
        "/v1/agent-studio/agents/agent-isolation/tool-registrations",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/v1/agent-studio/agents/agent-isolation/versions",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/v1/agent-studio/agents/agent-isolation/lineage",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/v1/agent-studio/agents/agent-isolation/deployments",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/v1/agent-studio/catalog",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []


def test_project_membership_is_enforced_and_demo_sandbox_bypasses_it(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-project-membership", headers=USER_HEADERS)

    no_membership_headers = _headers(tenant_id="demo", user_id="outsider")
    missing_membership = client.get(
        "/v1/agent-studio/agents/agent-project-membership/draft",
        params=_params(),
        headers=no_membership_headers,
    )
    assert missing_membership.status_code == 403
    assert "not a member of project" in missing_membership.json()["detail"]

    overage_headers = _headers(tenant_id="demo", user_id="overage", groups_overage=True)
    overage = client.get(
        "/v1/agent-studio/agents/agent-project-membership/draft",
        params=_params(),
        headers=overage_headers,
    )
    assert overage.status_code == 403
    assert "group overage" in overage.json()["detail"]

    sandbox_create = client.post(
        "/v1/agent-studio/agents",
        json=_body(
            OTHER_PROJECT_ID,
            logical_agent_id="agent-demo-sandbox",
            display_name="Sandbox Agent",
        ),
    )
    assert sandbox_create.status_code == 201, sandbox_create.text
    assert sandbox_create.json()["manifest"]["project_id"] == OTHER_PROJECT_ID
    assert sandbox_create.json()["manifest"]["owner_id"] == "demo-researcher"


def test_project_membership_in_one_project_does_not_authorize_a_different_project(
    client: TestClient,
) -> None:
    """USER_HEADERS only carries the ``project:{DEFAULT_PROJECT_ID}`` group;
    using it against a *different* project must be denied, never silently
    authorized by virtue of belonging to some project."""
    _create_agent(
        client,
        logical_agent_id="agent-cross-project-membership",
        project_id=OTHER_PROJECT_ID,
        headers=PLATFORM_OWNER_HEADERS,
    )
    response = client.get(
        "/v1/agent-studio/agents/agent-cross-project-membership/draft",
        params=_params(OTHER_PROJECT_ID),
        headers=USER_HEADERS,
    )
    assert response.status_code == 403
    assert "not a member of project" in response.json()["detail"]


def test_group_overage_denies_access_even_when_target_group_is_present(client: TestClient) -> None:
    """Regression for treating overage as a blanket fail-closed signal: even
    a claims list that (coincidentally) contains the right group name must
    still be rejected once the provider has reported it as truncated,
    because the resolver cannot trust *any* claim from an incomplete list."""
    overage_headers = _headers(
        tenant_id="demo",
        user_id="overage-with-group",
        groups=(project_group_name(DEFAULT_PROJECT_ID),),
        groups_overage=True,
    )
    _create_agent(client, logical_agent_id="agent-overage-with-group", headers=USER_HEADERS)
    response = client.get(
        "/v1/agent-studio/agents/agent-overage-with-group/draft",
        params=_params(),
        headers=overage_headers,
    )
    assert response.status_code == 403
    assert "group overage" in response.json()["detail"]


class _AlwaysMemberResolver:
    """Test double proving the router consults an app-composed
    ``ProjectMembershipResolver`` from ``app.state`` rather than hard-coding
    the claims-based adapter -- the adapter-swap seam the domain Protocol
    exists for."""

    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        return MembershipDecision(outcome=MembershipOutcome.MEMBER)


class _AlwaysUnavailableResolver:
    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        return MembershipDecision(outcome=MembershipOutcome.UNAVAILABLE, reason="directory unreachable")


def test_router_consults_app_composed_membership_resolver_when_present(client: TestClient) -> None:
    resolver: ProjectMembershipResolver = _AlwaysMemberResolver()
    cast(Any, client).app.state.agent_studio_membership_resolver = resolver
    no_membership_headers = _headers(tenant_id="demo", user_id="stranger")
    _create_agent(client, logical_agent_id="agent-swapped-resolver", headers=USER_HEADERS)
    response = client.get(
        "/v1/agent-studio/agents/agent-swapped-resolver/draft",
        params=_params(),
        headers=no_membership_headers,
    )
    assert response.status_code == 200, response.text


def test_router_denies_when_app_composed_resolver_reports_unavailable(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-unavailable-resolver", headers=USER_HEADERS)
    resolver: ProjectMembershipResolver = _AlwaysUnavailableResolver()
    cast(Any, client).app.state.agent_studio_membership_resolver = resolver
    response = client.get(
        "/v1/agent-studio/agents/agent-unavailable-resolver/draft",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 403
    assert "directory unreachable" in response.json()["detail"]


def test_draft_and_version_routes_are_cross_project_and_cross_tenant_isolated(
    client: TestClient,
) -> None:
    other_project_headers = _project_headers(
        tenant_id="demo",
        user_id="project-member",
        project_ids=(OTHER_PROJECT_ID,),
    )
    _create_agent(client, logical_agent_id="agent-scope-draft", headers=USER_HEADERS)
    version = _cut_version(client, "agent-scope-draft", headers=USER_HEADERS)

    cross_project_get = client.get(
        "/v1/agent-studio/agents/agent-scope-draft/draft",
        params=_params(OTHER_PROJECT_ID),
        headers=other_project_headers,
    )
    assert cross_project_get.status_code == 404

    cross_tenant_get = client.get(
        "/v1/agent-studio/agents/agent-scope-draft/draft",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant_get.status_code == 404

    cross_project_update = client.put(
        "/v1/agent-studio/agents/agent-scope-draft/draft",
        json={"manifest": _minimal_manifest(logical_agent_id="agent-scope-draft", project_id=OTHER_PROJECT_ID)},
        headers={**other_project_headers, "If-Match": "irrelevant-etag"},
    )
    assert cross_project_update.status_code == 403

    cross_tenant_update = client.put(
        "/v1/agent-studio/agents/agent-scope-draft/draft",
        json={"manifest": _minimal_manifest(logical_agent_id="agent-scope-draft")},
        headers={**OTHER_TENANT_HEADERS, "If-Match": "irrelevant-etag"},
    )
    assert cross_tenant_update.status_code == 403

    cross_project_cut = client.post(
        "/v1/agent-studio/agents/agent-scope-draft/versions",
        params=_params(OTHER_PROJECT_ID),
        headers=other_project_headers,
    )
    assert cross_project_cut.status_code == 403

    cross_tenant_cut = client.post(
        "/v1/agent-studio/agents/agent-scope-draft/versions",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant_cut.status_code == 403

    assert client.get(
        "/v1/agent-studio/agents/agent-scope-draft/versions",
        params=_params(OTHER_PROJECT_ID),
        headers=other_project_headers,
    ).json() == []
    assert client.get(
        "/v1/agent-studio/agents/agent-scope-draft/versions",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert version["logical_agent_id"] == "agent-scope-draft"


def test_release_and_approval_routes_are_cross_project_and_cross_tenant_isolated(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    other_project_headers = _project_headers(
        tenant_id="demo",
        user_id="project-member",
        project_ids=(OTHER_PROJECT_ID,),
    )
    _create_agent(client, logical_agent_id="agent-scope-release", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-scope-release", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-scope-release",
        principal_id="release-contributor",
        role=AgentRole.CONTRIBUTOR,
    )
    contributor_headers = _project_headers(
        tenant_id="demo",
        user_id="release-contributor",
        project_ids=(DEFAULT_PROJECT_ID,),
    )
    approval = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=contributor_headers,
    ).json()

    for headers, project_id in (
        (other_project_headers, OTHER_PROJECT_ID),
        (OTHER_TENANT_HEADERS, DEFAULT_PROJECT_ID),
    ):
        gates = client.post(
            f"/v1/agent-studio/versions/{version['id']}/gates",
            json=_body(project_id, **GATED_EVIDENCE),
            headers=headers,
        )
        assert gates.status_code == 404

        promotion = client.post(
            f"/v1/agent-studio/versions/{version['id']}/promote",
            json=_body(project_id, destination="dev", evidence_summary="blocked"),
            headers=headers,
        )
        assert promotion.status_code == 404

        decision = client.post(
            f"/v1/agent-studio/approvals/{approval['id']}/decision",
            json=_body(project_id, approve=True),
            headers=headers,
        )
        assert decision.status_code == 404


def test_deployment_and_tool_routes_are_cross_project_and_cross_tenant_isolated(
    client: TestClient,
) -> None:
    other_project_headers = _project_headers(
        tenant_id="demo",
        user_id="project-member",
        project_ids=(OTHER_PROJECT_ID,),
    )
    _create_agent(client, logical_agent_id="agent-scope-runtime", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-scope-runtime", headers=USER_HEADERS)
    create_registration = client.post(
        "/v1/agent-studio/agents/agent-scope-runtime/tool-registrations",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            kind="managed_foundry_native",
            handler_ref="builtin://web-search",
        ),
        headers=USER_HEADERS,
    )
    assert create_registration.status_code == 201
    deployment = _deploy_version(
        client,
        logical_agent_id="agent-scope-runtime",
        version_id=version["id"],
        headers=USER_HEADERS,
    )

    for headers, project_id in (
        (other_project_headers, OTHER_PROJECT_ID),
        (OTHER_TENANT_HEADERS, DEFAULT_PROJECT_ID),
    ):
        register = client.post(
            "/v1/agent-studio/agents/agent-scope-runtime/tool-registrations",
            json=_body(
                project_id,
                descriptor_id="foundry.web_search",
                operation="search",
                kind="managed_foundry_native",
                handler_ref="builtin://web-search",
            ),
            headers=headers,
        )
        assert register.status_code == 403

        assert client.get(
            "/v1/agent-studio/agents/agent-scope-runtime/tool-registrations",
            params=_params(project_id),
            headers=headers,
        ).json() == []

        health = client.post(
            f"/v1/agent-studio/deployments/{deployment['id']}/health",
            json=_body(project_id, status="healthy"),
            headers=headers,
        )
        assert health.status_code == 404

        assert client.get(
            "/v1/agent-studio/agents/agent-scope-runtime/deployments",
            params=_params(project_id),
            headers=headers,
        ).json() == []


def test_builder_and_memory_routes_are_cross_project_and_cross_tenant_isolated(
    client: TestClient,
) -> None:
    other_project_headers = _project_headers(
        tenant_id="demo",
        user_id="project-member",
        project_ids=(OTHER_PROJECT_ID,),
    )
    _create_agent(client, logical_agent_id="agent-scope-builder-memory", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-scope-builder-memory", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-scope-builder-memory", headers=USER_HEADERS)
    proposal = client.post(
        "/v1/agent-studio/agents/agent-scope-builder-memory/builder/messages",
        json=_body(message="seed proposal", base_etag=draft["etag"]),
        headers=USER_HEADERS,
    ).json()
    entry = client.post(
        "/v1/agent-studio/agents/agent-scope-builder-memory/memory",
        json=_body(scope_kind="conversation", scope_id="thread-1", content="seed memory"),
        headers=USER_HEADERS,
    ).json()

    for headers, project_id in (
        (other_project_headers, OTHER_PROJECT_ID),
        (OTHER_TENANT_HEADERS, DEFAULT_PROJECT_ID),
    ):
        propose = client.post(
            "/v1/agent-studio/agents/agent-scope-builder-memory/builder/messages",
            json=_body(project_id, message="blocked", base_etag=draft["etag"]),
            headers=headers,
        )
        assert propose.status_code == 403

        assert client.get(
            "/v1/agent-studio/agents/agent-scope-builder-memory/proposals",
            params=_params(project_id),
            headers=headers,
        ).json() == []
        fetch = client.get(
            f"/v1/agent-studio/agents/agent-scope-builder-memory/proposals/{proposal['id']}",
            params=_params(project_id),
            headers=headers,
        )
        assert fetch.status_code == 404

        recall = client.get(
            "/v1/agent-studio/agents/agent-scope-builder-memory/memory",
            params=_params(project_id, scope_kind="conversation", scope_id="thread-1"),
            headers=headers,
        )
        assert recall.status_code == 404

        inspect = client.get(
            f"/v1/agent-studio/agents/agent-scope-builder-memory/memory/{entry['id']}",
            params=_params(project_id),
            headers=headers,
        )
        assert inspect.status_code == 404

        correct = client.put(
            f"/v1/agent-studio/agents/agent-scope-builder-memory/memory/{entry['id']}",
            json=_body(project_id, content="blocked"),
            headers=headers,
        )
        assert correct.status_code == 404

        forget = client.request(
            "DELETE",
            f"/v1/agent-studio/agents/agent-scope-builder-memory/memory/{entry['id']}",
            json=_body(project_id, reason="blocked"),
            headers=headers,
        )
        assert forget.status_code == 404

        export = client.get(
            "/v1/agent-studio/agents/agent-scope-builder-memory/memory-export",
            params=_params(project_id, scope_kind="conversation", scope_id="thread-1"),
            headers=headers,
        )
        assert export.status_code == 404


def test_unavailable_service_routes_return_503(
    unavailable_client: TestClient,
    memory_unavailable_client: TestClient,
    client: TestClient,
) -> None:
    get_draft = unavailable_client.get(
        "/v1/agent-studio/agents/agent-unavailable/draft",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert get_draft.status_code == 503

    create_agent = unavailable_client.post(
        "/v1/agent-studio/agents",
        json=_body(logical_agent_id="agent-unavailable", display_name="Unavailable"),
        headers=USER_HEADERS,
    )
    assert create_agent.status_code == 503

    record_health = unavailable_client.post(
        "/v1/agent-studio/deployments/deployment-1/health",
        json=_body(status="healthy"),
        headers=USER_HEADERS,
    )
    assert record_health.status_code == 503

    resolve = unavailable_client.get(
        "/v1/agent-studio/agents/agent-unavailable/resolve",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert resolve.status_code == 503

    _create_agent(client, logical_agent_id="agent-memory-unavailable", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory-unavailable", headers=USER_HEADERS)
    remember = memory_unavailable_client.post(
        "/v1/agent-studio/agents/agent-memory-unavailable/memory",
        json=_body(
            scope_kind="conversation",
            scope_id="thread-1",
            content="memory unavailable",
        ),
        headers=USER_HEADERS,
    )
    assert remember.status_code == 503


# -- Expanded CapabilityBindingView endpoints --------------------------------
#
# Draft sidecar (``GET /agents/{id}/draft``), per-version capability-views
# (``GET /versions/{id}/capability-views``), and the aggregate workspace view
# (``GET /agents/{id}/workspace``) all expose *volatile, current-state*
# expansions of raw ``CapabilityBinding``s. None of these are the execution
# contract: the canonical manifest/version/contract/resolve surfaces stay
# raw-binding-only.


def test_draft_view_includes_capability_views_sidecar(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    _create_agent(client, logical_agent_id="agent-draft-capability-view", headers=USER_HEADERS)
    registry.register_instance(
        CapabilityInstance(
            id="draft-view-instance-1",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            discovered_provider_version="2026-07-01",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_ai_search",
            "operation": "search",
            "instance_id": "draft-view-instance-1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()
    draft = _get_draft(client, "agent-draft-capability-view", headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, "agent-draft-capability-view", draft["manifest"], headers=USER_HEADERS)

    response = client.get(
        "/v1/agent-studio/agents/agent-draft-capability-view/draft",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "draft" in body
    assert "capability_views" in body
    assert len(body["capability_views"]) == 1
    view = body["capability_views"][0]
    assert view["binding"]["descriptor_ref"]["id"] == "foundry.azure_ai_search"
    assert view["resolved_descriptor"]["id"] == "foundry.azure_ai_search"
    assert view["resolved_instance"]["id"] == "draft-view-instance-1"
    assert view["bindable"] is True
    assert view["stale_reason"] is None
    assert view["resolved_at"] is not None
    # Never usable as a persisted write-back target: capability_views is not
    # accepted as part of the draft/manifest shape on update.
    assert "capability_views" not in body["draft"]["manifest"]


def test_draft_view_is_project_scoped_not_found_for_missing_or_cross_scope(
    client: TestClient,
) -> None:
    missing = client.get(
        "/v1/agent-studio/agents/agent-does-not-exist/draft",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404


def test_version_capability_views_route_returns_expanded_bindings(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    _create_agent(client, logical_agent_id="agent-version-capability-views", headers=USER_HEADERS)
    registry.register_instance(
        CapabilityInstance(
            id="version-view-instance-1",
            tenant_id="demo",
            project_id="default",
            descriptor_id="foundry.azure_ai_search",
            discovered_provider_version="2026-07-01",
            readiness=InstanceReadiness.READY,
            registered_by="platform-owner",
        )
    )
    attach_response = client.post(
        "/v1/agent-studio/capabilities/attach",
        json={
            "descriptor_id": "foundry.azure_ai_search",
            "operation": "search",
            "instance_id": "version-view-instance-1",
        },
        headers=USER_HEADERS,
    )
    assert attach_response.status_code == 200, attach_response.text
    binding = attach_response.json()
    draft = _get_draft(client, "agent-version-capability-views", headers=USER_HEADERS)
    draft["manifest"]["capabilities"] = [binding]
    _update_manifest(client, "agent-version-capability-views", draft["manifest"], headers=USER_HEADERS)
    version = _cut_version(client, "agent-version-capability-views", headers=USER_HEADERS)

    response = client.get(
        f"/v1/agent-studio/versions/{version['id']}/capability-views",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    views = response.json()
    assert len(views) == 1
    assert views[0]["binding"]["descriptor_ref"]["id"] == "foundry.azure_ai_search"
    assert views[0]["bindable"] is True

    missing = client.get(
        "/v1/agent-studio/versions/missing-version/capability-views",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404

    for headers, project_id in (
        (
            _project_headers(tenant_id="demo", user_id="other-project-member", project_ids=(OTHER_PROJECT_ID,)),
            OTHER_PROJECT_ID,
        ),
        (OTHER_TENANT_HEADERS, DEFAULT_PROJECT_ID),
    ):
        cross_scope = client.get(
            f"/v1/agent-studio/versions/{version['id']}/capability-views",
            params=_params(project_id),
            headers=headers,
        )
        assert cross_scope.status_code == 404


def test_agent_workspace_route_aggregates_draft_version_release_and_deployments(
    client: TestClient,
) -> None:
    _create_agent(client, logical_agent_id="agent-workspace", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-workspace", headers=USER_HEADERS)
    deployment = _deploy_version(
        client, logical_agent_id="agent-workspace", version_id=version["id"], headers=USER_HEADERS
    )

    response = client.get(
        "/v1/agent-studio/agents/agent-workspace/workspace",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["logical_agent_id"] == "agent-workspace"
    assert body["draft"] is not None
    assert body["latest_version"]["id"] == version["id"]
    assert body["latest_release"] is not None
    assert body["latest_release"]["version_id"] == version["id"]
    assert [record["id"] for record in body["deployments"]] == [deployment["id"]]
    assert body["capability_views"] == []


def test_agent_workspace_route_handles_agent_without_any_version_yet(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-workspace-fresh", headers=USER_HEADERS)

    response = client.get(
        "/v1/agent-studio/agents/agent-workspace-fresh/workspace",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["latest_version"] is None
    assert body["latest_release"] is None
    assert body["deployments"] == []


def test_agent_workspace_route_is_not_found_for_missing_or_cross_scope_agent(
    client: TestClient,
) -> None:
    missing = client.get(
        "/v1/agent-studio/agents/agent-workspace-missing/workspace",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404

    _create_agent(client, logical_agent_id="agent-workspace-scoped", headers=USER_HEADERS)
    cross_tenant = client.get(
        "/v1/agent-studio/agents/agent-workspace-scoped/workspace",
        params=_params(),
        headers=OTHER_TENANT_HEADERS,
    )
    assert cross_tenant.status_code == 404


# -- Approval get/revoke routes -----------------------------------------------


def test_get_approval_route_returns_effective_state_and_is_scope_authorized(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-approval-get", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-approval-get",
        principal_id="contributor-get",
        role=AgentRole.CONTRIBUTOR,
    )
    contributor_headers = _project_headers(
        tenant_id="demo", user_id="contributor-get", project_ids=(DEFAULT_PROJECT_ID,)
    )
    version = _cut_gated_version(client, "agent-approval-get", headers=USER_HEADERS)
    promotion = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=contributor_headers,
    )
    assert promotion.status_code == 200, promotion.text
    approval = promotion.json()
    assert approval["state"] == "pending"

    response = client.get(
        f"/v1/agent-studio/approvals/{approval['id']}",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record"]["id"] == approval["id"]
    assert body["effective_state"] == "pending"
    assert body["revocations"] == []

    missing = client.get(
        "/v1/agent-studio/approvals/missing-approval",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404

    for headers, project_id in (
        (
            _project_headers(tenant_id="demo", user_id="other-project-member", project_ids=(OTHER_PROJECT_ID,)),
            OTHER_PROJECT_ID,
        ),
        (OTHER_TENANT_HEADERS, DEFAULT_PROJECT_ID),
    ):
        cross_scope = client.get(
            f"/v1/agent-studio/approvals/{approval['id']}",
            params=_params(project_id),
            headers=headers,
        )
        assert cross_scope.status_code == 404


def test_revoke_approval_route_self_revocation_then_blocks_further_decisions(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    """The original requester can always revoke their own request; once
    revoked, the approval permanently shows ``effective_state: revoked``
    and can never again be decided (409, not silently allowed)."""
    _create_agent(client, logical_agent_id="agent-approval-self-revoke", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-approval-self-revoke",
        principal_id="contributor-self-revoke",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo", user_id="contributor-self-revoke", project_ids=(DEFAULT_PROJECT_ID,)
    )
    version = _cut_gated_version(client, "agent-approval-self-revoke", headers=USER_HEADERS)
    promotion = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=requester_headers,
    )
    assert promotion.status_code == 200, promotion.text
    approval = promotion.json()
    assert approval["state"] == "pending"

    revoke = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="Changed my mind."),
        headers=USER_HEADERS,
    )
    assert revoke.status_code == 200, revoke.text
    body = revoke.json()
    assert body["effective_state"] == "revoked"
    assert len(body["revocations"]) == 1
    assert body["revocations"][0]["reason"] == "Changed my mind."

    # Idempotent: revoking again under a distinct request still shows the
    # permanent revoked state and does not error.
    reread = client.get(
        f"/v1/agent-studio/approvals/{approval['id']}",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert reread.json()["effective_state"] == "revoked"

    decision_after_revoke = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True),
        headers=USER_HEADERS,
    )
    assert decision_after_revoke.status_code == 409
    assert "revoked" in decision_after_revoke.json()["detail"]


def test_revoke_approval_route_enforces_minimum_role_for_non_requester_actors(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-approval-revoke-role", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-approval-revoke-role",
        principal_id="requester-2",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo", user_id="requester-2", project_ids=(DEFAULT_PROJECT_ID,)
    )
    version = _cut_gated_version(client, "agent-approval-revoke-role", headers=USER_HEADERS)
    promotion = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=requester_headers,
    )
    assert promotion.status_code == 200, promotion.text
    approval = promotion.json()

    viewer_revoke = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="Insufficient role."),
        headers=VIEWER_HEADERS,
    )
    assert viewer_revoke.status_code == 403

    owner_revoke = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="Owner can revoke."),
        headers=USER_HEADERS,
    )
    assert owner_revoke.status_code == 200, owner_revoke.text
    assert owner_revoke.json()["effective_state"] == "revoked"


def test_revoke_approval_route_platform_owner_bypasses_role_check(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-approval-revoke-platform", headers=USER_HEADERS)
    _grant_role(
        store,
        logical_agent_id="agent-approval-revoke-platform",
        principal_id="contributor-platform-revoke",
        role=AgentRole.CONTRIBUTOR,
    )
    requester_headers = _project_headers(
        tenant_id="demo", user_id="contributor-platform-revoke", project_ids=(DEFAULT_PROJECT_ID,)
    )
    version = _cut_gated_version(client, "agent-approval-revoke-platform", headers=USER_HEADERS)
    promotion = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="needs approval"),
        headers=requester_headers,
    )
    assert promotion.status_code == 200, promotion.text
    approval = promotion.json()
    assert approval["state"] == "pending"

    platform_revoke = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="Platform override."),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert platform_revoke.status_code == 200, platform_revoke.text
    assert platform_revoke.json()["effective_state"] == "revoked"


def test_revoke_approval_route_is_not_found_for_missing_or_cross_scope_approval(
    client: TestClient,
) -> None:
    missing = client.post(
        "/v1/agent-studio/approvals/missing-approval/revoke",
        json=_body(reason="no such approval"),
        headers=USER_HEADERS,
    )
    assert missing.status_code == 404


def test_audit_events_recorded_for_agent_lifecycle_routes(
    client: TestClient,
    audit_service: AuditService,
) -> None:
    """Every wired mutation route in the create->update->register->cut->
    gate->fork->promote->deploy->health->activate->rollback lifecycle
    appends exactly the expected ``AuditEvent`` kind(s), with the correct
    actor/subject/logical_agent_id/detail -- proving the ``_audit`` call
    sites are reachable and semantically correct, not merely present in the
    source.
    """
    scope = _scope()

    _create_agent(client, logical_agent_id="agent-audit-lifecycle", headers=USER_HEADERS)
    created_events = audit_service.list_events(scope=scope, logical_agent_id="agent-audit-lifecycle")
    assert [e.kind for e in created_events] == [
        AuditEventKind.DRAFT_CREATED,
        AuditEventKind.OWNERSHIP_GRANTED,
    ]
    assert created_events[0].actor_id == "user-1"
    assert created_events[0].subject_id == "agent-audit-lifecycle"
    assert created_events[1].subject_id == "user-1"
    assert created_events[1].detail["role"] == AgentRole.OWNER.value

    draft = _get_draft(client, "agent-audit-lifecycle", headers=USER_HEADERS)
    draft["manifest"]["description"] = "updated via audit test"
    updated = _update_manifest(client, "agent-audit-lifecycle", draft["manifest"], headers=USER_HEADERS)
    update_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.DRAFT_UPDATED
    )
    assert len(update_events) == 1
    assert update_events[0].subject_id == "agent-audit-lifecycle"
    assert update_events[0].detail["etag"] == updated["etag"]

    register_response = client.post(
        "/v1/agent-studio/agents/agent-audit-lifecycle/tool-registrations",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            kind="managed_foundry_native",
            handler_ref="builtin://web-search",
        ),
        headers=USER_HEADERS,
    )
    assert register_response.status_code == 201, register_response.text
    spec = register_response.json()
    tool_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.TOOL_REGISTERED
    )
    assert len(tool_events) == 1
    assert tool_events[0].subject_id == spec["id"]

    version = _cut_version(client, "agent-audit-lifecycle", headers=USER_HEADERS)
    cut_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.RELEASE_CUT
    )
    assert len(cut_events) == 1
    assert cut_events[0].subject_id == version["id"]
    assert cut_events[0].detail["sequence"] == str(version["sequence"])

    report = _run_gates(client, version["id"], headers=USER_HEADERS, evidence=GATED_EVIDENCE)
    assert all(r["status"] in {"passed", "not_applicable"} for r in report["results"])
    gate_pass_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.GATE_PASSED
    )
    assert len(gate_pass_events) == 1
    assert gate_pass_events[0].subject_id == report["id"]
    assert audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.POLICY_GATE_FAILED
    ) == ()

    fork_response = client.post(
        "/v1/agent-studio/agents/agent-audit-lifecycle/fork",
        json=_body(source_version_id=version["id"], new_logical_agent_id="agent-audit-lifecycle-fork"),
        headers=USER_HEADERS,
    )
    assert fork_response.status_code == 201, fork_response.text
    fork_events = audit_service.list_events(scope=scope, logical_agent_id="agent-audit-lifecycle-fork")
    assert [e.kind for e in fork_events] == [
        AuditEventKind.DRAFT_FORKED,
        AuditEventKind.OWNERSHIP_GRANTED,
    ]
    assert fork_events[0].subject_id == "agent-audit-lifecycle-fork"
    assert fork_events[0].detail["source_logical_agent_id"] == "agent-audit-lifecycle"
    assert fork_events[1].subject_id == "user-1"

    promote_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="ship it"),
        headers=USER_HEADERS,
    )
    assert promote_response.status_code == 200, promote_response.text
    promotion_events = audit_service.list_events(
        scope=scope,
        logical_agent_id="agent-audit-lifecycle",
        kind=AuditEventKind.RELEASE_PROMOTION_REQUESTED,
    )
    assert len(promotion_events) == 1
    assert promotion_events[0].detail["destination"] == "dev"
    assert promotion_events[0].detail["version_id"] == version["id"]

    deployment = _deploy_version(
        client,
        logical_agent_id="agent-audit-lifecycle",
        version_id=version["id"],
        headers=USER_HEADERS,
    )
    deploy_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.DEPLOYMENT_CREATED
    )
    assert len(deploy_events) == 1
    assert deploy_events[0].subject_id == deployment["id"]

    health_response = client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy", detail="smoke ok"),
        headers=USER_HEADERS,
    )
    assert health_response.status_code == 200, health_response.text
    health_events = audit_service.list_events(
        scope=scope,
        logical_agent_id="agent-audit-lifecycle",
        kind=AuditEventKind.DEPLOYMENT_HEALTH_RECORDED,
    )
    assert len(health_events) == 1
    assert health_events[0].subject_id == deployment["id"]
    assert health_events[0].detail["status"] == HealthStatus.HEALTHY.value

    activate_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/activate",
        json=_body(),
        headers=USER_HEADERS,
    )
    assert activate_response.status_code == 200, activate_response.text
    release = activate_response.json()
    activate_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-lifecycle", kind=AuditEventKind.RELEASE_ACTIVATED
    )
    assert len(activate_events) == 1
    assert activate_events[0].subject_id == release["id"]

    draft2 = _get_draft(client, "agent-audit-lifecycle", headers=USER_HEADERS)
    draft2["manifest"]["description"] = "second version for rollback"
    _update_manifest(client, "agent-audit-lifecycle", draft2["manifest"], headers=USER_HEADERS)
    second_version = _cut_gated_version(client, "agent-audit-lifecycle", headers=USER_HEADERS)
    second_deployment = _deploy_version(
        client,
        logical_agent_id="agent-audit-lifecycle",
        version_id=second_version["id"],
        headers=USER_HEADERS,
    )
    rollback_response = client.post(
        "/v1/agent-studio/agents/agent-audit-lifecycle/rollback",
        json=_body(deployment_id=second_deployment["id"], target_version_id=version["id"]),
        headers=USER_HEADERS,
    )
    assert rollback_response.status_code == 201, rollback_response.text
    rollback_record = rollback_response.json()
    rollback_events = audit_service.list_events(
        scope=scope,
        logical_agent_id="agent-audit-lifecycle",
        kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK,
    )
    assert len(rollback_events) == 1
    assert rollback_events[0].subject_id == rollback_record["id"]
    assert rollback_events[0].detail["target_version_id"] == version["id"]
    assert rollback_events[0].detail["from_deployment_id"] == second_deployment["id"]


def test_audit_events_recorded_for_gate_failure_and_capability_approval_lifecycle(
    client: TestClient,
    audit_service: AuditService,
    store: AgentStudioStore,
) -> None:
    """``run_gates`` failure emits ``POLICY_GATE_FAILED`` (not ``GATE_PASSED``),
    and the full capability-approval lifecycle (request/decide/revoke/
    consume) each append their own distinct ``AuditEvent``.
    """
    scope = _scope()

    _create_agent(client, logical_agent_id="agent-audit-gates", headers=USER_HEADERS)
    ungated_version = _cut_version(client, "agent-audit-gates", headers=USER_HEADERS)
    failing_report = _run_gates(
        client,
        ungated_version["id"],
        headers=USER_HEADERS,
        evidence={"evidence": {"tests_passed": False, "test_detail": "unit tests failed"}},
    )
    assert any(r["status"] == "failed" for r in failing_report["results"])
    failed_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-gates", kind=AuditEventKind.POLICY_GATE_FAILED
    )
    assert len(failed_events) == 1
    assert failed_events[0].subject_id == failing_report["id"]
    assert audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-gates", kind=AuditEventKind.GATE_PASSED
    ) == ()

    binding, approval = _setup_approved_capability_approval(
        client, store, logical_agent_id="agent-audit-approval"
    )
    request_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-approval", kind=AuditEventKind.APPROVAL_REQUESTED
    )
    assert len(request_events) == 1
    assert request_events[0].subject_id == approval["id"]
    assert request_events[0].detail["descriptor_id"] == "foundry.azure_functions"

    decided_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-approval", kind=AuditEventKind.APPROVAL_DECIDED
    )
    assert len(decided_events) == 1
    assert decided_events[0].subject_id == approval["id"]
    assert decided_events[0].detail["approved"] == "True"
    assert decided_events[0].detail["state"] == "approved"

    consume_response = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert consume_response.status_code == 200, consume_response.text
    consumed_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-approval", kind=AuditEventKind.APPROVAL_CONSUMED
    )
    assert len(consumed_events) == 1
    assert consumed_events[0].subject_id == approval["id"]
    assert consumed_events[0].detail["outcome"] == "consumed"
    assert consumed_events[0].detail["binding_id"] == binding["binding_id"]

    revoke_response = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="no longer needed"),
        headers=USER_HEADERS,
    )
    assert revoke_response.status_code == 200, revoke_response.text
    revoked_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-approval", kind=AuditEventKind.APPROVAL_REVOKED
    )
    assert len(revoked_events) == 1
    assert revoked_events[0].subject_id == approval["id"]
    assert revoked_events[0].detail["reason"] == "no longer needed"


def test_audit_events_recorded_for_escalation_and_builder_apply_routes(
    client: TestClient,
    audit_service: AuditService,
    store: AgentStudioStore,
) -> None:
    """``request_escalation`` -> ``APPROVAL_REQUESTED``; approving an
    ``ADMIN_ESCALATION`` decision also appends a follow-on
    ``OWNERSHIP_GRANTED`` for the grantee; applying a builder proposal
    appends ``BUILDER_PROPOSAL_APPLIED``.
    """
    scope = _scope()

    _create_agent(client, logical_agent_id="agent-audit-escalation", headers=USER_HEADERS)
    request_response = client.post(
        "/v1/agent-studio/agents/agent-audit-escalation/escalations",
        json=_body(requested_role="maintainer", evidence_summary="need write access"),
        headers=VIEWER_HEADERS,
    )
    assert request_response.status_code == 201, request_response.text
    approval = request_response.json()
    escalation_request_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-escalation", kind=AuditEventKind.APPROVAL_REQUESTED
    )
    assert len(escalation_request_events) == 1
    assert escalation_request_events[0].subject_id == approval["id"]
    assert escalation_request_events[0].actor_id == "viewer-1"
    assert escalation_request_events[0].detail["requested_role"] == AgentRole.MAINTAINER.value

    decide_response = client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=True, rationale="approved"),
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert decide_response.status_code == 200, decide_response.text
    decided_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-escalation", kind=AuditEventKind.APPROVAL_DECIDED
    )
    assert len(decided_events) == 1
    assert decided_events[0].subject_id == approval["id"]
    assert decided_events[0].actor_id == "platform-owner"

    escalation_grant_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-escalation", kind=AuditEventKind.OWNERSHIP_GRANTED
    )
    # One from ``create_agent`` (owner) plus one from this escalation grant.
    assert len(escalation_grant_events) == 2
    escalation_grant = next(e for e in escalation_grant_events if e.subject_id == "viewer-1")
    assert escalation_grant.detail["role"] == AgentRole.MAINTAINER.value
    assert escalation_grant.detail["via_approval_id"] == approval["id"]

    _create_agent(client, logical_agent_id="agent-audit-builder", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-audit-builder", headers=USER_HEADERS)
    propose = client.post(
        "/v1/agent-studio/agents/agent-audit-builder/builder/messages",
        json=_body(message="Add a helpful description.", base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert propose.status_code == 201, propose.text
    proposal = propose.json()
    apply_response = client.post(
        f"/v1/agent-studio/agents/agent-audit-builder/proposals/{proposal['id']}/apply",
        json=_body(base_etag=draft["etag"]),
        headers=USER_HEADERS,
    )
    assert apply_response.status_code == 200, apply_response.text
    updated_draft = apply_response.json()
    applied_events = audit_service.list_events(
        scope=scope, logical_agent_id="agent-audit-builder", kind=AuditEventKind.BUILDER_PROPOSAL_APPLIED
    )
    assert len(applied_events) == 1
    assert applied_events[0].subject_id == proposal["id"]
    assert applied_events[0].detail["draft_etag"] == updated_draft["etag"]


def test_audit_unavailable_client_fails_closed_before_every_wired_mutation(
    client: TestClient,
    audit_unavailable_client: TestClient,
    store: AgentStudioStore,
) -> None:
    """Every wired mutation route refuses with 503 -- and performs no
    domain mutation at all -- when the composed ``AuditService`` is
    unavailable, proving ``_audit_service(request)`` is resolved (and
    fails closed) *before* any of these routes call into the domain
    service. Prerequisite state (agents/versions/deployments/approvals/
    proposals) is built with the normal ``client`` fixture, which shares
    the same underlying ``store``/services with ``audit_unavailable_client``.
    """
    # create_agent: no precondition; refuses before any draft is persisted.
    create_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents",
        json={
            "logical_agent_id": "agent-audit-unavailable",
            "project_id": DEFAULT_PROJECT_ID,
            "display_name": "Should not be created",
            "description": "",
            "owner_kind": "user",
        },
        headers=USER_HEADERS,
    )
    assert create_response.status_code == 503
    assert store.get_draft(_scope(), "agent-audit-unavailable") is None

    # Build prerequisite state with the normal (audit-available) client.
    _create_agent(client, logical_agent_id="agent-audit-503", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-audit-503", headers=USER_HEADERS)

    update_response = audit_unavailable_client.put(
        "/v1/agent-studio/agents/agent-audit-503/draft",
        json={"manifest": draft["manifest"]},
        headers={**USER_HEADERS, "If-Match": draft["etag"]},
    )
    assert update_response.status_code == 503
    assert _get_draft(client, "agent-audit-503", headers=USER_HEADERS)["etag"] == draft["etag"]

    register_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/tool-registrations",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            kind="managed_foundry_native",
            handler_ref="builtin://web-search",
        ),
        headers=USER_HEADERS,
    )
    assert register_response.status_code == 503
    assert (
        client.get(
            "/v1/agent-studio/agents/agent-audit-503/tool-registrations",
            params=_params(),
            headers=USER_HEADERS,
        ).json()
        == []
    )

    cut_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/versions",
        params=_params(),
        headers=USER_HEADERS,
    )
    assert cut_response.status_code == 503
    assert (
        client.get(
            "/v1/agent-studio/agents/agent-audit-503/versions",
            params=_params(),
            headers=USER_HEADERS,
        ).json()
        == []
    )

    # Cut a real version via the available client for the remaining routes.
    version = _cut_version(client, "agent-audit-503", headers=USER_HEADERS)

    gates_response = audit_unavailable_client.post(
        f"/v1/agent-studio/versions/{version['id']}/gates",
        json={"project_id": DEFAULT_PROJECT_ID, **GATED_EVIDENCE},
        headers=USER_HEADERS,
    )
    assert gates_response.status_code == 503
    assert store.get_version(_scope(), version["id"]) is not None

    fork_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/fork",
        json=_body(source_version_id=version["id"], new_logical_agent_id="agent-audit-503-fork"),
        headers=USER_HEADERS,
    )
    assert fork_response.status_code == 503
    assert store.get_draft(_scope(), "agent-audit-503-fork") is None

    # Gate the version via the available client so promote/deploy/activate work.
    gated_report = _run_gates(client, version["id"], headers=USER_HEADERS, evidence=GATED_EVIDENCE)
    assert all(r["status"] in {"passed", "not_applicable"} for r in gated_report["results"])

    promote_response = audit_unavailable_client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="should not promote"),
        headers=USER_HEADERS,
    )
    assert promote_response.status_code == 503
    # ``gated_report`` above (via the available client) already created a
    # GATED ``AgentRelease``; the refused promote must not have advanced it.
    unpromoted_release = store.latest_release_for_version(_scope(), version["id"])
    assert unpromoted_release is not None
    assert unpromoted_release.status is ReleaseStatus.GATED

    escalation_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/escalations",
        json=_body(requested_role="maintainer", evidence_summary="need write access"),
        headers=VIEWER_HEADERS,
    )
    assert escalation_response.status_code == 503

    capability_approval_response = audit_unavailable_client.post(
        f"/v1/agent-studio/versions/{version['id']}/capability-approvals",
        json=_body(
            descriptor_id="foundry.web_search",
            operation="search",
            evidence_summary="should not be recorded",
        ),
        headers=USER_HEADERS,
    )
    assert capability_approval_response.status_code == 503

    deploy_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/deployments",
        json=_body(version_id=version["id"]),
        headers=USER_HEADERS,
    )
    assert deploy_response.status_code == 503
    assert (
        client.get(
            "/v1/agent-studio/agents/agent-audit-503/deployments",
            params=_params(),
            headers=USER_HEADERS,
        ).json()
        == []
    )

    # Deploy for real via the available client so health/rollback/activate can be exercised.
    deployment = _deploy_version(
        client, logical_agent_id="agent-audit-503", version_id=version["id"], headers=USER_HEADERS
    )

    health_response = audit_unavailable_client.post(
        f"/v1/agent-studio/deployments/{deployment['id']}/health",
        json=_body(status="healthy"),
        headers=USER_HEADERS,
    )
    assert health_response.status_code == 503

    activate_response = audit_unavailable_client.post(
        f"/v1/agent-studio/versions/{version['id']}/activate",
        json=_body(),
        headers=USER_HEADERS,
    )
    assert activate_response.status_code == 503
    release_after_activate_attempt = store.latest_release_for_version(_scope(), version["id"])
    assert release_after_activate_attempt is not None
    assert release_after_activate_attempt.status is not ReleaseStatus.ACTIVE

    rollback_response = audit_unavailable_client.post(
        "/v1/agent-studio/agents/agent-audit-503/rollback",
        json=_body(deployment_id=deployment["id"], target_version_id=version["id"]),
        headers=USER_HEADERS,
    )
    assert rollback_response.status_code == 503

    # Real approval + real proposal via the available client, then verify the
    # unavailable client refuses to decide/revoke/consume/apply them.
    binding, approval = _setup_approved_capability_approval(
        client, store, logical_agent_id="agent-audit-503-approval"
    )

    decide_response = audit_unavailable_client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/decision",
        json=_body(approve=False),
        headers=USER_HEADERS,
    )
    assert decide_response.status_code == 503
    approval_after_decide_attempt = store.get_approval(_scope(), approval["id"])
    assert approval_after_decide_attempt is not None
    assert approval_after_decide_attempt.state.value == "approved"

    revoke_response = audit_unavailable_client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/revoke",
        json=_body(reason="should not be recorded"),
        headers=USER_HEADERS,
    )
    assert revoke_response.status_code == 503
    assert store.list_revocations(_scope(), approval["id"]) == ()

    consume_response = audit_unavailable_client.post(
        f"/v1/agent-studio/approvals/{approval['id']}/consume",
        json=_consume_body(binding_id=binding["binding_id"]),
        headers=USER_HEADERS,
    )
    assert consume_response.status_code == 503

    _create_agent(client, logical_agent_id="agent-audit-503-builder", headers=USER_HEADERS)
    builder_draft = _get_draft(client, "agent-audit-503-builder", headers=USER_HEADERS)
    builder_proposal = client.post(
        "/v1/agent-studio/agents/agent-audit-503-builder/builder/messages",
        json=_body(message="Add a helpful description.", base_etag=builder_draft["etag"]),
        headers=USER_HEADERS,
    ).json()
    apply_response = audit_unavailable_client.post(
        f"/v1/agent-studio/agents/agent-audit-503-builder/proposals/{builder_proposal['id']}/apply",
        json=_body(base_etag=builder_draft["etag"]),
        headers=USER_HEADERS,
    )
    assert apply_response.status_code == 503
    assert _get_draft(client, "agent-audit-503-builder", headers=USER_HEADERS)["etag"] == builder_draft["etag"]
