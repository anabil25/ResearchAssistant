from __future__ import annotations

import base64
import importlib
import json
from typing import Any
from uuid import uuid4

import pytest
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_assistant_api.agent_chat import ThreadHandle
from research_assistant_api.agent_chat import router as agent_chat_router
from research_assistant_api.app import app
from research_assistant_api.config import Settings
from research_assistant_api.connector_gateway import ConnectorGatewayError
from research_assistant_api.cosmos_workspace import (
    CosmosWorkspaceStore,
    WorkspaceProjectUnavailableError,
)
from research_assistant_api.foundry import (
    HostedAgentConfigurationError,
    HostedAgentInvocationError,
    HostedAgentNotReadyError,
    HostedAgentReply,
)
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ChatMessage,
    ChatThread,
    ChatThreadConflictError,
    PersonalProject,
    PersonalProjectCreate,
    PersonalProjectUpdate,
    ProjectLifecycle,
    WorkspaceStore,
    utc_now,
)
from research_assistant_core.models import Capability

app_module = importlib.import_module("research_assistant_api.app")
TEST_PROJECT_ID = f"project-{'0' * 32}"


class _TestWorkspaceProjectProvider:
    def __init__(self) -> None:
        self._projects: dict[str, PersonalProject] = {}
        self._stores: dict[str, WorkspaceStore] = {}
        self._preferences: dict[str, str] = {}
        now = utc_now()
        project = PersonalProject(
            project_id=TEST_PROJECT_ID,
            owner_user_id="demo-researcher",
            name="Personal research workspace",
            description="A private workspace for governed research.",
            created_at=now,
            updated_at=now,
            template_project_id=TEST_PROJECT_ID,
        )
        self._projects[project.project_id] = project
        self._stores[project.project_id] = self._new_store(project)
        self._preferences[project.owner_user_id] = project.project_id

    @staticmethod
    def _new_store(project: PersonalProject) -> WorkspaceStore:
        return WorkspaceStore(
            tenant_id="demo",
            project_id=project.project_id,
            project_name=project.name,
            project_description=project.description,
        )

    @staticmethod
    def _require_tenant(identity: IdentityContext) -> None:
        if identity.tenant_id != "demo":
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")

    def _owned(self, identity: IdentityContext, project_id: str) -> PersonalProject:
        self._require_tenant(identity)
        project = self._projects.get(project_id)
        if (
            project is None
            or project.owner_user_id != identity.user_id
            or project.lifecycle is not ProjectLifecycle.ACTIVE
        ):
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")
        return project

    def list_projects(self, identity: IdentityContext) -> tuple[PersonalProject, ...]:
        self._require_tenant(identity)
        return tuple(
            project
            for project in self._projects.values()
            if project.owner_user_id == identity.user_id
            and project.lifecycle is ProjectLifecycle.ACTIVE
        )

    def create_project(
        self,
        identity: IdentityContext,
        payload: PersonalProjectCreate,
    ) -> PersonalProject:
        self._require_tenant(identity)
        now = utc_now()
        project = PersonalProject(
            project_id=f"project-{uuid4().hex}",
            owner_user_id=identity.user_id,
            name=payload.name,
            description=payload.description,
            created_at=now,
            updated_at=now,
            template_project_id=TEST_PROJECT_ID,
        )
        self._projects[project.project_id] = project
        self._stores[project.project_id] = self._new_store(project)
        self._preferences[identity.user_id] = project.project_id
        return project

    def update_project(
        self,
        identity: IdentityContext,
        project_id: str,
        payload: PersonalProjectUpdate,
    ) -> PersonalProject:
        project = self._owned(identity, project_id)
        updated = project.model_copy(
            update={
                "name": payload.name if payload.name is not None else project.name,
                "description": (
                    payload.description
                    if payload.description is not None
                    else project.description
                ),
                "lifecycle": (
                    ProjectLifecycle.ARCHIVED if payload.archive else project.lifecycle
                ),
                "updated_at": utc_now(),
            }
        )
        self._projects[project_id] = updated
        if payload.name is not None or payload.description is not None:
            store = self._stores[project_id]
            store.update_settings(
                store.settings().model_copy(
                    update={"name": updated.name, "description": updated.description}
                )
            )
        if payload.archive and self._preferences.get(identity.user_id) == project_id:
            self._preferences.pop(identity.user_id, None)
        return updated

    def select_project(self, identity: IdentityContext, project_id: str) -> PersonalProject:
        project = self._owned(identity, project_id)
        self._preferences[identity.user_id] = project_id
        return project

    def active_project_id(self, identity: IdentityContext) -> str | None:
        self._require_tenant(identity)
        project_id = self._preferences.get(identity.user_id)
        if project_id is None:
            return None
        try:
            self._owned(identity, project_id)
        except WorkspaceProjectUnavailableError:
            return None
        return project_id

    def workspace_for(
        self,
        identity: IdentityContext,
        project_id: str | None,
    ) -> WorkspaceStore:
        selected = project_id or self.active_project_id(identity)
        if selected is None:
            raise WorkspaceProjectUnavailableError("The requested project is unavailable.")
        return self._stores[self._owned(identity, selected).project_id]

    def stores_for_reconciliation(self) -> tuple[WorkspaceStore, ...]:
        return tuple(
            self._stores[project.project_id]
            for project in self._projects.values()
            if project.lifecycle is ProjectLifecycle.ACTIVE
        )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "foundry_project_endpoint": "https://foundry.example.test/api/projects/test",
        "cosmos_endpoint": "https://cosmos.example.test",
        "storage_blob_endpoint": "https://storage.example.test",
        "search_endpoint": "https://search.example.test",
        "workspace_tenant_id": "demo",
        "workspace_project_id": TEST_PROJECT_ID,
    }
    values.update(overrides)
    return Settings.model_validate(values)


