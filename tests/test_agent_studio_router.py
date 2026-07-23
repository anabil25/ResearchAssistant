"""Integration tests for the Agent Studio FastAPI router."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.artifact_bundle_store import InMemoryArtifactBundleStore
from research_assistant_api.agent_studio.builder_service import (
    BuilderService,
    InMemoryManifestProposalGenerator,
    ProposedManifestChange,
    UnavailableManifestProposalGenerator,
)
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityRegistry,
    default_registry,
)
from research_assistant_api.agent_studio.deployment_service import DeploymentService
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
    CapabilityInstance,
    DeploymentEnvironment,
    HealthStatus,
    InstanceReadiness,
    MemoryMechanism,
    MemoryScopeKind,
    ModelDeploymentRef,
    OwnershipGrant,
    ReleaseStatus,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.release_service import AuthorizationError, ReleaseService
from research_assistant_api.agent_studio.router import router as agent_studio_router
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
    return default_registry()


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
def builder_service(store: AgentStudioStore) -> BuilderService:
    def _transform(manifest: AgentManifest, message: str) -> ProposedManifestChange:
        return ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"description": message}),
            generator="test-builder-generator",
        )

    return BuilderService(store, InMemoryManifestProposalGenerator(_transform), InMemoryArtifactBundleStore())


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
    )
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
    response = client.get(
        f"/v1/agent-studio/agents/{logical_agent_id}/draft",
        params={"project_id": project_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def _update_manifest(
    client: TestClient,
    logical_agent_id: str,
    manifest: dict[str, Any],
    *,
    headers: dict[str, str] = USER_HEADERS,
) -> dict[str, Any]:
    response = client.put(
        f"/v1/agent-studio/agents/{logical_agent_id}/draft",
        json={"manifest": manifest},
        headers=headers,
    )
    assert response.status_code == 200, response.text
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
    assert body["descriptor_id"] == "foundry.azure_ai_search"
    assert body["operation"] == "search"
    assert body["instance_id"] == "search-instance-1"
    assert body["connection_ref"] == "conn://azure-ai-search"
    assert body["policy_ref"] == "policy://grounding"
    assert body["pinned_provider_version"] == "2026-07-01"
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
        headers=VIEWER_HEADERS,
    )
    assert viewer_response.status_code == 403

    mismatch_manifest = dict(draft["manifest"])
    mismatch_manifest["logical_agent_id"] = "agent-other"
    mismatch_response = client.put(
        "/v1/agent-studio/agents/agent-draft/draft",
        json={"manifest": mismatch_manifest},
        headers=USER_HEADERS,
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
        headers=USER_HEADERS,
    )
    assert no_draft_response.status_code == 404


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

    viewer_cut_response = client.post(
        "/v1/agent-studio/agents/agent-versioned/versions",
        params=_params(),
        headers=VIEWER_HEADERS,
    )
    assert viewer_cut_response.status_code == 403


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

    auto_response = client.post(
        f"/v1/agent-studio/versions/{version['id']}/promote",
        json=_body(destination="dev", evidence_summary="ship it"),
        headers=USER_HEADERS,
    )
    assert auto_response.status_code == 200
    assert auto_response.json()["id"] == version["id"]
    active_release = store.latest_release_for_version(_scope(), version["id"])
    assert active_release is not None
    assert active_release.status is ReleaseStatus.ACTIVE

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
    reactivated_release = store.latest_release_for_version(_scope(), pending_version["id"])
    assert reactivated_release is not None
    assert reactivated_release.status is ReleaseStatus.ACTIVE

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
        headers=VIEWER_HEADERS,
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
    assert resolved["capability_versions"] == {"foundry.web_search": "1"}
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
) -> None:
    unavailable_generator_service = BuilderService(
        store,
        UnavailableManifestProposalGenerator(),
        InMemoryArtifactBundleStore(),
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

    cases = [
        ("GET", f"/v1/agent-studio/agents/agent-isolation/draft?project_id={DEFAULT_PROJECT_ID}", None, 404),
        (
            "PUT",
            "/v1/agent-studio/agents/agent-isolation/draft",
            {"manifest": _minimal_manifest(logical_agent_id="agent-isolation", tenant_id="other-tenant")},
            403,
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/fork",
            _body(source_version_id=version["id"], new_logical_agent_id="agent-isolation-fork"),
            409,
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
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/tool-registrations?project_id={DEFAULT_PROJECT_ID}",
            None,
            200,
        ),
        ("GET", f"/v1/agent-studio/agents/agent-isolation/versions?project_id={DEFAULT_PROJECT_ID}", None, 200),
        ("GET", f"/v1/agent-studio/agents/agent-isolation/lineage?project_id={DEFAULT_PROJECT_ID}", None, 200),
        ("POST", f"/v1/agent-studio/versions/{version['id']}/gates", _body(**GATED_EVIDENCE), 404),
        (
            "POST",
            f"/v1/agent-studio/versions/{version['id']}/promote",
            _body(destination="dev", evidence_summary="no access"),
            404,
        ),
        (
            "POST",
            f"/v1/agent-studio/approvals/{approval['id']}/decision",
            _body(approve=True),
            404,
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/deployments",
            _body(version_id=version["id"]),
            409,
        ),
        ("GET", f"/v1/agent-studio/agents/agent-isolation/deployments?project_id={DEFAULT_PROJECT_ID}", None, 200),
        (
            "POST",
            f"/v1/agent-studio/deployments/{deployment['id']}/health",
            _body(status="healthy"),
            404,
        ),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/rollback",
            _body(deployment_id=deployment["id"], target_version_id=version["id"]),
            409,
        ),
        ("GET", f"/v1/agent-studio/agents/agent-isolation/resolve?project_id={DEFAULT_PROJECT_ID}", None, 404),
        ("GET", f"/v1/agent-studio/versions/{version['id']}/contract?project_id={DEFAULT_PROJECT_ID}", None, 404),
        ("GET", f"/v1/agent-studio/catalog?project_id={DEFAULT_PROJECT_ID}", None, 200),
        (
            "POST",
            "/v1/agent-studio/agents/agent-isolation/memory",
            _body(scope_kind="conversation", scope_id="thread-iso", content="no access"),
            404,
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory?project_id={DEFAULT_PROJECT_ID}&scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}?project_id={DEFAULT_PROJECT_ID}",
            None,
            404,
        ),
        (
            "PUT",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            _body(content="blocked"),
            404,
        ),
        (
            "DELETE",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            _body(reason="blocked"),
            404,
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory-export?project_id={DEFAULT_PROJECT_ID}&scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
        ),
        (
            "GET",
            f"/v1/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}/audit?project_id={DEFAULT_PROJECT_ID}",
            None,
            404,
        ),
    ]

    for method, path, payload, expected_status in cases:
        response = client.request(
            method,
            path,
            json=payload,
            headers=OTHER_TENANT_HEADERS,
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
        headers=other_project_headers,
    )
    assert cross_project_update.status_code == 403

    cross_tenant_update = client.put(
        "/v1/agent-studio/agents/agent-scope-draft/draft",
        json={"manifest": _minimal_manifest(logical_agent_id="agent-scope-draft")},
        headers=OTHER_TENANT_HEADERS,
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
