"""Integration tests for the Agent Studio API surface (``router.py``) using
``TestClient`` against a minimal app instance wired with in-memory service
implementations (only ever done in tests, never in production code).

Covers authz (platform-owner vs non-owner, tenant isolation), capability
maturity enforcement, gate-blocking before promotion, approval flows
(promotion + escalation), memory remember/recall, and every 503
"cloud-unavailable" branch in the accessor helpers.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry, default_registry
from research_assistant_api.agent_studio.deployment_service import DeploymentService
from research_assistant_api.agent_studio.memory_service import InMemoryMemoryStore, MemoryService
from research_assistant_api.agent_studio.model_discovery import (
    InMemoryModelDiscovery,
    ModelDiscovery,
    UnavailableModelDiscovery,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    ModelDeploymentRef,
    OwnershipGrant,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.release_service import ReleaseService
from research_assistant_api.agent_studio.router import router as agent_studio_router
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings

GATED_EVIDENCE = {
    "evidence": {"build_succeeded": True, "tests_passed": True, "smoke_passed": True},
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
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _headers(*, tenant_id: str, user_id: str, groups: tuple[str, ...] = ()) -> dict[str, str]:
    return {"x-ms-client-principal": _principal(tenant_id=tenant_id, user_id=user_id, groups=groups)}


OWNER_HEADERS = _headers(tenant_id="demo", user_id="owner-1", groups=("research-admins",))
VIEWER_HEADERS = _headers(tenant_id="demo", user_id="viewer-1", groups=())
OTHER_TENANT_HEADERS = _headers(tenant_id="other-tenant", user_id="owner-1", groups=("research-admins",))


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
    return Settings(trust_platform_identity_headers=True, allow_demo_identity=True, workspace_tenant_id="demo")


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
        (ModelDeploymentRef(deployment_name="gpt-4o-prod", model_name="gpt-4o", model_format="OpenAI"),)
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


def _create_agent(
    client: TestClient,
    *,
    logical_agent_id: str = "agent-router-test",
    owner_kind: str = "user",
    headers: dict[str, str] | None = None,
    memory_scopes: list[dict[str, object]] | None = None,
) -> None:
    response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": logical_agent_id, "display_name": "Router Test Agent", "owner_kind": owner_kind},
        headers=headers or OWNER_HEADERS,
    )
    assert response.status_code == 201, response.text
    if memory_scopes is not None:
        draft = response.json()
        draft["manifest"]["memory_policy"] = {"enabled": True, "scopes": memory_scopes}
        update = client.put(
            f"/api/agent-studio/agents/{logical_agent_id}/draft",
            json={"manifest": draft["manifest"]},
            headers=headers or OWNER_HEADERS,
        )
        assert update.status_code == 200, update.text


def _cut_gated_version(
    client: TestClient, logical_agent_id: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    version = client.post(
        f"/api/agent-studio/agents/{logical_agent_id}/versions", headers=headers or OWNER_HEADERS
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]
    gated = client.post(
        f"/api/agent-studio/versions/{version_id}/gates", json=GATED_EVIDENCE, headers=headers or OWNER_HEADERS
    )
    assert gated.status_code == 200, gated.text
    assert all(result["status"] == "passed" for result in gated.json()["results"]), gated.text
    return cast("dict[str, Any]", version.json())


# -- Capabilities -----------------------------------------------------------


def test_list_capabilities_returns_full_catalog_with_honest_maturity(client: TestClient) -> None:
    response = client.get("/api/agent-studio/capabilities", headers=OWNER_HEADERS)
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()}
    assert "foundry.web_search" in ids
    assert "foundry.memory" in ids  # preview capability still visible


def test_attach_capability_succeeds_for_ga_operation(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/capabilities/attach",
        json={"descriptor_id": "foundry.web_search", "operation": "search"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["descriptor_id"] == "foundry.web_search"


def test_attach_capability_rejects_preview_operation(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/capabilities/attach",
        json={"descriptor_id": "foundry.memory", "operation": "recall"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422
    assert "preview" in response.json()["detail"]


def test_attach_capability_rejects_unknown_descriptor(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/capabilities/attach",
        json={"descriptor_id": "unknown.capability", "operation": "run"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422


# -- Manifest schema endpoint ---------------------------------------------


def test_get_agent_manifest_schema_returns_schema_and_digest(client: TestClient) -> None:
    response = client.get("/api/agent-studio/schemas/agent-manifest", headers=OWNER_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "agent-studio.manifest.v1"
    assert body["digest"].startswith("sha256:")
    assert body["json_schema"]["title"] == "AgentManifest"


# -- Tool registrations -----------------------------------------------------


def test_register_tool_succeeds_for_ga_operation(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tool-reg")
    response = client.post(
        "/api/agent-studio/agents/agent-tool-reg/tool-registrations",
        json={
            "descriptor_id": "foundry.web_search",
            "operation": "search",
            "kind": "managed_foundry_native",
            "handler_ref": "builtin://web_search",
        },
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["descriptor_id"] == "foundry.web_search"
    assert body["operation"] == "search"
    assert body["kind"] == "managed_foundry_native"
    assert body["handler_ref"] == "builtin://web_search"
    assert body["logical_agent_id"] == "agent-tool-reg"


def test_register_tool_rejects_non_ga_operation(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tool-reg-nonga")
    response = client.post(
        "/api/agent-studio/agents/agent-tool-reg-nonga/tool-registrations",
        json={
            "descriptor_id": "foundry.memory",
            "operation": "recall",
            "kind": "custom_handler",
            "handler_ref": "custom://memory",
        },
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422


def test_register_tool_rejects_viewer_role(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tool-reg-viewer")
    response = client.post(
        "/api/agent-studio/agents/agent-tool-reg-viewer/tool-registrations",
        json={
            "descriptor_id": "foundry.web_search",
            "operation": "search",
            "kind": "managed_foundry_native",
            "handler_ref": "builtin://web_search",
        },
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 403


def test_list_tool_registrations_returns_registered_tools(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tool-reg-list")
    client.post(
        "/api/agent-studio/agents/agent-tool-reg-list/tool-registrations",
        json={
            "descriptor_id": "foundry.web_search",
            "operation": "search",
            "kind": "managed_foundry_native",
            "handler_ref": "builtin://web_search",
        },
        headers=OWNER_HEADERS,
    )
    response = client.get(
        "/api/agent-studio/agents/agent-tool-reg-list/tool-registrations", headers=OWNER_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["descriptor_id"] == "foundry.web_search"


def test_list_tool_registrations_returns_empty_for_agent_with_none_registered(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tool-reg-empty")
    response = client.get(
        "/api/agent-studio/agents/agent-tool-reg-empty/tool-registrations", headers=OWNER_HEADERS
    )
    assert response.status_code == 200
    assert response.json() == []


# -- Models -------------------------------------------------------------


def test_list_deployed_models_returns_configured_models(client: TestClient) -> None:
    response = client.get("/api/agent-studio/models", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.json()[0]["deployment_name"] == "gpt-4o-prod"


def test_list_deployed_models_returns_503_when_discovery_unavailable(unavailable_client: TestClient) -> None:
    response = unavailable_client.get("/api/agent-studio/models", headers=OWNER_HEADERS)
    assert response.status_code == 503


# -- Agent creation / drafts ---------------------------------------------


def test_create_agent_succeeds_for_user_owned_agent(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "agent-create-test", "display_name": "Create Test", "owner_kind": "user"},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["manifest"]["owner_kind"] == "user"


def test_create_agent_succeeds_for_system_agent_as_platform_owner(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "agent-system-test", "display_name": "System Test", "owner_kind": "system"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201


def test_create_agent_rejects_system_agent_from_non_platform_owner(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "agent-system-test", "display_name": "System Test", "owner_kind": "system"},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 403


def test_create_agent_rejects_duplicate(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-dup-test")
    response = client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "agent-dup-test", "display_name": "Dup", "owner_kind": "user"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409


def test_get_draft_returns_404_for_missing_agent(client: TestClient) -> None:
    response = client.get("/api/agent-studio/agents/agent-missing-test/draft", headers=OWNER_HEADERS)
    assert response.status_code == 404


def test_get_draft_returns_created_draft(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-draft-get")
    response = client.get("/api/agent-studio/agents/agent-draft-get/draft", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.json()["logical_agent_id"] == "agent-draft-get"


def test_update_draft_succeeds_for_owner(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-draft-update")
    draft = client.get("/api/agent-studio/agents/agent-draft-update/draft", headers=OWNER_HEADERS).json()
    draft["manifest"]["display_name"] = "Updated Name"
    response = client.put(
        "/api/agent-studio/agents/agent-draft-update/draft",
        json={"manifest": draft["manifest"]},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["manifest"]["display_name"] == "Updated Name"


def test_update_draft_rejects_viewer_role(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-draft-viewer")
    draft = client.get("/api/agent-studio/agents/agent-draft-viewer/draft", headers=OWNER_HEADERS).json()
    response = client.put(
        "/api/agent-studio/agents/agent-draft-viewer/draft",
        json={"manifest": draft["manifest"]},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 403


def test_update_draft_rejects_mismatched_manifest_ids(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-draft-mismatch")
    draft = client.get("/api/agent-studio/agents/agent-draft-mismatch/draft", headers=OWNER_HEADERS).json()
    draft["manifest"]["logical_agent_id"] = "agent-different-id"
    response = client.put(
        "/api/agent-studio/agents/agent-draft-mismatch/draft",
        json={"manifest": draft["manifest"]},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_update_draft_returns_404_when_no_draft_exists(client: TestClient, store: AgentStudioStore) -> None:
    # Grant ownership without ever creating a draft (defensive branch: a
    # role check can pass while the underlying draft record is missing).
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-no-draft", principal_id="owner-1", role=AgentRole.OWNER,
            granted_by="admin",
        )
    )
    manifest = AgentManifest(
        logical_agent_id="agent-no-draft", tenant_id="demo", display_name="No Draft",
        owner_kind=AgentOwnerKind.USER, owner_id="owner-1",
    )
    response = client.put(
        "/api/agent-studio/agents/agent-no-draft/draft",
        json={"manifest": manifest.model_dump(mode="json")},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


# -- Fork -----------------------------------------------------------------


def test_fork_agent_succeeds_from_released_version(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-fork-source")
    version = _cut_gated_version(client, "agent-fork-source")
    response = client.post(
        "/api/agent-studio/agents/agent-fork-source/fork",
        json={"source_version_id": version["id"], "new_logical_agent_id": "agent-fork-result"},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["logical_agent_id"] == "agent-fork-result"
    assert response.json()["based_on_version_id"] == version["id"]


def test_fork_agent_rejects_unknown_source_version(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/agents/agent-fork-source/fork",
        json={"source_version_id": "missing-version", "new_logical_agent_id": "agent-fork-result-2"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409


def test_fork_agent_rejects_duplicate_new_agent_id(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-fork-source-2")
    version = _cut_gated_version(client, "agent-fork-source-2")
    _create_agent(client, logical_agent_id="agent-fork-existing")
    response = client.post(
        "/api/agent-studio/agents/agent-fork-source-2/fork",
        json={"source_version_id": version["id"], "new_logical_agent_id": "agent-fork-existing"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409


# -- Versions / lineage ----------------------------------------------------


def test_cut_version_succeeds_for_owner(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-cut-version")
    response = client.post("/api/agent-studio/agents/agent-cut-version/versions", headers=OWNER_HEADERS)
    assert response.status_code == 201
    assert response.json()["sequence"] == 1


def test_cut_version_rejects_viewer_role(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-cut-viewer")
    response = client.post("/api/agent-studio/agents/agent-cut-viewer/versions", headers=VIEWER_HEADERS)
    assert response.status_code == 403


def test_cut_version_returns_404_when_no_draft(client: TestClient, store: AgentStudioStore) -> None:
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-cut-no-draft", principal_id="owner-1", role=AgentRole.OWNER,
            granted_by="admin",
        )
    )
    response = client.post("/api/agent-studio/agents/agent-cut-no-draft/versions", headers=OWNER_HEADERS)
    assert response.status_code == 404


def test_list_versions_returns_versions(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-list-versions")
    client.post("/api/agent-studio/agents/agent-list-versions/versions", headers=OWNER_HEADERS)
    response = client.get("/api/agent-studio/agents/agent-list-versions/versions", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_list_lineage_is_empty_for_non_forked_agent(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-lineage-empty")
    response = client.get("/api/agent-studio/agents/agent-lineage-empty/lineage", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.json() == []


def test_list_lineage_records_fork_edge_on_first_cut(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-lineage-source")
    version = _cut_gated_version(client, "agent-lineage-source")
    client.post(
        "/api/agent-studio/agents/agent-lineage-source/fork",
        json={"source_version_id": version["id"], "new_logical_agent_id": "agent-lineage-fork"},
        headers=OWNER_HEADERS,
    )
    client.post("/api/agent-studio/agents/agent-lineage-fork/versions", headers=OWNER_HEADERS)
    response = client.get("/api/agent-studio/agents/agent-lineage-fork/lineage", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["parent_version_id"] == version["id"]


# -- Gates ------------------------------------------------------------------


def test_run_gates_returns_404_for_unknown_version(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/versions/missing-version/gates", json={"evidence": {}}, headers=OWNER_HEADERS
    )
    assert response.status_code == 404


def test_run_gates_fails_closed_without_evidence(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-gates-fail")
    version = client.post(
        "/api/agent-studio/agents/agent-gates-fail/versions", headers=OWNER_HEADERS
    ).json()
    response = client.post(
        f"/api/agent-studio/versions/{version['id']}/gates", json={"evidence": {}}, headers=OWNER_HEADERS
    )
    assert response.status_code == 200
    assert any(result["status"] != "passed" for result in response.json()["results"])


# -- Promotion / approvals ---------------------------------------------------


def test_request_promotion_returns_404_for_unknown_version(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/versions/missing-version/promote",
        json={"destination": "prod", "evidence_summary": "Looks good."},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_request_promotion_rejects_ungated_version(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-promote-ungated")
    version = client.post(
        "/api/agent-studio/agents/agent-promote-ungated/versions", headers=OWNER_HEADERS
    ).json()
    response = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "prod", "evidence_summary": "Looks good."},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409


def test_request_promotion_auto_promotes_for_owner(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-promote-auto")
    version = _cut_gated_version(client, "agent-promote-auto")
    response = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "prod", "evidence_summary": "Looks good."},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "released"


def test_request_promotion_requires_approval_for_contributor(
    client: TestClient, store: AgentStudioStore
) -> None:
    _create_agent(client, logical_agent_id="agent-promote-approval")
    version = _cut_gated_version(client, "agent-promote-approval")
    # Contributors are below MAINTAINER, so promotion must route to approval
    # rather than auto-promoting.
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo",
            logical_agent_id="agent-promote-approval",
            principal_id="contributor-1",
            role=AgentRole.CONTRIBUTOR,
            granted_by="owner-1",
        )
    )
    contributor_headers = _headers(tenant_id="demo", user_id="contributor-1")
    response = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "prod", "evidence_summary": "Looks good."},
        headers=contributor_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "pending"
    assert body["kind"] == "release_promotion"


def test_decide_approval_returns_404_for_missing_approval(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/approvals/missing-approval/decision",
        json={"approve": True},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_decide_promotion_approves_and_releases_version(client: TestClient, store: AgentStudioStore) -> None:
    _create_agent(client, logical_agent_id="agent-decide-promote")
    version = _cut_gated_version(client, "agent-decide-promote")
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-decide-promote", principal_id="contributor-2",
            role=AgentRole.CONTRIBUTOR, granted_by="owner-1",
        )
    )
    contributor_headers = _headers(tenant_id="demo", user_id="contributor-2")
    approval = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "prod", "evidence_summary": "Evidence."},
        headers=contributor_headers,
    ).json()
    decision = client.post(
        f"/api/agent-studio/approvals/{approval['id']}/decision",
        json={"approve": True, "rationale": "Ship it."},
        headers=OWNER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"
    version_after = client.get(
        "/api/agent-studio/agents/agent-decide-promote/versions", headers=OWNER_HEADERS
    ).json()[0]
    assert version_after["status"] == "released"


def test_decide_promotion_rejects_when_already_decided(client: TestClient, store: AgentStudioStore) -> None:
    _create_agent(client, logical_agent_id="agent-decide-twice")
    version = _cut_gated_version(client, "agent-decide-twice")
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-decide-twice", principal_id="contributor-3",
            role=AgentRole.CONTRIBUTOR, granted_by="owner-1",
        )
    )
    contributor_headers = _headers(tenant_id="demo", user_id="contributor-3")
    approval = client.post(
        f"/api/agent-studio/versions/{version['id']}/promote",
        json={"destination": "prod", "evidence_summary": "Evidence."},
        headers=contributor_headers,
    ).json()
    first = client.post(
        f"/api/agent-studio/approvals/{approval['id']}/decision",
        json={"approve": False},
        headers=OWNER_HEADERS,
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/agent-studio/approvals/{approval['id']}/decision",
        json={"approve": True},
        headers=OWNER_HEADERS,
    )
    assert second.status_code == 409


def test_decide_approval_returns_404_when_promotion_version_missing(
    client: TestClient, store: AgentStudioStore
) -> None:
    # Defensive branch: an approval record references a version_id that no
    # longer resolves in the store.
    store.create_approval(
        StudioApprovalRecord(
            id="ghost-approval",
            version_id="ghost-version",
            tenant_id="demo",
            kind="release_promotion",  # type: ignore[arg-type]
            gated_action="promote_version",
            destination="prod",
            requested_by="owner-1",
            evidence_summary="Evidence.",
            risk="medium",
            idempotency_key="ghost-key",
        )
    )
    response = client.post(
        "/api/agent-studio/approvals/ghost-approval/decision",
        json={"approve": True},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


# -- Escalations --------------------------------------------------------


def test_request_escalation_creates_pending_approval(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-escalation")
    response = client.post(
        "/api/agent-studio/agents/agent-escalation/escalations",
        json={"requested_role": "maintainer", "evidence_summary": "Need to maintain this agent."},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["kind"] == "admin_escalation"
    assert response.json()["state"] == "pending"


def test_decide_escalation_grants_role_on_approval(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-escalation-grant")
    escalation = client.post(
        "/api/agent-studio/agents/agent-escalation-grant/escalations",
        json={"requested_role": "maintainer", "evidence_summary": "Need to maintain this agent."},
        headers=VIEWER_HEADERS,
    ).json()
    decision = client.post(
        f"/api/agent-studio/approvals/{escalation['id']}/decision",
        json={"approve": True},
        headers=OWNER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"
    # The escalated principal can now update the draft (MAINTAINER >= CONTRIBUTOR).
    viewer_now_maintainer_headers = _headers(tenant_id="demo", user_id="viewer-1")
    draft = client.get(
        "/api/agent-studio/agents/agent-escalation-grant/draft", headers=OWNER_HEADERS
    ).json()
    update = client.put(
        "/api/agent-studio/agents/agent-escalation-grant/draft",
        json={"manifest": draft["manifest"]},
        headers=viewer_now_maintainer_headers,
    )
    assert update.status_code == 200


def test_decide_escalation_rejects_when_declined(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-escalation-reject")
    escalation = client.post(
        "/api/agent-studio/agents/agent-escalation-reject/escalations",
        json={"requested_role": "maintainer", "evidence_summary": "Need to maintain this agent."},
        headers=VIEWER_HEADERS,
    ).json()
    decision = client.post(
        f"/api/agent-studio/approvals/{escalation['id']}/decision",
        json={"approve": False, "rationale": "Not yet."},
        headers=OWNER_HEADERS,
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "rejected"


# -- Deployments --------------------------------------------------------


def test_deploy_succeeds_for_gated_version(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-deploy-route")
    version = _cut_gated_version(client, "agent-deploy-route")
    response = client.post(
        "/api/agent-studio/agents/agent-deploy-route/deployments",
        json={"version_id": version["id"]},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["version_id"] == version["id"]


def test_deploy_rejects_viewer_role(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-deploy-viewer")
    version = _cut_gated_version(client, "agent-deploy-viewer")
    response = client.post(
        "/api/agent-studio/agents/agent-deploy-viewer/deployments",
        json={"version_id": version["id"]},
        headers=VIEWER_HEADERS,
    )
    assert response.status_code == 409


def test_list_deployments_returns_created_deployments(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-deploy-list")
    version = _cut_gated_version(client, "agent-deploy-list")
    client.post(
        "/api/agent-studio/agents/agent-deploy-list/deployments",
        json={"version_id": version["id"]},
        headers=OWNER_HEADERS,
    )
    response = client.get("/api/agent-studio/agents/agent-deploy-list/deployments", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_record_health_updates_deployment(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-health")
    version = _cut_gated_version(client, "agent-health")
    deployment = client.post(
        "/api/agent-studio/agents/agent-health/deployments",
        json={"version_id": version["id"]},
        headers=OWNER_HEADERS,
    ).json()
    response = client.post(
        f"/api/agent-studio/deployments/{deployment['id']}/health",
        json={"status": "degraded", "detail": "slow"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["health"]["status"] == "degraded"


def test_record_health_returns_404_for_missing_deployment(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/deployments/missing-deployment/health",
        json={"status": "healthy"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_rollback_succeeds_for_maintainer(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-rollback")
    first_version = _cut_gated_version(client, "agent-rollback")
    first_deploy = client.post(
        "/api/agent-studio/agents/agent-rollback/deployments",
        json={"version_id": first_version["id"]},
        headers=OWNER_HEADERS,
    ).json()
    draft = client.get("/api/agent-studio/agents/agent-rollback/draft", headers=OWNER_HEADERS).json()
    client.put(
        "/api/agent-studio/agents/agent-rollback/draft",
        json={"manifest": draft["manifest"]},
        headers=OWNER_HEADERS,
    )
    second_version = _cut_gated_version(client, "agent-rollback")
    second_deploy = client.post(
        "/api/agent-studio/agents/agent-rollback/deployments",
        json={"version_id": second_version["id"]},
        headers=OWNER_HEADERS,
    ).json()
    assert first_deploy["id"] != second_deploy["id"]
    response = client.post(
        "/api/agent-studio/agents/agent-rollback/rollback",
        json={"deployment_id": second_deploy["id"], "target_version_id": first_version["id"]},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["version_id"] == first_version["id"]


def test_rollback_rejects_when_deployment_missing(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-rollback-missing")
    response = client.post(
        "/api/agent-studio/agents/agent-rollback-missing/rollback",
        json={"deployment_id": "missing-deployment", "target_version_id": "missing-version"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 409


def test_resolve_returns_404_when_no_binding(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-resolve-empty")
    response = client.get("/api/agent-studio/agents/agent-resolve-empty/resolve", headers=OWNER_HEADERS)
    assert response.status_code == 404


def test_resolve_returns_bound_version_after_deploy(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-resolve-bound")
    version = _cut_gated_version(client, "agent-resolve-bound")
    client.post(
        "/api/agent-studio/agents/agent-resolve-bound/deployments",
        json={"version_id": version["id"]},
        headers=OWNER_HEADERS,
    )
    response = client.get("/api/agent-studio/agents/agent-resolve-bound/resolve", headers=OWNER_HEADERS)
    assert response.status_code == 200
    assert response.json()["id"] == version["id"]


# -- Memory ---------------------------------------------------------------


def test_remember_returns_404_for_missing_agent(client: TestClient) -> None:
    response = client.post(
        "/api/agent-studio/agents/agent-memory-missing/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_remember_succeeds_for_declared_scope(client: TestClient) -> None:
    _create_agent(
        client,
        logical_agent_id="agent-memory-remember",
        memory_scopes=[{"kind": "conversation", "mechanism": "application_memory_store"}],
    )
    response = client.post(
        "/api/agent-studio/agents/agent-memory-remember/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello there"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 201
    assert response.json()["content"] == "hello there"


def test_remember_rejects_undeclared_scope(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-memory-undeclared")
    response = client.post(
        "/api/agent-studio/agents/agent-memory-undeclared/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422


def test_remember_rejects_non_ga_memory_mechanism(client: TestClient) -> None:
    _create_agent(
        client,
        logical_agent_id="agent-memory-nonga",
        memory_scopes=[{"kind": "conversation", "mechanism": "foundry_native_memory_store"}],
    )
    response = client.post(
        "/api/agent-studio/agents/agent-memory-nonga/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422
    assert "not GA" in response.json()["detail"]


def test_recall_returns_404_for_missing_agent(client: TestClient) -> None:
    response = client.get(
        "/api/agent-studio/agents/agent-recall-missing/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 404


def test_recall_returns_remembered_entries(client: TestClient) -> None:
    _create_agent(
        client,
        logical_agent_id="agent-memory-recall",
        memory_scopes=[{"kind": "conversation", "mechanism": "application_memory_store"}],
    )
    client.post(
        "/api/agent-studio/agents/agent-memory-recall/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello there"},
        headers=OWNER_HEADERS,
    )
    response = client.get(
        "/api/agent-studio/agents/agent-memory-recall/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["content"] == "hello there"


def test_recall_returns_422_when_manifest_declares_non_ga_mechanism(client: TestClient) -> None:
    _create_agent(
        client,
        logical_agent_id="agent-memory-recall-nonga",
        memory_scopes=[{"kind": "conversation", "mechanism": "foundry_native_memory_store"}],
    )
    response = client.get(
        "/api/agent-studio/agents/agent-memory-recall-nonga/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422
    assert "not GA" in response.json()["detail"]


def test_recall_returns_empty_list_for_undeclared_scope(client: TestClient) -> None:
    # Unlike ``remember``, ``recall`` only enforces GA-mechanism validation
    # across whatever scopes *are* declared; an undeclared scope simply has
    # no entries rather than being rejected outright. Memory must still be
    # explicitly enabled via MemoryPolicy (persistent memory is off by
    # default) even though the specific scope being recalled isn't declared.
    _create_agent(
        client,
        logical_agent_id="agent-memory-recall-undeclared",
        memory_scopes=[{"kind": "user", "mechanism": "application_memory_store"}],
    )
    response = client.get(
        "/api/agent-studio/agents/agent-memory-recall-undeclared/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == []


def test_recall_returns_422_when_memory_policy_disabled(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-memory-recall-policy-off")
    response = client.get(
        "/api/agent-studio/agents/agent-memory-recall-policy-off/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 422
    assert "disabled" in response.json()["detail"]


def test_remember_returns_503_when_memory_service_unavailable(
    memory_unavailable_client: TestClient,
) -> None:
    _create_agent(memory_unavailable_client, logical_agent_id="agent-memory-unavailable")
    response = memory_unavailable_client.post(
        "/api/agent-studio/agents/agent-memory-unavailable/memory",
        json={"scope_kind": "conversation", "scope_id": "conv-1", "content": "hello"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 503


def test_recall_returns_503_when_memory_service_unavailable(
    memory_unavailable_client: TestClient,
) -> None:
    _create_agent(memory_unavailable_client, logical_agent_id="agent-memory-unavailable-2")
    response = memory_unavailable_client.get(
        "/api/agent-studio/agents/agent-memory-unavailable-2/memory",
        params={"scope_kind": "conversation", "scope_id": "conv-1"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 503


# -- Tenant isolation --------------------------------------------------------


def test_draft_is_isolated_across_tenants(client: TestClient) -> None:
    _create_agent(client, logical_agent_id="agent-tenant-isolated")
    response = client.get(
        "/api/agent-studio/agents/agent-tenant-isolated/draft", headers=OTHER_TENANT_HEADERS
    )
    assert response.status_code == 404


# -- 503 unavailable paths for store / release_service / deployment_service --


def test_get_draft_returns_503_when_store_unavailable(unavailable_client: TestClient) -> None:
    response = unavailable_client.get("/api/agent-studio/agents/agent-x/draft", headers=OWNER_HEADERS)
    assert response.status_code == 503


def test_create_agent_returns_503_when_release_service_unavailable(unavailable_client: TestClient) -> None:
    response = unavailable_client.post(
        "/api/agent-studio/agents",
        json={"logical_agent_id": "agent-unavailable-x", "display_name": "X", "owner_kind": "user"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 503


def test_record_health_returns_503_when_deployment_service_unavailable(unavailable_client: TestClient) -> None:
    response = unavailable_client.post(
        "/api/agent-studio/deployments/deployment-x/health",
        json={"status": "healthy"},
        headers=OWNER_HEADERS,
    )
    assert response.status_code == 503


def test_resolve_returns_503_when_deployment_service_unavailable(unavailable_client: TestClient) -> None:
    response = unavailable_client.get("/api/agent-studio/agents/agent-x/resolve", headers=OWNER_HEADERS)
    assert response.status_code == 503