class _SharedThreadContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.etag = 0

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        document = self.documents.get(item)
        if document is None or document["tenantRunKey"] != partition_key:
            raise CosmosResourceNotFoundError(  # type: ignore[no-untyped-call]
                status_code=404,
                message="not found",
            )
        return dict(document)

    def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["id"] in self.documents:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409,
                message="conflict",
            )
        self.etag += 1
        self.documents[body["id"]] = {**body, "_etag": str(self.etag)}
        return dict(self.documents[body["id"]])

    def replace_item(
        self,
        *,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: object,
    ) -> dict[str, Any]:
        del match_condition
        current = self.documents.get(item)
        if current is None:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=404,
                message="not found",
            )
        if current["_etag"] != etag:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=412,
                message="stale",
            )
        self.etag += 1
        self.documents[item] = {**body, "_etag": str(self.etag)}
        return dict(self.documents[item])


def _cosmos_thread_store(container: _SharedThreadContainer) -> CosmosWorkspaceStore:
    store = CosmosWorkspaceStore.__new__(CosmosWorkspaceStore)
    WorkspaceStore.__init__(store, tenant_id="demo", project_id=TEST_PROJECT_ID)
    store._runs_container = container  # type: ignore[assignment]
    return store


def test_cosmos_chat_threads_refresh_across_replicas_and_reject_stale_writes() -> None:
    container = _SharedThreadContainer()
    first = _cosmos_thread_store(container)
    second = _cosmos_thread_store(container)
    stale_writer = _cosmos_thread_store(container)
    now = utc_now()
    created = first.save_chat_thread(
        ChatThread(
            id="thread-shared",
            project_id=TEST_PROJECT_ID,
            tenant_id="demo",
            capability=Capability.GRANT,
            agent_name="grant-agent",
            owner_principal_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            delegated_user_identity="opaque-user",
            created_at=now,
            updated_at=now,
        )
    )
    stale = stale_writer.chat_thread(created.id, owner_principal_id="user-1")
    assert stale is not None
    assistant = ChatMessage(
        id="reply-1",
        role="assistant",
        content="Verified response",
        created_at=utc_now(),
    )
    first.save_chat_thread(created.model_copy(update={"messages": [assistant]}))

    refreshed = second.chat_thread(created.id, owner_principal_id="user-1")
    assert refreshed is not None
    assert [message.id for message in refreshed.messages] == ["reply-1"]
    with pytest.raises(ChatThreadConflictError, match="changed"):
        second.save_chat_thread(stale.model_copy(update={"attachments": []}))


