from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import research_assistant_api.foundry as foundry
from azure.core.credentials import AccessToken, TokenCredential
from fastapi.testclient import TestClient
from openai import APIStatusError
from research_assistant_api.app import _agent_message, app
from research_assistant_api.approval_context import (
    ApprovalContextRejectedError,
    ApprovalContextRequest,
    ApprovalContextResolverScope,
    ApprovalContextUnavailableError,
    ApprovalRejectionCode,
    ResolvedApprovalContext,
    compose_approval_context_resolver,
    resolve_approval_context,
)
from research_assistant_api.config import Settings
from research_assistant_api.foundry import HostedAgentInvocationError, HostedAgentReply
from research_assistant_core.models import Capability, ResearchRequest
from research_assistant_core.service import ResearchService
from research_assistant_core.studio_models import StudioRunRequest


class FakeCredential(TokenCredential):
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("fake", 4_102_444_800)


def test_gateway_forwards_request_tool_preference(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_text="Bounded analysis", id="response-1")

    class FakeProject:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["allow_preview"] is True

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "literature-agent"
            return SimpleNamespace(responses=FakeResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", FakeProject)
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    offline = gateway.invoke(
        "Analyze supplied evidence only.",
        agent_name="literature-agent",
        allow_tools=False,
    )
    online = gateway.invoke(
        "Research current public guidance.",
        agent_name="literature-agent",
        allow_tools=True,
    )

    assert offline.content == "Bounded analysis"
    assert online.response_id == "response-1"
    assert calls == [
        {
            "input": "Analyze supplied evidence only.",
        },
        {
            "input": "Research current public guidance.",
        },
    ]


def test_public_online_agent_message_excludes_internal_objective_and_evidence() -> None:
    generic = ResearchService().run(
        Capability.LITERATURE,
        ResearchRequest(query="internal auditable synthesis objective"),
    )
    payload = StudioRunRequest(
        objective="Confidential project objective must remain internal.",
        online_research=True,
        inputs={
            "public_search_query": "current public reproducibility guidance",
            "public_research_acknowledged": True,
        },
    )

    message = _agent_message(Capability.LITERATURE, payload, generic)
    envelope = json.loads(message)

    assert "current public reproducibility guidance" in message
    assert "Confidential project objective" not in message
    assert generic.citations[0].quote not in message
    assert envelope["evidence"] == []


def test_agent_message_maps_dataset_approval_and_coordinator_routes() -> None:
    service = ResearchService()
    dataset = service.run(
        Capability.DATASET,
        ResearchRequest(
            query="Profile approved data",
            context={"csv_text": "value\n1\n"},
        ),
    )
    dataset_message = json.loads(
        _agent_message(
            Capability.DATASET,
            StudioRunRequest(
                objective="Profile approved data",
                inputs={
                    "filename": "approved.csv",
                },
            ),
            dataset,
        )
    )
    assert "approved_compute" not in dataset_message
    assert "approval_decision_id" not in dataset_message
    assert "invocation_id" not in dataset_message
    assert "idempotency_key" not in dataset_message

    orchestration = service.run(
        Capability.ORCHESTRATION,
        ResearchRequest(query="Coordinate a literature and grant review"),
    )
    coordinator_message = json.loads(
        _agent_message(
            Capability.ORCHESTRATION,
            StudioRunRequest(
                objective="Coordinate a literature and grant review",
                inputs={"requested_capabilities": ["literature", "grant"]},
            ),
            orchestration,
        )
    )
    assert coordinator_message["requested_capabilities"] == [
        "literature",
        "grant",
    ]
    with pytest.raises(ValueError, match="requested_capabilities"):
        _agent_message(
            Capability.ORCHESTRATION,
            StudioRunRequest(objective="Invalid orchestration", inputs={}),
            orchestration,
        )


def test_dataset_hosted_envelope_is_bounded_and_uses_stable_caller_key() -> None:
    service = ResearchService()
    result = service.run(
        Capability.DATASET,
        ResearchRequest(query="Profile bounded data"),
    )
    envelope = json.loads(
        _agent_message(
            Capability.DATASET,
            StudioRunRequest(
                objective="Profile bounded data",
                inputs={
                    "filename": "large.csv",
                    "csv_text": "x" * 50_000,
                    "idempotency_key": "dataset-operation-1",
                },
            ),
            result,
            approval_context=ResolvedApprovalContext(
                request_digest="a" * 64,
                approval_decision_id="approval-server-1",
                invocation_id="invocation-server-1",
            ),
        )
    )
    assert len(envelope["query"]) == 40_000
    assert envelope["query"].endswith("[INPUT TRUNCATED TO HOSTED CONTRACT LIMIT]")
    assert "approved_compute" not in envelope
    assert envelope["approval_decision_id"] == "approval-server-1"
    assert envelope["invocation_id"] == "invocation-server-1"
    assert envelope["idempotency_key"] == "dataset-operation-1"


class FakeApprovalContextResolver:
    is_durable = True

    def __init__(
        self,
        rejection: ApprovalRejectionCode | None = None,
        *,
        mismatch: bool = False,
    ) -> None:
        self.rejection = rejection
        self.mismatch = mismatch
        self.requests: list[ApprovalContextRequest] = []

    async def resolve(self, request: ApprovalContextRequest) -> ResolvedApprovalContext:
        self.requests.append(request)
        if self.rejection is not None:
            raise ApprovalContextRejectedError(
                self.rejection,
                "The durable decision is not consumable.",
            )
        return ResolvedApprovalContext(
            request_digest=("0" * 64 if self.mismatch else request.digest),
            approval_decision_id="approval-server-1",
            invocation_id="invocation-server-1",
        )


class FakeApprovalContextResolverFactory:
    def __init__(self, resolver: Any) -> None:
        self.resolver = resolver
        self.scopes: list[ApprovalContextResolverScope] = []

    def build(self, scope: ApprovalContextResolverScope) -> Any:
        self.scopes.append(scope)
        return self.resolver


class CapturingDatasetGateway:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def invoke(
        self,
        message: str,
        *,
        agent_name: str | None = None,
        allow_tools: bool = True,
    ) -> HostedAgentReply:
        del allow_tools
        self.messages.append(message)
        return HostedAgentReply(
            agent_name=agent_name or "dataset-agent",
            content="Bounded dataset analysis",
            response_id="response-dataset-1",
        )


def _hosted_dataset_request(**inputs: Any) -> dict[str, Any]:
    return {
        "query": "Profile the approved bounded dataset",
        "tenant_id": "demo",
        "project_id": "demo-project",
        "context": {
            "filename": "approved.csv",
            "csv_text": "value\n1\n",
            "approval_reference": "approval-request-1",
            "idempotency_key": "dataset-operation-1",
            **inputs,
        },
    }


def test_dataset_api_uses_only_trusted_resolved_approval_context() -> None:
    resolver = FakeApprovalContextResolver()
    gateway = CapturingDatasetGateway()
    with TestClient(app) as client:
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test/api/projects/test",
        )
        app.state.approval_context_resolver = resolver
        app.state.hosted = gateway
        response = client.post("/api/research/dataset", json=_hosted_dataset_request())

    assert response.status_code == 200
    assert len(resolver.requests) == 1
    approval_request = resolver.requests[0]
    assert approval_request.tenant_id == "demo"
    assert approval_request.project_id == "demo-project"
    assert approval_request.actor_id == "demo-researcher"
    assert approval_request.operation_id == "dataset.compute"
    envelope = json.loads(gateway.messages[0])
    assert envelope["approval_decision_id"] == "approval-server-1"
    assert envelope["invocation_id"] == "invocation-server-1"
    assert envelope["idempotency_key"] == "dataset-operation-1"
    assert "approved_compute" not in envelope
    assert "analysis_approved" not in envelope


