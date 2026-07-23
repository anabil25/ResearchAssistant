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
from research_assistant_api.agent_studio.release_service import ReleaseService
from research_assistant_api.agent_studio.router import router as agent_studio_router
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings

GATED_EVIDENCE = {
    "evidence": {
        "build_succeeded": True,
        "tests_passed": True,
        "smoke_passed": True,
    }
}


def _principal(*, tenant_id: str, user_id: str, groups: tuple[str, ...] = ()) -> str:
    payload = {
        "userId": user_id,
        "userDetails": user_id,
        "claims": [
            {"typ": "tid", "val": tenant_id},
            *({"typ": "groups", "val": group} for group in groups),
        ],
    }
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")


def _headers(*, tenant_id: str, user_id: str, groups: tuple[str, ...] = ()) -> dict[str, str]:
    return {
        "x-ms-client-principal": _principal(
            tenant_id=tenant_id,
            user_id=user_id,
            groups=groups,
        )
    }


PLATFORM_OWNER_HEADERS = _headers(
    tenant_id="demo",
    user_id="platform-owner",
    groups=("research-admins",),
)
USER_HEADERS = _headers(tenant_id="demo", user_id="user-1")
VIEWER_HEADERS = _headers(tenant_id="demo", user_id="viewer-1")
OTHER_TENANT_HEADERS = _headers(
    tenant_id="other-tenant",
    user_id="other-user",
    groups=("research-admins",),
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
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
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
) -> Iterator[TestClient]:
    app = _build_app(
        settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=None,
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
) -> Iterator[TestClient]:
    app = _build_app(
        locked_settings,
        store=store,
        registry=registry,
        model_discovery=model_discovery,
        release_service=release_service,
        deployment_service=deployment_service,
        memory_service=memory_service,
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
    project_id: str = "default",
) -> dict[str, Any]:
    manifest = AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        display_name=display_name,
        owner_kind=owner_kind,
        owner_id=owner_id,
    )
    return cast("dict[str, Any]", manifest.model_dump(mode="json"))