def test_cosmos_chat_turn_lease_excludes_a_second_replica() -> None:
    container = _SharedThreadContainer()
    first = _cosmos_thread_store(container)
    second = _cosmos_thread_store(container)
    now = utc_now()
    created = first.save_chat_thread(
        ChatThread(
            id="thread-leased",
            project_id=TEST_PROJECT_ID,
            tenant_id="demo",
            capability=Capability.GRANT,
            agent_name="grant-agent",
            owner_principal_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            delegated_user_identity="opaque-user",
            created_at=now,
            updated_at=now,
        )
    )

    claimed = first.claim_chat_turn(created, "client-message-0001")
    observed = second.chat_thread(created.id, owner_principal_id="user-1")
    assert observed is not None
    assert observed.active_turn_id == "client-message-0001"
    with pytest.raises(ChatThreadConflictError, match="already in progress"):
        second.claim_chat_turn(observed, "client-message-0002")

    first.release_chat_turn(claimed, "client-message-0001")
    released = second.chat_thread(created.id, owner_principal_id="user-1")
    assert released is not None
    assert released.active_turn_id is None
    assert released.active_turn_lease_id is None
    assert released.active_turn_expires_at is None


def test_expired_request_cannot_release_same_id_successor_lease() -> None:
    container = _SharedThreadContainer()
    first = _cosmos_thread_store(container)
    second = _cosmos_thread_store(container)
    now = utc_now()
    created = first.save_chat_thread(
        ChatThread(
            id="thread-reclaimed",
            project_id=TEST_PROJECT_ID,
            tenant_id="demo",
            capability=Capability.GRANT,
            agent_name="grant-agent",
            owner_principal_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            delegated_user_identity="opaque-user",
            created_at=now,
            updated_at=now,
        )
    )

    expired = first.claim_chat_turn(
        created,
        "client-message-reused",
        lease_minutes=0,
    )
    successor = second.claim_chat_turn(
        expired,
        "client-message-reused",
    )
    assert successor.active_turn_lease_id != expired.active_turn_lease_id

    first.release_chat_turn(expired, "client-message-reused")
    observed = first.chat_thread(created.id, owner_principal_id="user-1")
    assert observed is not None
    assert observed.active_turn_id == "client-message-reused"
    assert observed.active_turn_lease_id == successor.active_turn_lease_id


def test_chat_turn_release_retries_an_etag_conflict() -> None:
    container = _SharedThreadContainer()
    store = _cosmos_thread_store(container)
    now = utc_now()
    created = store.save_chat_thread(
        ChatThread(
            id="thread-release-retry",
            project_id=TEST_PROJECT_ID,
            tenant_id="demo",
            capability=Capability.GRANT,
            agent_name="grant-agent",
            owner_principal_id="user-1",
            conversation_id="conversation-1",
            session_id="session-1",
            delegated_user_identity="opaque-user",
            created_at=now,
            updated_at=now,
        )
    )
    claimed = store.claim_chat_turn(created, "client-message-release")
    original_replace = container.replace_item
    attempts = 0

    def replace_once_with_conflict(**kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=412,
                message="stale",
            )
        return original_replace(**kwargs)

    container.replace_item = replace_once_with_conflict  # type: ignore[method-assign]
    store.release_chat_turn(claimed, "client-message-release")

    released = store.chat_thread(created.id, owner_principal_id="user-1")
    assert attempts == 2
    assert released is not None
    assert released.active_turn_id is None