def test_dataset_api_fails_closed_without_trusted_resolver() -> None:
    gateway = CapturingDatasetGateway()
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted")
        app.state.hosted = gateway
        response = client.post("/api/research/dataset", json=_hosted_dataset_request())

    assert response.status_code == 503
    assert gateway.messages == []


def test_dataset_api_rejects_client_supplied_authority_before_resolution() -> None:
    resolver = FakeApprovalContextResolver()
    gateway = CapturingDatasetGateway()
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted")
        app.state.approval_context_resolver = resolver
        app.state.hosted = gateway
        response = client.post(
            "/api/research/dataset",
            json=_hosted_dataset_request(
                approval_decision_id="forged-decision",
                invocation_id="forged-invocation",
            ),
        )

    assert response.status_code == 422
    assert resolver.requests == []
    assert gateway.messages == []


@pytest.mark.parametrize("missing_field", ["approval_reference", "idempotency_key"])
def test_dataset_api_rejects_missing_server_context_fields(
    missing_field: str,
) -> None:
    resolver = FakeApprovalContextResolver()
    payload = _hosted_dataset_request()
    payload["context"].pop(missing_field)
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted")
        app.state.approval_context_resolver = resolver
        response = client.post("/api/research/dataset", json=payload)

    assert response.status_code == 403
    assert response.json()["detail"].endswith("(missing).")
    assert resolver.requests == []


@pytest.mark.parametrize("rejection", ["expired", "forged", "missing", "revoked"])
def test_dataset_api_rejects_unconsumable_durable_decisions(
    rejection: ApprovalRejectionCode,
) -> None:
    resolver = FakeApprovalContextResolver(rejection)
    gateway = CapturingDatasetGateway()
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted")
        app.state.approval_context_resolver = resolver
        app.state.hosted = gateway
        response = client.post("/api/research/dataset", json=_hosted_dataset_request())

    assert response.status_code == 403
    assert response.json()["detail"].endswith(f"({rejection}).")
    assert gateway.messages == []


