from __future__ import annotations

import base64
import importlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from research_assistant_api.app import app
from research_assistant_api.config import Settings
from research_assistant_api.connector_gateway import ConnectorGatewayError
from research_assistant_api.foundry import (
    HostedAgentConfigurationError,
    HostedAgentInvocationError,
    HostedAgentNotReadyError,
    HostedAgentReply,
)
from research_assistant_api.orchestration import RunSchedulingError

app_module = importlib.import_module("research_assistant_api.app")


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

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"].startswith("req-")


def test_capabilities_and_research_endpoint() -> None:
    with TestClient(app) as client:
        capabilities = client.get("/api/capabilities")
        result = client.post(
            "/api/research/literature",
            json={"query": "Compare auditable research synthesis"},
        )

    assert capabilities.status_code == 200
    assert len(capabilities.json()) == 6
    assert result.status_code == 200
    assert result.json()["run"]["capability"] == "literature"
    assert result.json()["citations"]


def test_mock_assistant_uses_bounded_capability() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/assistant",
            json={
                "message": "When must AI be disclosed to the IRB?",
                "capability": "institutional_qa",
            },
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "bounded"
    assert response.json()["agent_name"] == "institutional_qa-deterministic"


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
        "mode": "mock",
    }
    assert workflows.status_code == 200
    assert workflows.json()
    assert workflows.json()[0]["stages"]
    assert projects.status_code == 200
    assert projects.json()[0]["id"] == "demo-project"
    assert missing_run.status_code == 404
    assert missing_run.json()["detail"] == "Run not found."


def test_ready_reports_missing_hosted_endpoint() -> None:
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted", foundry_project_endpoint=None)
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "Hosted mode is missing FOUNDRY_PROJECT_ENDPOINT"


def test_workspace_requires_authenticated_identity_when_demo_disabled() -> None:
    with TestClient(app) as client:
        app.state.settings = Settings(
            allow_demo_identity=False,
            trust_platform_identity_headers=True,
        )
        response = client.get("/api/workspace")

    assert response.status_code == 401
    assert response.json()["detail"] == "An authenticated platform identity is required."