@pytest.fixture(autouse=True)
def isolated_app_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def skip_agent_studio_initialization(
        _application: FastAPI,
        _settings: Settings,
    ) -> None:
        return None

    monkeypatch.setattr(app_module, "get_settings", _settings)
    monkeypatch.setattr(
        app_module,
        "build_workspace_project_provider",
        lambda _settings: _TestWorkspaceProjectProvider(),
    )
    monkeypatch.setattr(
        app_module,
        "_init_agent_studio",
        skip_agent_studio_initialization,
    )


def _principal(tenant_id: str, groups: list[str]) -> str:
    payload = {
        "userId": "user-1",
        "userDetails": "User One",
        "claims": [
            {"typ": "tid", "val": tenant_id},
            *({"typ": "groups", "val": group} for group in groups),
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_health_and_security_headers() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200, response.json()
    assert response.json()["status"] == "healthy"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"].startswith("req-")


def test_capabilities_and_research_endpoint() -> None:
    class FakeHostedGateway:
        def invoke(self, *args: Any, **kwargs: Any) -> HostedAgentReply:
            del args, kwargs
            return HostedAgentReply(
                agent_name="literature-agent",
                content=json.dumps(
                    {
                        "summary": "Auditable research synthesis is ready.",
                        "claims": [
                            {
                                "text": "The response is grounded in the test source.",
                                "support": "supported",
                                "evidence_ids": ["test:evidence"],
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "test:evidence",
                                "title": "Deterministic test source",
                            }
                        ],
                        "limitations": [],
                    }
                ),
                response_id="response-test",
            )

    with TestClient(app) as client:
        app.state.hosted = FakeHostedGateway()
        capabilities = client.get("/api/capabilities")
        result = client.post(
            "/api/research/literature",
            json={
                "query": "Compare auditable research synthesis",
                "project_id": TEST_PROJECT_ID,
                "tenant_id": "demo",
                "group_ids": [],
            },
        )

    assert capabilities.status_code == 200
    assert len(capabilities.json()) == len(Capability)
    assert result.status_code == 200
    assert result.json()["capability"] == "literature"
    assert result.json()["evidence"][0]["evidence_id"] == "test:evidence"


def test_assistant_uses_the_bounded_hosted_capability() -> None:
    class FakeHostedGateway:
        def invoke(self, *args: Any, **kwargs: Any) -> HostedAgentReply:
            del args
            assert kwargs["agent_name"] == "institution-agent"
            return HostedAgentReply(
                agent_name="institution-agent",
                content=json.dumps(
                    {
                        "summary": "Consult the current institutional policy source.",
                        "claims": [],
                        "evidence": [],
                        "limitations": ["No policy source was supplied."],
                    }
                ),
                response_id="response-test",
            )

    with TestClient(app) as client:
        app.state.hosted = FakeHostedGateway()
        response = client.post(
            "/api/assistant",
            json={
                "message": "When must AI be disclosed to the IRB?",
                "capability": "institutional_qa",
            },
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "hosted"
    assert response.json()["agent_name"] == "institution-agent"


def test_ready_workflows_projects_and_missing_run_routes() -> None:
    with TestClient(app) as client:
        ready = client.get("/ready")
        workflows = client.get("/api/workflows")
        projects = client.get("/api/projects")
        missing_run = client.get("/api/runs/run-does-not-exist")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "service": "research-assistant-api",
    }
    assert workflows.status_code == 200
    assert workflows.json()
    assert workflows.json()[0]["stages"]
    assert projects.status_code == 200
    assert projects.json()[0]["id"] == TEST_PROJECT_ID
    assert projects.json()[0]["name"] == "Personal research workspace"
    assert missing_run.status_code == 404
    assert missing_run.json()["detail"] == "Run not found."


def test_grant_message_route_rejects_unready_connector_without_invocation() -> None:
    class CountingGateway:
        send_calls = 0

        def open_thread(self, _agent_name: str, *, user_identity: str) -> ThreadHandle:
            assert user_identity.startswith("ra:")
            return ThreadHandle(
                conversation_id="conversation-1",
                session_id="session-1",
            )

        def send(self, **_kwargs: Any) -> HostedAgentReply:
            self.send_calls += 1
            raise AssertionError("The Hosted Agent must not be invoked")

    gateway = CountingGateway()
    store = WorkspaceStore(project_id="demo-project", tenant_id="demo")

    class StaticWorkspaceProvider:
        def workspace_for(self, _identity: object, _project_id: str | None) -> WorkspaceStore:
            return store

    route_app = FastAPI()
    route_app.include_router(agent_chat_router)
    route_app.state.settings = _settings()
    route_app.state.agent_chat = gateway
    route_app.state.workspace_projects = StaticWorkspaceProvider()
    with TestClient(route_app) as client:
        opened = client.post(
            "/api/agent-chat/threads",
            json={"capability": "grant", "agent_name": "grant-agent"},
        )
        assert opened.status_code == 201, opened.text
        thread_id = opened.json()["id"]

        rejected = client.post(
            f"/api/agent-chat/threads/{thread_id}/messages",
            json={
                "text": "Find a verified genomics opportunity.",
                "client_message_id": "client-message-0001",
            },
        )
        persisted = client.get(f"/api/agent-chat/threads/{thread_id}")

    assert rejected.status_code == 503
    assert rejected.json()["detail"] == (
        "Required connector Grants.gov is not ready for grant-agent. "
        "Test it in Project Settings, then retry."
    )
    assert gateway.send_calls == 0
    assert persisted.status_code == 200
    assert persisted.json()["messages"] == []


def test_message_route_requires_turn_id_and_rejects_durable_lease_before_invocation() -> None:
    class CountingGateway:
        send_calls = 0

        def open_thread(self, _agent_name: str, *, user_identity: str) -> ThreadHandle:
            assert user_identity.startswith("ra:")
            return ThreadHandle(
                conversation_id="conversation-1",
                session_id="session-1",
            )

        def send(self, **_kwargs: Any) -> HostedAgentReply:
            self.send_calls += 1
            raise AssertionError("A leased turn must not invoke the Hosted Agent")

    gateway = CountingGateway()
    store = WorkspaceStore(project_id="demo-project", tenant_id="demo")
    assert store.record_connector_test("grants_gov", "ready") is not None

    class StaticWorkspaceProvider:
        def workspace_for(self, _identity: object, _project_id: str | None) -> WorkspaceStore:
            return store

    route_app = FastAPI()
    route_app.include_router(agent_chat_router)
    route_app.state.settings = _settings()
    route_app.state.agent_chat = gateway
    route_app.state.workspace_projects = StaticWorkspaceProvider()
    with TestClient(route_app) as client:
        opened = client.post(
            "/api/agent-chat/threads",
            json={"capability": "grant", "agent_name": "grant-agent"},
        )
        assert opened.status_code == 201, opened.text
        thread_id = opened.json()["id"]
        thread = store.chat_thread(thread_id, owner_principal_id="demo-researcher")
        assert thread is not None
        store.claim_chat_turn(thread, "client-message-active")

        missing_id = client.post(
            f"/api/agent-chat/threads/{thread_id}/messages",
            json={"text": "Find a verified genomics opportunity."},
        )
        leased = client.post(
            f"/api/agent-chat/threads/{thread_id}/messages",
            json={
                "text": "Find a verified genomics opportunity.",
                "client_message_id": "client-message-0002",
            },
        )

    assert missing_id.status_code == 422
    assert leased.status_code == 409
    assert "already in progress" in leased.json()["detail"]
    assert gateway.send_calls == 0


def test_personal_projects_enforce_owner_selection_and_archive_lifecycle() -> None:
    owner_headers = {
        "X-MS-CLIENT-PRINCIPAL": _principal("demo", ["researchers"]),
    }
    other_user_headers = {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(
            json.dumps(
                {
                    "userId": "user-2",
                    "userDetails": "Another user",
                    "claims": [
                        {"typ": "tid", "val": "demo"},
                        {"typ": "groups", "val": "researchers"},
                    ],
                }
            ).encode()
        ).decode(),
    }
    with TestClient(app) as client:
        app.state.settings = _settings(entra_auth_enforced=True)
        created = client.post(
            "/api/projects",
            headers=owner_headers,
            json={
                "name": "Cancer outcomes review",
                "description": "A private workspace for a bounded evidence review.",
            },
        )

        assert created.status_code == 201
        project = created.json()
        assert project["is_active"] is True

        selected = client.get(
            "/api/workspace",
            headers={**owner_headers, "X-Research-Project-ID": project["id"]},
        )
        foreign = client.get(
            "/api/workspace",
            headers={**other_user_headers, "X-Research-Project-ID": project["id"]},
        )
        archived = client.patch(
            f"/api/projects/{project['id']}",
            headers=owner_headers,
            json={"archive": True},
        )
        after_archive = client.get("/api/workspace", headers=owner_headers)

    assert selected.status_code == 200
    assert selected.json()["project"]["project_id"] == project["id"]
    assert selected.json()["library_items"] == 0
    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "The requested project is unavailable."
    assert archived.status_code == 200
    assert archived.json()["is_active"] is False
    assert after_archive.status_code == 404


def test_ready_reports_missing_hosted_endpoint() -> None:
    with TestClient(app) as client:
        app.state.settings = _settings().model_copy(
            update={"foundry_project_endpoint": ""}
        )
        response = client.get("/ready")

    assert response.status_code == 503, response.json()
    assert response.json()["detail"] == "FOUNDRY_PROJECT_ENDPOINT is required"


def test_workspace_requires_authenticated_identity_when_demo_disabled() -> None:
    with TestClient(app) as client:
        app.state.settings = _settings(entra_auth_enforced=True)
        response = client.get("/api/workspace")

    assert response.status_code == 401, response.json()
    assert response.json()["detail"] == "An authenticated platform identity is required."


def test_public_ingestion_requires_research_admin_role() -> None:
    headers = {
        "X-MS-CLIENT-PRINCIPAL": _principal("demo", ["researchers"]),
    }
    with TestClient(app) as client:
        app.state.settings = _settings(entra_auth_enforced=True)
        project = client.post(
            "/api/projects",
            headers=headers,
            json={
                "name": "Public release review",
                "description": "A personal workspace for reviewing a public release.",
            },
        ).json()
        response = client.post(
            "/api/library/ingest",
            headers={**headers, "X-Research-Project-ID": project["id"]},
            json={
                "title": "Public release candidate",
                "kind": "Policy",
                "source": "Workspace upload",
                "access": "public",
                "license": "Project supplied",
                "description": "Should require explicit admin permission.",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Only a research administrator can classify a source as public."
    )


def test_upload_rejects_empty_and_oversized_runtime_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def empty_read(self: Any, size: int = -1) -> bytes:
        del self, size
        return b""

    monkeypatch.setattr("starlette.datastructures.UploadFile.read", empty_read)
    with TestClient(app) as client:
        empty = client.post(
            "/api/library/upload",
            data={
                "title": "Empty upload",
                "kind": "Policy",
                "access": "internal",
                "license": "Project supplied",
                "description": "Empty content should be rejected.",
            },
            files={"file": ("empty.txt", b"ignored", "text/plain")},
        )

    async def oversized_read(self: Any, size: int = -1) -> bytes:
        del self, size
        return b"x" * 20_000_001

    monkeypatch.setattr("starlette.datastructures.UploadFile.read", oversized_read)
    with TestClient(app) as client:
        oversized = client.post(
            "/api/library/upload",
            data={
                "title": "Large upload",
                "kind": "Policy",
                "access": "internal",
                "license": "Project supplied",
                "description": "Oversized content should be rejected.",
            },
            files={"file": ("large.txt", b"ignored", "text/plain")},
        )

    assert empty.status_code == 422
    assert empty.json()["detail"] == "Uploaded source is empty."
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == "The runtime ingestion limit is 20 MB per source."


def test_update_connector_surfaces_success_validation_and_missing_cases() -> None:
    with TestClient(app) as client:
        connector_id = next(
            item["id"]
            for item in client.get("/api/connectors").json()
            if item["id"] not in {"pubmed", "grants_gov"}
        )
        updated = client.put(
            f"/api/connectors/{connector_id}",
            json={"enabled": True, "assigned_agents": ["literature", "grant"]},
        )
        invalid = client.put(
            f"/api/connectors/{connector_id}",
            json={"enabled": True, "assigned_agents": ["unknown-specialist"]},
        )
        missing = client.put(
            "/api/connectors/missing-connector",
            json={"enabled": True, "assigned_agents": ["literature"]},
        )

    assert updated.status_code == 200
    assert updated.json()["id"] == connector_id
    assert updated.json()["assigned_agents"] == ["literature", "grant"]
    assert invalid.status_code == 422
    assert "unknown specialist" in invalid.json()["detail"]
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Connector not found."


def test_connector_test_surfaces_unavailable_conflict_and_missing_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableGateway:
        async def search(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise ConnectorGatewayError("upstream failed")

        async def close(self) -> None:
            return None

    class ReadyGateway:
        async def search(self, *args: Any, **kwargs: Any) -> Any:
            class Result:
                warnings: tuple[str, ...] = ()

            del args, kwargs
            return Result()

        async def close(self) -> None:
            return None

    with TestClient(app) as client:
        connector_id = client.get("/api/connectors").json()[0]["id"]
        app.state.connector_gateway = UnavailableGateway()
        unavailable = client.post(f"/api/connectors/{connector_id}/test")
        missing = client.post("/api/connectors/missing-connector/test")

        app.state.connector_gateway = ReadyGateway()
        workspace = app.state.workspace_projects.stores_for_reconciliation()[0]
        monkeypatch.setattr(
            workspace,
            "record_connector_test",
            lambda connector_id, status: (_ for _ in ()).throw(ValueError("test conflict")),
        )
        conflict = client.post(f"/api/connectors/{connector_id}/test")

        monkeypatch.setattr(
            workspace,
            "record_connector_test",
            lambda connector_id, status: None,
        )
        vanished = client.post(f"/api/connectors/{connector_id}/test")

    assert unavailable.status_code == 200
    assert unavailable.json()["test_status"] == "unavailable"
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Connector not found."
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "test conflict"
    assert vanished.status_code == 404
    assert vanished.json()["detail"] == "Connector not found."


def test_connector_test_uses_the_configured_screening_assignment() -> None:
    assert app_module._connector_probe_capability(["screening"]) == Capability.SCREENING
    assert app_module._connector_probe_capability(["unknown"]) == Capability.LITERATURE


def test_studio_route_requires_live_agent_chat() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/studios/literature/run",
            json={"objective": "Compare auditable research synthesis"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "This capability runs through the live agent-chat surface."
    )


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (HostedAgentConfigurationError("hosted agent configuration failed"), 503),
        (HostedAgentNotReadyError("agent not ready"), 503),
        (HostedAgentInvocationError("agent failed"), 502),
    ],
)
def test_research_route_maps_hosted_agent_errors(
    error: Exception,
    status_code: int,
) -> None:
    class FakeHostedGateway:
        def invoke(self, *args: Any, **kwargs: Any) -> HostedAgentReply:
            del args, kwargs
            raise error

    with TestClient(app) as client:
        app.state.settings = _settings()
        app.state.hosted = FakeHostedGateway()
        response = client.post(
            "/api/research/literature",
            json={
                "query": "Exercise hosted error mapping",
                "project_id": TEST_PROJECT_ID,
                "tenant_id": "demo",
                "group_ids": [],
            },
        )

    assert response.status_code == status_code
    assert response.json()["detail"] == str(error)