def _create_agent(
    client: TestClient,
    *,
    logical_agent_id: str,
    headers: dict[str, str] = USER_HEADERS,
    owner_kind: str = "user",
    display_name: str = "Router Test Agent",
    description: str = "",
) -> dict[str, Any]:
    response = client.post(
        "/api/agent-studio/agents",
        json={
            "logical_agent_id": logical_agent_id,
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
) -> dict[str, Any]:
    response = client.get(
        f"/api/agent-studio/agents/{logical_agent_id}/draft",
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
        f"/api/agent-studio/agents/{logical_agent_id}/draft",
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
    project_id: str | None = None,
) -> None:
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo",
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
) -> dict[str, Any]:
    response = client.post(
        f"/api/agent-studio/agents/{logical_agent_id}/versions",
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return cast("dict[str, Any]", response.json())


def _run_gates(
    client: TestClient,
    version_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.post(
        f"/api/agent-studio/versions/{version_id}/gates",
        json=evidence or GATED_EVIDENCE,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def _cut_gated_version(
    client: TestClient,
    logical_agent_id: str,
    *,
    headers: dict[str, str] = USER_HEADERS,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    version = _cut_version(client, logical_agent_id, headers=headers)
    report = _run_gates(client, version["id"], headers=headers, evidence=evidence)
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
    trace_ref: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"version_id": version_id}
    if trace_ref is not None:
        payload["trace_ref"] = trace_ref
    response = client.post(
        f"/api/agent-studio/agents/{logical_agent_id}/deployments",
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
) -> dict[str, Any]:
    draft = _get_draft(client, logical_agent_id, headers=headers)
    draft["manifest"]["memory_policy"] = {
        "enabled": True,
        "scopes": [{"kind": scope_kind, "mechanism": mechanism}],
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
    response = unauthenticated_client.get("/api/agent-studio/capabilities")
    assert response.status_code == 401
    assert "authenticated platform identity" in response.json()["detail"]


def test_list_capabilities_and_attach_cover_catalog_and_new_request_shape(
    client: TestClient,
    registry: CapabilityRegistry,
) -> None:
    list_response = client.get(
        "/api/agent-studio/capabilities",
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
        "/api/agent-studio/capabilities/attach",
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
        "/api/agent-studio/capabilities/attach",
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
        "/api/agent-studio/capabilities/attach",
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
        "/api/agent-studio/schemas/agent-manifest",
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
    ok_response = client.get("/api/agent-studio/models", headers=USER_HEADERS)
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
        "/api/agent-studio/models",
        headers=USER_HEADERS,
    )
    assert unavailable_response.status_code == 503
    assert "model discovery is unavailable" in unavailable_response.json()["detail"]


def test_create_agent_covers_validation_authz_and_duplicates(client: TestClient) -> None:
    bad_response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "bad", "display_name": "Bad"},
        headers=USER_HEADERS,
    )
    assert bad_response.status_code == 422

    user_agent = _create_agent(client, logical_agent_id="agent-create-user", headers=USER_HEADERS)
    assert user_agent["manifest"]["owner_kind"] == "user"
    assert user_agent["manifest"]["owner_id"] == "user-1"
    assert user_agent["manifest"]["project_id"] == "default"

    system_response = client.post(
        "/api/agent-studio/agents",
        json={
            "logical_agent_id": "agent-create-system",
            "display_name": "System Agent",
            "owner_kind": "system",
        },
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert system_response.status_code == 201
    assert system_response.json()["manifest"]["owner_kind"] == "system"

    forbidden_response = client.post(
        "/api/agent-studio/agents",
        json={
            "logical_agent_id": "agent-forbidden-system",
            "display_name": "Forbidden",
            "owner_kind": "system",
        },
        headers=VIEWER_HEADERS,
    )
    assert forbidden_response.status_code == 403

    duplicate_response = client.post(
        "/api/agent-studio/agents",
        json={
            "logical_agent_id": "agent-create-user",
            "display_name": "Duplicate",
            "owner_kind": "user",
        },
        headers=USER_HEADERS,
    )
    assert duplicate_response.status_code == 409


def test_draft_routes_cover_get_update_and_missing_paths(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    create_body = _create_agent(client, logical_agent_id="agent-draft", headers=USER_HEADERS)
    draft = _get_draft(client, "agent-draft", headers=USER_HEADERS)
    assert draft["etag"] == create_body["etag"]

    draft["manifest"]["display_name"] = "Updated Draft Name"
    draft["manifest"]["project_id"] = "project-alpha"
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
    assert updated["manifest"]["project_id"] == "project-alpha"
    assert updated["manifest"]["input_schema_ref"]["ref"] == "schema://input"

    viewer_response = client.put(
        "/api/agent-studio/agents/agent-draft/draft",
        json={"manifest": draft["manifest"]},
        headers=VIEWER_HEADERS,
    )
    assert viewer_response.status_code == 403

    mismatch_manifest = dict(draft["manifest"])
    mismatch_manifest["logical_agent_id"] = "agent-other"
    mismatch_response = client.put(
        "/api/agent-studio/agents/agent-draft/draft",
        json={"manifest": mismatch_manifest},
        headers=USER_HEADERS,
    )
    assert mismatch_response.status_code == 404

    missing_draft_response = client.get(
        "/api/agent-studio/agents/agent-missing/draft",
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
        "/api/agent-studio/agents/agent-no-draft/draft",
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
        "/api/agent-studio/agents/agent-fork-source/fork",
        json={
            "source_version_id": source_version["id"],
            "new_logical_agent_id": "agent-fork-child",
        },
        headers=VIEWER_HEADERS,
    )
    assert fork_response.status_code == 201, fork_response.text
    assert fork_response.json()["based_on_version_id"] == source_version["id"]
    assert fork_response.json()["manifest"]["visibility"] == "private"

    child_version = _cut_version(client, "agent-fork-child", headers=VIEWER_HEADERS)
    assert child_version["fork_of_version_id"] == source_version["id"]

    lineage_response = client.get(
        "/api/agent-studio/agents/agent-fork-child/lineage",
        headers=VIEWER_HEADERS,
    )
    assert lineage_response.status_code == 200
    assert lineage_response.json() == [
        {
            "tenant_id": "demo",
            "child_logical_agent_id": "agent-fork-child",
            "child_version_id": child_version["id"],
            "parent_logical_agent_id": "agent-fork-source",
            "parent_version_id": source_version["id"],
            "relationship": "fork",
        }
    ]

    missing_source_response = client.post(
        "/api/agent-studio/agents/agent-fork-source/fork",
        json={
            "source_version_id": "missing-version",
            "new_logical_agent_id": "agent-fork-missing",
        },
        headers=USER_HEADERS,
    )
    assert missing_source_response.status_code == 409

    _create_agent(client, logical_agent_id="agent-fork-existing", headers=USER_HEADERS)
    duplicate_response = client.post(
        "/api/agent-studio/agents/agent-fork-source/fork",
        json={
            "source_version_id": source_version["id"],
            "new_logical_agent_id": "agent-fork-existing",
        },
        headers=USER_HEADERS,
    )
    assert duplicate_response.status_code == 409


def test_tool_registration_routes_cover_success_role_and_maturity(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tools", headers=USER_HEADERS)

    create_response = client.post(
        "/api/agent-studio/agents/agent-tools/tool-registrations",
        json={
            "descriptor_id": "foundry.web_search",
            "operation": "search",
            "kind": "managed_foundry_native",
            "handler_ref": "builtin://web-search",
        },
        headers=USER_HEADERS,
    )
    assert create_response.status_code == 201, create_response.text
    assert create_response.json()["logical_agent_id"] == "agent-tools"

    list_response = client.get(
        "/api/agent-studio/agents/agent-tools/tool-registrations",
        headers=USER_HEADERS,
    )
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    preview_response = client.post(
        "/api/agent-studio/agents/agent-tools/tool-registrations",
        json={
            "descriptor_id": "foundry.memory",
            "operation": "recall",
            "kind": "custom_handler",
            "handler_ref": "custom://memory",
        },
        headers=USER_HEADERS,
    )
    assert preview_response.status_code == 422

    viewer_response = client.post(
        "/api/agent-studio/agents/agent-tools/tool-registrations",
        json={
            "descriptor_id": "foundry.web_search",
            "operation": "search",
            "kind": "managed_foundry_native",
            "handler_ref": "builtin://web-search",
        },
        headers=VIEWER_HEADERS,
    )
    assert viewer_response.status_code == 403

    empty_agent = _create_agent(client, logical_agent_id="agent-tools-empty", headers=USER_HEADERS)
    assert empty_agent["logical_agent_id"] == "agent-tools-empty"
    empty_list_response = client.get(
        "/api/agent-studio/agents/agent-tools-empty/tool-registrations",
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
        "/api/agent-studio/agents/agent-versioned/versions",
        headers=USER_HEADERS,
    )
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [version["id"]]

    lineage_response = client.get(
        "/api/agent-studio/agents/agent-versioned/lineage",
        headers=USER_HEADERS,
    )
    assert lineage_response.status_code == 200
    assert lineage_response.json() == []

    missing_gates_response = client.post(
        "/api/agent-studio/versions/missing/gates",
        json={"evidence": {}},
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
        "/api/agent-studio/agents/agent-versioned/versions",
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
        "/api/agent-studio/agents/agent-cut-no-draft/versions",
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
        "/api/agent-studio/versions/missing/promote",
        json={"destination": "dev", "evidence_summary": "ok"},
        headers=USER_HEADERS,
    )
    assert missing_response.status_code == 404

    auto_response = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "dev", "evidence_summary": "ship it"},
        headers=USER_HEADERS,
    )
    assert auto_response.status_code == 200
    assert auto_response.json()["id"] == version["id"]
    assert (
        store.latest_release_for_version("demo", version["id"]).status
        is ReleaseStatus.ACTIVE
    )

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
    contributor_headers = _headers(tenant_id="demo", user_id="contributor-1")
    pending_response = client.post(
        f"/api/agent-studio/versions/{pending_version['id']}/promote",
        json={
            "destination": "dev",
            "evidence_summary": "needs maintainer approval",
            "risk": "medium",
        },
        headers=contributor_headers,
    )
    assert pending_response.status_code == 200
    pending_body = pending_response.json()
    assert pending_body["kind"] == "release_promotion"
    assert pending_body["state"] == "pending"
    assert pending_body["environment"] == "development"

    viewer_decision = client.post(
        f"/api/agent-studio/approvals/{pending_body['id']}/decision",
        json={"approve": True},
        headers=VIEWER_HEADERS,
    )
    assert viewer_decision.status_code == 409

    decision = client.post(
        f"/api/agent-studio/approvals/{pending_body['id']}/decision",
        json={"approve": True, "rationale": "approved"},
        headers=USER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"
    assert (
        store.latest_release_for_version("demo", pending_version["id"]).status
        is ReleaseStatus.ACTIVE
    )

    decided_again = client.post(
        f"/api/agent-studio/approvals/{pending_body['id']}/decision",
        json={"approve": False},
        headers=USER_HEADERS,
    )
    assert decided_again.status_code == 409

    ghost_response = client.post(
        "/api/agent-studio/approvals/missing-approval/decision",
        json={"approve": True},
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
        f"/api/agent-studio/versions/{ungated_version['id']}/promote",
        json={"destination": "dev", "evidence_summary": "not gated"},
        headers=USER_HEADERS,
    )
    assert ungated_response.status_code == 409

    store.create_approval(
        StudioApprovalRecord(
            id="ghost-approval",
            version_id="ghost-version",
            tenant_id="demo",
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
        "/api/agent-studio/approvals/ghost-approval/decision",
        json={"approve": True},
        headers=USER_HEADERS,
    )
    assert response.status_code == 404


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
        "/api/agent-studio/agents/agent-escalation/escalations",
        json={
            "requested_role": "maintainer",
            "evidence_summary": "need write access",
        },
        headers=VIEWER_HEADERS,
    )
    assert request_response.status_code == 201
    approval = request_response.json()
    assert approval["kind"] == "admin_escalation"
    assert approval["state"] == "pending"

    maintainer_headers = _headers(tenant_id="demo", user_id="maintainer-1")
    maintainer_decision = client.post(
        f"/api/agent-studio/approvals/{approval['id']}/decision",
        json={"approve": True},
        headers=maintainer_headers,
    )
    assert maintainer_decision.status_code == 409

    approved = client.post(
        f"/api/agent-studio/approvals/{approval['id']}/decision",
        json={"approve": True, "rationale": "approved"},
        headers=PLATFORM_OWNER_HEADERS,
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert (
        store.role_for("demo", "agent-escalation", "viewer-1")
        is AgentRole.MAINTAINER
    )

    draft = _get_draft(client, "agent-escalation", headers=USER_HEADERS)
    update_response = client.put(
        "/api/agent-studio/agents/agent-escalation/draft",
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
        "/api/agent-studio/agents/agent-deploy/deployments",
        json={"version_id": first_version["id"]},
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
        f"/api/agent-studio/deployments/{first_deployment['id']}/health",
        json={
            "status": "degraded",
            "detail": "slow response",
            "trace_ref": "trace://health",
        },
        headers=USER_HEADERS,
    )
    assert health_response.status_code == 200
    assert health_response.json()["health"]["status"] == HealthStatus.DEGRADED.value
    assert health_response.json()["trace_ref"] == "trace://health"

    missing_health = client.post(
        "/api/agent-studio/deployments/missing/health",
        json={"status": "healthy"},
        headers=USER_HEADERS,
    )
    assert missing_health.status_code == 404

    deploy_list = client.get(
        "/api/agent-studio/agents/agent-deploy/deployments",
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
    maintainer_headers = _headers(tenant_id="demo", user_id="maintainer-2")
    rollback = client.post(
        "/api/agent-studio/agents/agent-deploy/rollback",
        json={
            "deployment_id": second_deployment["id"],
            "target_version_id": first_version["id"],
        },
        headers=maintainer_headers,
    )
    assert rollback.status_code == 201
    assert rollback.json()["version_id"] == first_version["id"]
    assert rollback.json()["rollback_of_deployment_id"] == second_deployment["id"]

    bad_rollback = client.post(
        "/api/agent-studio/agents/agent-deploy/rollback",
        json={
            "deployment_id": "missing",
            "target_version_id": first_version["id"],
        },
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
        "digest": "sha256:" + "a" * 64,
    }
    draft["manifest"]["output_schema_ref"] = {
        "ref": "schema://contract-output",
        "digest": "sha256:" + "b" * 64,
    }
    draft["manifest"]["capabilities"] = [
        client.post(
            "/api/agent-studio/capabilities/attach",
            json={"descriptor_id": "foundry.web_search", "operation": "search"},
            headers=USER_HEADERS,
        ).json()
    ]
    _update_manifest(client, "agent-contract", draft["manifest"], headers=USER_HEADERS)

    contract_version = _cut_version(client, "agent-contract", headers=USER_HEADERS)
    pre_release_contract = client.get(
        f"/api/agent-studio/versions/{contract_version['id']}/contract?environment=development",
        headers=USER_HEADERS,
    )
    assert pre_release_contract.status_code == 404

    _run_gates(client, contract_version["id"], headers=USER_HEADERS)
    promoted = client.post(
        f"/api/agent-studio/versions/{contract_version['id']}/promote",
        json={"destination": "dev", "evidence_summary": "release candidate"},
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
        "/api/agent-studio/agents/agent-contract/resolve?environment=development",
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
        f"/api/agent-studio/versions/{contract_version['id']}/contract?environment=development",
        headers=USER_HEADERS,
    )
    assert exact_contract_response.status_code == 200
    assert exact_contract_response.json()["release_status"] == ReleaseStatus.ACTIVE.value

    _create_agent(client, logical_agent_id="agent-catalog-empty", headers=USER_HEADERS)
    catalog_response = client.get(
        "/api/agent-studio/catalog?environment=development",
        headers=USER_HEADERS,
    )
    assert catalog_response.status_code == 200
    assert [item["logical_agent_id"] for item in catalog_response.json()] == ["agent-contract"]

    unresolved_response = client.get(
        "/api/agent-studio/agents/agent-catalog-empty/resolve",
        headers=USER_HEADERS,
    )
    assert unresolved_response.status_code == 404

    missing_contract = client.get(
        "/api/agent-studio/versions/missing-version/contract",
        headers=USER_HEADERS,
    )
    assert missing_contract.status_code == 404

    assert store.get_binding(
        "demo",
        "agent-contract",
        DeploymentEnvironment.DEVELOPMENT,
    ) is not None


def test_memory_lifecycle_covers_remember_recall_inspect_correct_forget_export_and_audit(
    client: TestClient,
) -> None:
    _create_agent(client, logical_agent_id="agent-memory", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory", headers=USER_HEADERS)

    first_entry = client.post(
        "/api/agent-studio/agents/agent-memory/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "role": "fact",
            "content": "first memory",
            "ttl_days": 7,
            "read_acl": ["reader-1"],
            "write_acl": ["writer-1"],
        },
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
        "/api/agent-studio/agents/agent-memory/memory",
        params={"scope_kind": "conversation", "scope_id": "thread-1", "limit": 10},
        headers=USER_HEADERS,
    )
    assert creator_recall.status_code == 200
    assert [entry["id"] for entry in creator_recall.json()] == [first_body["id"]]

    denied_recall = client.get(
        "/api/agent-studio/agents/agent-memory/memory",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=VIEWER_HEADERS,
    )
    assert denied_recall.status_code == 200
    assert denied_recall.json() == []

    inspect_response = client.get(
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        headers=USER_HEADERS,
    )
    assert inspect_response.status_code == 200
    assert inspect_response.json()["content"] == "first memory"

    denied_inspect = client.get(
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        headers=VIEWER_HEADERS,
    )
    assert denied_inspect.status_code == 403

    correct_response = client.put(
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json={"content": "corrected memory"},
        headers=USER_HEADERS,
    )
    assert correct_response.status_code == 200
    assert correct_response.json()["content"] == "corrected memory"
    assert correct_response.json()["provenance"] == "operator_correction"

    denied_correct = client.put(
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json={"content": "should fail"},
        headers=VIEWER_HEADERS,
    )
    assert denied_correct.status_code == 403

    second_entry = client.post(
        "/api/agent-studio/agents/agent-memory/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "content": "second memory",
        },
        headers=USER_HEADERS,
    )
    assert second_entry.status_code == 201

    export_response = client.get(
        "/api/agent-studio/agents/agent-memory/memory-export",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=USER_HEADERS,
    )
    assert export_response.status_code == 200
    assert {entry["id"] for entry in export_response.json()} == {
        first_body["id"],
        second_entry.json()["id"],
    }

    forget_response = client.request(
        "DELETE",
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}",
        json={"reason": "superseded"},
        headers=USER_HEADERS,
    )
    assert forget_response.status_code == 200
    assert forget_response.json()["deleted_at"] is not None

    denied_forget = client.request(
        "DELETE",
        f"/api/agent-studio/agents/agent-memory/memory/{second_entry.json()['id']}",
        json={"reason": "should fail"},
        headers=VIEWER_HEADERS,
    )
    assert denied_forget.status_code == 403

    post_forget_recall = client.get(
        "/api/agent-studio/agents/agent-memory/memory",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=USER_HEADERS,
    )
    assert post_forget_recall.status_code == 200
    assert [entry["id"] for entry in post_forget_recall.json()] == [second_entry.json()["id"]]

    post_forget_export = client.get(
        "/api/agent-studio/agents/agent-memory/memory-export",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=USER_HEADERS,
    )
    assert post_forget_export.status_code == 200
    assert [entry["id"] for entry in post_forget_export.json()] == [second_entry.json()["id"]]

    audit_response = client.get(
        f"/api/agent-studio/agents/agent-memory/memory/{first_body['id']}/audit",
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
        "/api/agent-studio/agents/agent-missing/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "content": "missing agent",
        },
        headers=USER_HEADERS,
    )
    assert missing_agent_remember.status_code == 404

    _create_agent(client, logical_agent_id="agent-memory-disabled", headers=USER_HEADERS)
    disabled_remember = client.post(
        "/api/agent-studio/agents/agent-memory-disabled/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "content": "not allowed",
        },
        headers=USER_HEADERS,
    )
    assert disabled_remember.status_code == 422

    disabled_recall = client.get(
        "/api/agent-studio/agents/agent-memory-disabled/memory",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=USER_HEADERS,
    )
    assert disabled_recall.status_code == 422

    disabled_export = client.get(
        "/api/agent-studio/agents/agent-memory-disabled/memory-export",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
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
        "/api/agent-studio/agents/agent-memory-scope/memory",
        json={
            "scope_kind": "project",
            "scope_id": "project-1",
            "content": "wrong scope",
        },
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
        "/api/agent-studio/agents/agent-memory-preview/memory",
        params={"scope_kind": "conversation", "scope_id": "thread-1"},
        headers=USER_HEADERS,
    )
    assert preview_recall.status_code == 422

    _create_agent(client, logical_agent_id="agent-memory-missing-entry", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory-missing-entry", headers=USER_HEADERS)
    for method, path, payload in [
        ("GET", "/api/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry", None),
        (
            "PUT",
            "/api/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry",
            {"content": "update"},
        ),
        (
            "DELETE",
            "/api/agent-studio/agents/agent-memory-missing-entry/memory/missing-entry",
            {"reason": "forget"},
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
        "/api/agent-studio/agents/agent-memory-missing-entry/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "content": "blocked by unavailable store",
        },
        headers=USER_HEADERS,
    )
    assert memory_unavailable.status_code == 503

    audit_missing_draft = client.get(
        "/api/agent-studio/agents/missing-agent/memory/entry-1/audit",
        headers=USER_HEADERS,
    )
    assert audit_missing_draft.status_code == 404


def test_tenant_isolation_holds_for_tenant_scoped_routes(
    client: TestClient,
    store: AgentStudioStore,
) -> None:
    _create_agent(client, logical_agent_id="agent-isolation", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-isolation", headers=USER_HEADERS)
    version = _cut_gated_version(client, "agent-isolation", headers=USER_HEADERS)
    pending_headers = _headers(tenant_id="demo", user_id="contributor-2")
    _grant_role(
        store,
        logical_agent_id="agent-isolation",
        principal_id="contributor-2",
        role=AgentRole.CONTRIBUTOR,
    )
    approval = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "dev", "evidence_summary": "needs approval"},
        headers=pending_headers,
    ).json()
    deployment = _deploy_version(
        client,
        logical_agent_id="agent-isolation",
        version_id=version["id"],
        headers=USER_HEADERS,
    )
    memory_entry = client.post(
        "/api/agent-studio/agents/agent-isolation/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-iso",
            "content": "tenant scoped",
        },
        headers=USER_HEADERS,
    ).json()

    cases = [
        ("GET", "/api/agent-studio/agents/agent-isolation/draft", None, 404),
        (
            "PUT",
            "/api/agent-studio/agents/agent-isolation/draft",
            {"manifest": _minimal_manifest(logical_agent_id="agent-isolation", tenant_id="other-tenant")},
            403,
        ),
        (
            "POST",
            "/api/agent-studio/agents/agent-isolation/fork",
            {
                "source_version_id": version["id"],
                "new_logical_agent_id": "agent-isolation-fork",
            },
            409,
        ),
        (
            "POST",
            "/api/agent-studio/agents/agent-isolation/tool-registrations",
            {
                "descriptor_id": "foundry.web_search",
                "operation": "search",
                "kind": "managed_foundry_native",
                "handler_ref": "builtin://web-search",
            },
            403,
        ),
        ("GET", "/api/agent-studio/agents/agent-isolation/tool-registrations", None, 200),
        ("GET", "/api/agent-studio/agents/agent-isolation/versions", None, 200),
        ("GET", "/api/agent-studio/agents/agent-isolation/lineage", None, 200),
        ("POST", f"/api/agent-studio/versions/{version['id']}/gates", GATED_EVIDENCE, 404),
        (
            "POST",
            f"/api/agent-studio/versions/{version['id']}/promote",
            {"destination": "dev", "evidence_summary": "no access"},
            404,
        ),
        (
            "POST",
            f"/api/agent-studio/approvals/{approval['id']}/decision",
            {"approve": True},
            404,
        ),
        (
            "POST",
            "/api/agent-studio/agents/agent-isolation/deployments",
            {"version_id": version["id"]},
            409,
        ),
        ("GET", "/api/agent-studio/agents/agent-isolation/deployments", None, 200),
        (
            "POST",
            f"/api/agent-studio/deployments/{deployment['id']}/health",
            {"status": "healthy"},
            404,
        ),
        (
            "POST",
            "/api/agent-studio/agents/agent-isolation/rollback",
            {"deployment_id": deployment["id"], "target_version_id": version["id"]},
            409,
        ),
        ("GET", "/api/agent-studio/agents/agent-isolation/resolve", None, 404),
        ("GET", f"/api/agent-studio/versions/{version['id']}/contract", None, 404),
        ("GET", "/api/agent-studio/catalog", None, 200),
        (
            "POST",
            "/api/agent-studio/agents/agent-isolation/memory",
            {
                "scope_kind": "conversation",
                "scope_id": "thread-iso",
                "content": "no access",
            },
            404,
        ),
        (
            "GET",
            "/api/agent-studio/agents/agent-isolation/memory?scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
        ),
        (
            "GET",
            f"/api/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            None,
            404,
        ),
        (
            "PUT",
            f"/api/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            {"content": "blocked"},
            404,
        ),
        (
            "DELETE",
            f"/api/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}",
            {"reason": "blocked"},
            404,
        ),
        (
            "GET",
            "/api/agent-studio/agents/agent-isolation/memory-export?scope_kind=conversation&scope_id=thread-iso",
            None,
            404,
        ),
        (
            "GET",
            f"/api/agent-studio/agents/agent-isolation/memory/{memory_entry['id']}/audit",
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
        "/api/agent-studio/agents/agent-isolation/tool-registrations",
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/api/agent-studio/agents/agent-isolation/versions",
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/api/agent-studio/agents/agent-isolation/lineage",
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get(
        "/api/agent-studio/agents/agent-isolation/deployments",
        headers=OTHER_TENANT_HEADERS,
    ).json() == []
    assert client.get("/api/agent-studio/catalog", headers=OTHER_TENANT_HEADERS).json() == []


def test_unavailable_service_routes_return_503(
    unavailable_client: TestClient,
    memory_unavailable_client: TestClient,
    client: TestClient,
) -> None:
    get_draft = unavailable_client.get(
        "/api/agent-studio/agents/agent-unavailable/draft",
        headers=USER_HEADERS,
    )
    assert get_draft.status_code == 503

    create_agent = unavailable_client.post(
        "/api/agent-studio/agents",
        json={
            "logical_agent_id": "agent-unavailable",
            "display_name": "Unavailable",
        },
        headers=USER_HEADERS,
    )
    assert create_agent.status_code == 503

    record_health = unavailable_client.post(
        "/api/agent-studio/deployments/deployment-1/health",
        json={"status": "healthy"},
        headers=USER_HEADERS,
    )
    assert record_health.status_code == 503

    resolve = unavailable_client.get(
        "/api/agent-studio/agents/agent-unavailable/resolve",
        headers=USER_HEADERS,
    )
    assert resolve.status_code == 503

    _create_agent(client, logical_agent_id="agent-memory-unavailable", headers=USER_HEADERS)
    _enable_memory_scope(client, "agent-memory-unavailable", headers=USER_HEADERS)
    remember = memory_unavailable_client.post(
        "/api/agent-studio/agents/agent-memory-unavailable/memory",
        json={
            "scope_kind": "conversation",
            "scope_id": "thread-1",
            "content": "memory unavailable",
        },
        headers=USER_HEADERS,
    )
    assert remember.status_code == 503