def test_public_ingestion_requires_research_admin_role() -> None:
    headers = {
        "X-MS-CLIENT-PRINCIPAL": _principal("demo", ["researchers"]),
    }
    with TestClient(app) as client:
        app.state.settings = Settings(
            allow_demo_identity=False,
            trust_platform_identity_headers=True,
        )
        response = client.post(
            "/api/library/ingest",
            headers=headers,
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


def test_ingestion_scheduling_failure_marks_item_and_run_failed() -> None:
    class FailingScheduler:
        configured = True

        def schedule(self, *, instance_id: str, payload: dict[str, Any]) -> str:
            del instance_id, payload
            raise RunSchedulingError("Scheduler rejected the ingest request.")

        def approve(self, **kwargs: Any) -> None:
            del kwargs

        def close(self) -> None:
            return None

    with TestClient(app) as client:
        app.state.scheduler = FailingScheduler()
        before_ids = {item["id"] for item in client.get("/api/library").json()}
        response = client.post(
            "/api/library/ingest",
            json={
                "title": "Scheduling failure artifact",
                "kind": "Policy",
                "source": "Workspace upload",
                "access": "internal",
                "license": "Project supplied",
                "description": "Exercise deterministic failure handling.",
            },
        )
        after_library = client.get("/api/library").json()
        created_item = next(item for item in after_library if item["id"] not in before_ids)
        created_run = next(
            run
            for run in client.get("/api/runs").json()
            if run["title"] == "Ingest Scheduling failure artifact"
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Scheduler rejected the ingest request."
    assert created_item["status"] == "blocked"
    assert "Ingestion blocked: Scheduler rejected the ingest request." in created_item[
        "description"
    ]
    assert created_run["status"] == "failed"
    assert created_run["scheduling_state"] == "failed"


def test_studio_scheduling_failure_marks_run_failed_and_cancels_approval() -> None:
    class FailingScheduler:
        configured = True

        def schedule(self, *, instance_id: str, payload: dict[str, Any]) -> str:
            del instance_id, payload
            raise RunSchedulingError("Scheduler rejected the studio run.")

        def approve(self, **kwargs: Any) -> None:
            del kwargs

        def close(self) -> None:
            return None

    with TestClient(app) as client:
        app.state.scheduler = FailingScheduler()
        before_run_ids = {run["id"] for run in client.get("/api/runs").json()}
        before_approval_ids = {approval["id"] for approval in client.get("/api/approvals").json()}
        response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Create a scheduler failure that cancels approval"},
        )
        created_run = next(
            run for run in client.get("/api/runs").json() if run["id"] not in before_run_ids
        )
        created_approval = next(
            approval
            for approval in client.get("/api/approvals").json()
            if approval["id"] not in before_approval_ids
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Scheduler rejected the studio run."
    assert created_run["status"] == "failed"
    assert created_run["scheduling_state"] == "failed"
    assert created_approval["state"] == "cancelled"
    assert created_approval["rationale"] == "Scheduler rejected the studio run."


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
        monkeypatch.setattr(
            app.state.workspace,
            "record_connector_test",
            lambda connector_id, status: (_ for _ in ()).throw(ValueError("test conflict")),
        )
        conflict = client.post(f"/api/connectors/{connector_id}/test")

        monkeypatch.setattr(
            app.state.workspace,
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


def test_approval_routes_handle_missing_records_delivery_failures_and_scheduler_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = None
    missing_after_decide = None
    missing_run = None
    scheduler_failure = None

    with TestClient(app) as client:
        missing = client.post(
            "/api/approvals/missing-approval/decision",
            json={"decision": "approved", "rationale": "Missing approval"},
        )

        existing_id = client.get("/api/approvals").json()[0]["id"]
        monkeypatch.setattr(app.state.workspace, "decide_approval", lambda *args: None)
        missing_after_decide = client.post(
            f"/api/approvals/{existing_id}/decision",
            json={"decision": "approved", "rationale": "Race lost"},
        )

    class RecordingScheduler:
        configured = True

        def __init__(self, *, fail_on_approve: bool = False) -> None:
            self.fail_on_approve = fail_on_approve

        def schedule(self, *, instance_id: str, payload: dict[str, Any]) -> str:
            del payload
            return instance_id

        def approve(
            self,
            *,
            instance_id: str,
            approval_id: str,
            idempotency_key: str,
            approved: bool,
        ) -> None:
            del instance_id, approval_id, idempotency_key, approved
            if self.fail_on_approve:
                raise RunSchedulingError("Approval event delivery failed.")

        def close(self) -> None:
            return None

    with TestClient(app) as client:
        app.state.scheduler = RecordingScheduler()
        run_response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Create an approval that loses its run"},
        )
        approval_id = next(
            item["id"]
            for item in client.get("/api/approvals").json()
            if item["run_id"] == run_response.json()["run"]["id"]
        )
        monkeypatch.setattr(app.state.workspace, "run", lambda run_id: None)
        missing_run = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"decision": "approved", "rationale": "Run disappeared."},
        )

    with TestClient(app) as client:
        scheduler = RecordingScheduler(fail_on_approve=True)
        app.state.scheduler = scheduler
        run_response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Create an approval that fails delivery"},
        )
        approval_id = next(
            item["id"]
            for item in client.get("/api/approvals").json()
            if item["run_id"] == run_response.json()["run"]["id"]
        )
        scheduler_failure = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"decision": "approved", "rationale": "Delivery should fail."},
        )
        stored = next(
            item for item in client.get("/api/approvals").json() if item["id"] == approval_id
        )

    assert missing is not None and missing.status_code == 404
    assert missing.json()["detail"] == "Approval not found."
    assert missing_after_decide is not None and missing_after_decide.status_code == 404
    assert missing_after_decide.json()["detail"] == "Approval not found."
    assert missing_run is not None and missing_run.status_code == 409
    assert missing_run.json()["detail"] == "Approval run no longer exists."
    assert scheduler_failure is not None and scheduler_failure.status_code == 503
    assert scheduler_failure.json()["detail"] == "Approval event delivery failed."
    assert stored["event_delivery"] == "failed"