def test_dataset_api_rejects_mismatched_resolver_output() -> None:
    resolver = FakeApprovalContextResolver(mismatch=True)
    gateway = CapturingDatasetGateway()
    with TestClient(app) as client:
        app.state.settings = Settings(execution_mode="hosted")
        app.state.approval_context_resolver = resolver
        app.state.hosted = gateway
        response = client.post("/api/research/dataset", json=_hosted_dataset_request())

    assert response.status_code == 403
    assert response.json()["detail"].endswith("(mismatch).")
    assert gateway.messages == []


@pytest.mark.asyncio
async def test_approval_context_helper_fails_closed_without_resolver() -> None:
    request = ApprovalContextRequest.from_inputs(
        tenant_id="tenant-a",
        project_id="project-a",
        actor_id="actor-a",
        inputs={
            "approval_reference": "approval-request-1",
            "idempotency_key": "dataset-operation-1",
        },
    )
    with pytest.raises(ApprovalContextUnavailableError):
        await resolve_approval_context(None, request)


def test_approval_resolver_composition_requires_a_durable_app_owned_provider() -> None:
    scope = ApprovalContextResolverScope(
        tenant_id="tenant-a",
        project_id="project-a",
        environment="production",
    )
    assert compose_approval_context_resolver(None, scope, required=False) is None
    with pytest.raises(ApprovalContextUnavailableError, match="no provider"):
        compose_approval_context_resolver(None, scope, required=True)

    durable = FakeApprovalContextResolver()
    factory = FakeApprovalContextResolverFactory(durable)
    assert compose_approval_context_resolver(factory, scope, required=True) is durable
    assert factory.scopes == [scope]

    non_durable = SimpleNamespace(is_durable=False)
    with pytest.raises(ApprovalContextUnavailableError, match="not a durable"):
        compose_approval_context_resolver(
            FakeApprovalContextResolverFactory(non_durable),
            scope,
            required=True,
        )


def test_api_lifespan_installs_required_production_approval_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_module = importlib.import_module("research_assistant_api.app")
    production = Settings(
        environment="production",
        workspace_tenant_id="tenant-real",
        workspace_project_id="project-real",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: production)
    resolver = FakeApprovalContextResolver()
    factory = FakeApprovalContextResolverFactory(resolver)
    app.state.approval_context_resolver_factory = factory
    try:
        with TestClient(app):
            assert app.state.approval_context_resolver is resolver
            assert factory.scopes == [
                ApprovalContextResolverScope(
                    tenant_id="tenant-real",
                    project_id="project-real",
                    environment="production",
                )
            ]
    finally:
        del app.state.approval_context_resolver_factory

    with (
        pytest.raises(ApprovalContextUnavailableError, match="no provider"),
        TestClient(app),
    ):
        pass


@pytest.mark.asyncio
async def test_approval_context_helper_rejects_non_durable_resolver() -> None:
    request = ApprovalContextRequest.from_inputs(
        tenant_id="tenant-a",
        project_id="project-a",
        actor_id="actor-a",
        inputs={
            "approval_reference": "approval-request-1",
            "idempotency_key": "dataset-operation-1",
        },
    )
    with pytest.raises(ApprovalContextUnavailableError, match="not durable"):
        await resolve_approval_context(
            cast(Any, SimpleNamespace(is_durable=False)),
            request,
        )


def test_gateway_retries_documented_session_not_ready_sequence(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class RetryResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise APIStatusError(
                    "session not ready",
                    response=httpx.Response(
                        424,
                        request=httpx.Request(
                            "POST",
                            "https://foundry.example.test/responses",
                        ),
                    ),
                    body={"error": {"code": "session_not_ready"}},
                )
            return SimpleNamespace(output_text="ready", id="response-ready")

    class RetryProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "dataset-agent"
            return SimpleNamespace(responses=RetryResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", RetryProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    response = gateway.invoke(
        "profile supplied data",
        agent_name="dataset-agent",
    )

    assert response.content == "ready"
    assert attempts == 3
    assert sleeps == [15, 30]


def test_gateway_rejects_empty_success_shaped_agent_response(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            return SimpleNamespace(output_text="  ", id="empty-response")

    class EmptyProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            return SimpleNamespace(responses=EmptyResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", EmptyProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    with pytest.raises(HostedAgentInvocationError, match="returned no output"):
        gateway.invoke("public query", agent_name="literature-online-agent")

    assert attempts == 3
    assert sleeps == [2, 5]


def test_gateway_recovers_from_transient_empty_agent_output(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class TransientEmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return SimpleNamespace(output_text=" ", id="empty-response")
            return SimpleNamespace(output_text="Recovered analysis", id="response-ready")

    class TransientEmptyProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "literature-agent"
            return SimpleNamespace(responses=TransientEmptyResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", TransientEmptyProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    response = gateway.invoke("analyze evidence", agent_name="literature-agent")

    assert response.content == "Recovered analysis"
    assert response.response_id == "response-ready"
    assert attempts == 2
    assert sleeps == [2]