def test_studio_route_surfaces_research_service_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.research,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid study request")),
        )
        response = client.post(
            "/api/studios/literature/run",
            json={"objective": "Trigger a validation error"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid study request"


def test_studio_route_uses_hosted_agent_for_online_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_public_metadata(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["metadata"] = {"args": args, "kwargs": kwargs}
        return [{"source": "pubmed", "status": "ready", "records": [{"id": "pub-1"}]}]

    class FakeHostedGateway:
        def invoke(
            self,
            message: str,
            *,
            agent_name: str | None = None,
            allow_tools: bool = True,
        ) -> HostedAgentReply:
            calls["invoke"] = {
                "message": message,
                "agent_name": agent_name,
                "allow_tools": allow_tools,
            }
            return HostedAgentReply(
                agent_name=agent_name or "missing",
                content="Hosted synthesis source_id: paper-workflow",
                response_id="response-1",
            )

    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        monkeypatch.setattr(app_module, "retrieve_public_metadata", fake_public_metadata)
        response = client.post(
            "/api/studios/literature/run",
            json={
                "objective": "Compare public reproducibility guidance",
                "online_research": True,
                "inputs": {
                    "public_search_query": "public reproducibility guidance",
                    "public_research_acknowledged": True,
                    "sources": ["PubMed"],
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["insight"]["agent_name"] == "literature-online-agent"
    assert response.json()["insight"]["online_research_used"] is True
    assert calls["invoke"]["agent_name"] == "literature-online-agent"
    assert calls["invoke"]["allow_tools"] is True
    assert "public reproducibility guidance" in calls["invoke"]["message"]
    assert calls["metadata"]["kwargs"]["requested_sources"] == ["PubMed"]


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (
            HostedAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT is required in hosted execution mode"),
            503,
        ),
        (HostedAgentNotReadyError("agent not ready"), 503),
        (HostedAgentInvocationError("agent failed"), 502),
    ],
)
def test_studio_route_maps_hosted_agent_errors(
    error: Exception,
    status_code: int,
) -> None:
    class FakeHostedGateway:
        def invoke(self, *args: Any, **kwargs: Any) -> HostedAgentReply:
            del args, kwargs
            raise error

    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        response = client.post(
            "/api/studios/literature/run",
            json={"objective": "Exercise hosted error mapping"},
        )

    assert response.status_code == status_code
    assert response.json()["detail"] == str(error)


def test_research_route_rejects_wrong_project_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app) as client:
        wrong_project = client.post(
            "/api/research/literature",
            json={
                "query": "Compare bounded synthesis",
                "project_id": "other-project",
            },
        )
        monkeypatch.setattr(
            app.state.research,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad query")),
        )
        invalid = client.post(
            "/api/research/literature",
            json={"query": "Compare bounded synthesis"},
        )

    assert wrong_project.status_code == 403
    assert wrong_project.json()["detail"] == "Request project is not authorized for this workspace."
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "bad query"


def test_research_route_uses_hosted_agent_and_records_unresolved_insight() -> None:
    class FakeHostedGateway:
        def invoke(
            self,
            message: str,
            *,
            agent_name: str | None = None,
            allow_tools: bool = True,
        ) -> HostedAgentReply:
            assert agent_name == "literature-agent"
            assert allow_tools is False
            assert "Authorized evidence" in message
            return HostedAgentReply(
                agent_name="literature-agent",
                content="Hosted analysis source_id: paper-workflow; source_id: invented-source",
                response_id="response-42",
            )

    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        response = client.post(
            "/api/research/literature",
            json={"query": "Compare auditable research synthesis"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["hosted_agent_response_id"] == "response-42"
    assert payload["metadata"]["hosted_agent_insight"]["agent_name"] == "literature-agent"
    assert payload["metadata"]["hosted_agent_insight"]["unresolved_source_ids"] == [
        "invented-source"
    ]
    assert payload["provenance"]["model_deployment"] == "foundry-hosted:literature-agent"
    assert any(
        "unresolved source identifiers" in caveat
        for caveat in payload["provenance"]["caveats"]
    )


def test_research_route_fetches_public_metadata_for_online_hosted_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_public_metadata(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["metadata"] = {"args": args, "kwargs": kwargs}
        return [{"source": "pubmed", "status": "ready", "records": []}]

    class FakeHostedGateway:
        def invoke(
            self,
            message: str,
            *,
            agent_name: str | None = None,
            allow_tools: bool = True,
        ) -> HostedAgentReply:
            calls["invoke"] = {
                "message": message,
                "agent_name": agent_name,
                "allow_tools": allow_tools,
            }
            return HostedAgentReply(
                agent_name=agent_name or "missing",
                content="Hosted online analysis source_id: paper-workflow",
                response_id="response-online",
            )

    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        monkeypatch.setattr(app_module, "retrieve_public_metadata", fake_public_metadata)
        response = client.post(
            "/api/research/literature",
            json={
                "query": "Compare public guidance",
                "context": {
                    "online_research": True,
                    "public_search_query": "current public guidance",
                    "public_research_acknowledged": True,
                    "sources": ["PubMed"],
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["metadata"]["online_research"] is True
    assert calls["invoke"]["agent_name"] == "literature-online-agent"
    assert calls["invoke"]["allow_tools"] is True
    assert calls["metadata"]["kwargs"]["requested_sources"] == ["PubMed"]


def test_research_route_deselecting_all_grant_sources_makes_zero_gateway_calls() -> None:
    # Full-stack regression for the "sources"/"funding_sources" key
    # mismatch: the client sends `context["sources"]`, the route must
    # forward it verbatim (not silently drop to `None`/defaults), and an
    # explicit empty list -- every connector deselected in the UI -- must
    # reach `retrieve_public_metadata` unmocked (the real production
    # function, not a fake) and result in zero live connector-gateway
    # calls. A raising fake gateway proves this: if any connector were
    # still searched, the route would surface a 500 from the
    # AssertionError instead of a clean 200 with empty metadata.
    class _UnreachableConnectorGateway:
        async def search(
            self,
            capability: Any,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> Any:
            raise AssertionError(
                f"connector gateway search() must not be called when every "
                f"source was deselected (capability={capability}, "
                f"source={source}, query={query}, limit={limit})"
            )

        async def close(self) -> None:
            return None

    calls: dict[str, Any] = {}

    class FakeHostedGateway:
        def invoke(
            self,
            message: str,
            *,
            agent_name: str | None = None,
            allow_tools: bool = True,
        ) -> HostedAgentReply:
            calls["invoke"] = {
                "message": message,
                "agent_name": agent_name,
                "allow_tools": allow_tools,
            }
            return HostedAgentReply(
                agent_name=agent_name or "missing",
                content="Hosted synthesis source_id: paper-workflow",
                response_id="response-grant-empty-sources",
            )

    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        app.state.connector_gateway = _UnreachableConnectorGateway()
        response = client.post(
            "/api/research/grant",
            json={
                "query": "Compare public funding opportunities",
                "context": {
                    "online_research": True,
                    "public_search_query": "current public funding guidance",
                    "public_research_acknowledged": True,
                    "sources": [],
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["metadata"]["online_research"] is True
    assert calls["invoke"]["agent_name"] == "grant-online-agent"


def test_research_route_rejects_conflicting_sources_and_legacy_funding_sources() -> None:
    # `funding_sources` is a retired alias no production code path reads.
    # If a caller (a stale client build, a hand-crafted request, etc.) sends
    # both it and the canonical `sources` field with disagreeing values, the
    # server must refuse to guess which list is authoritative rather than
    # silently honoring one and dropping the other.
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="mock")
        response = client.post(
            "/api/research/grant",
            json={
                "query": "Compare public funding opportunities",
                "context": {
                    "sources": ["NSF"],
                    "funding_sources": ["NIH"],
                },
            },
        )

    assert response.status_code == 422
    assert "funding_sources" in response.json()["detail"]
    assert "sources" in response.json()["detail"]


def test_research_route_allows_matching_sources_and_legacy_funding_sources() -> None:
    # Non-conflicting duplication (both fields present, identical value) is
    # not ambiguous -- only a disagreement between the two is rejected.
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="mock")
        response = client.post(
            "/api/research/grant",
            json={
                "query": "Compare public funding opportunities",
                "context": {
                    "sources": ["NSF"],
                    "funding_sources": ["NSF"],
                },
            },
        )

    assert response.status_code == 200


def test_studio_run_route_rejects_conflicting_sources_and_legacy_funding_sources() -> None:
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="mock")
        response = client.post(
            "/api/studios/grant/run",
            json={
                "objective": "Compare public funding opportunities in depth",
                "inputs": {
                    "sources": ["NSF"],
                    "funding_sources": ["NIH"],
                },
            },
        )

    assert response.status_code == 422
    assert "funding_sources" in response.json()["detail"]


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (
            HostedAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT is required in hosted execution mode"),
            503,
        ),
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
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.hosted = FakeHostedGateway()
        response = client.post(
            "/api/research/literature",
            json={"query": "Exercise hosted error mapping"},
        )

    assert response.status_code == status_code
    assert response.json()["detail"] == str(error)
