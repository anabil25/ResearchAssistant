from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from research_assistant_connector_adapter.app import app
from research_assistant_connector_adapter.provider_api import ProviderInvokePayload
from research_assistant_connector_adapter.provider_runtime import (
    APPROVAL_CONSUMPTION_URL_ENV,
    PROVIDER_DEADLINE_SECONDS_ENV,
    PROVIDER_RELEASE_ID_ENV,
    DurableApprovalConsumptionClient,
    build_provider_runtime,
    provider_environment_from_environment,
)
from research_assistant_connectors.providers import (
    ApprovalConsumptionRequest,
    ApprovalConsumptionStatus,
    ApprovalUsePolicy,
    AsyncProviderAdapter,
    AsyncProviderRegistry,
    CapabilityBinding,
    CapabilityInstance,
    DiscoveryResult,
    HealthReport,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    ProviderDescriptor,
    ProviderTimeoutError,
    ValidationReport,
    WebhookConfig,
    WebhookProvider,
    policy_reference,
)
from starlette.requests import Request


class FakeCredential:
    def get_token(self, *scopes: str) -> Any:
        assert scopes
        return SimpleNamespace(token="provider-token")


@dataclass
class BlockingCallState:
    started: Event
    release: Event
    cancelled: Event
    continued: Event


class CooperativeBlockingProvider:
    def __init__(
        self,
        states: dict[str, BlockingCallState],
    ) -> None:
        self._delegate = WebhookProvider(
            WebhookConfig(
                "https://hooks.test/events",
                "tenant",
                "publish",
                health_method=None,
            )
        )
        self._states = states

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._delegate.descriptor

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        state = self._states[context.principal_id]
        state.started.set()
        while not state.release.wait(0.005):
            if context.is_cancelled():
                state.cancelled.set()
                raise ProviderTimeoutError(
                    "Blocking provider observed cancellation",
                    provider_id=self.descriptor.provider_id,
                )
        context.raise_if_cancelled_or_expired(
            provider_id=self.descriptor.provider_id
        )
        state.continued.set()
        return self._delegate.discover(context)

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return self._delegate.validate(target, context)

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        return self._delegate.health(target, context)

    def invoke(
        self,
        request: InvocationRequest,
        context: InvocationContext,
    ) -> InvocationResult:
        return self._delegate.invoke(request, context)


def _blocking_state() -> BlockingCallState:
    return BlockingCallState(Event(), Event(), Event(), Event())


def _invocation_context(
    principal_id: str,
    *,
    deadline_seconds: float = 30.0,
) -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant",
        principal_id=principal_id,
        project_id="project",
        credential=None,
        transport=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        correlation_id=f"correlation-{principal_id}",
        trace_id=f"trace-{principal_id}",
        sleep=lambda _: None,
        release_id="release",
        invocation_id=f"invocation-{principal_id}",
        deadline_at=(
            datetime.now(UTC) + timedelta(seconds=deadline_seconds)
        ).isoformat(),
    )


def _request(*, principal_id: str | None = "caller") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/providers/azure_ai_search/capabilities",
            "headers": [],
            "state": {},
        }
    )
    request.state.request_id = "request-1"
    request.state.provider_cancelled = False
    if principal_id is not None:
        request.state.authenticated_principal_id = principal_id
    return request


def _environment(**updates: str) -> dict[str, str]:
    values = {
        "RESEARCH_WORKSPACE_TENANT_ID": "tenant",
        "RESEARCH_WORKSPACE_PROJECT_ID": "project",
        PROVIDER_RELEASE_ID_ENV: "release-1",
    }
    values.update(updates)
    return values


def _consumption_request() -> ApprovalConsumptionRequest:
    return ApprovalConsumptionRequest(
        decision_id="decision",
        provider_contract_version="research-assistant.integration-provider.v7",
        tenant_id="tenant",
        project_id="project",
        principal_id="principal",
        binding_id="binding",
        instance_fingerprint="1" * 64,
        descriptor_id="descriptor",
        descriptor_version="1",
        operation_id="operation",
        operation_version="1",
        arguments_hash="2" * 64,
        resolved_destination_hash="3" * 64,
        policy_ref=policy_reference("policy"),
        release_id="release",
        invocation_id="invocation",
        idempotency_key="key",
        use_policy=ApprovalUsePolicy.ONE_TIME,
        max_uses=1,
    )


def test_unconfigured_runtime_advertises_explicit_warning() -> None:
    runtime = build_provider_runtime({})
    catalog = asyncio.run(runtime.service.catalog())
    assert catalog["providers"] == []
    assert catalog["warnings"] == [
        {
            "reason_code": "provider_not_configured",
            "message": "No integration provider endpoints are configured for this deployment.",
            "provider_id": "integration_provider",
            "instance_id": None,
        }
    ]


def test_environment_builds_real_registry_and_authenticated_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": [{"name": "research-index"}]})

    runtime = build_provider_runtime(
        _environment(AZURE_SEARCH_ENDPOINT="https://search.test"),
        credential_factory=FakeCredential,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert isinstance(runtime.service.registry, AsyncProviderRegistry)
    assert all(
        isinstance(provider, AsyncProviderAdapter)
        for provider in runtime.service.registry.providers.values()
    )

    async def exercise_runtime() -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
    ]:
        request = _request()
        catalog_result = await runtime.service.catalog()
        discovery_result = await runtime.service.discover(
            "azure_ai_search",
            request,
        )
        instance_id = discovery_result["instances"][0]["instance_id"]
        validation_result = await runtime.service.validate(
            "azure_ai_search",
            instance_id,
            request,
        )
        health_result = await runtime.service.health(
            "azure_ai_search",
            instance_id,
            request,
        )
        invocation_result = await runtime.service.invoke(
            "azure_ai_search",
            ProviderInvokePayload(
                instance_id=instance_id,
                operation_id="search.documents.query",
                arguments={"search": "governed"},
            ),
            request,
        )
        return (
            catalog_result,
            discovery_result,
            validation_result,
            health_result,
            invocation_result,
        )

    catalog, discovery, validation, health, invocation = asyncio.run(
        exercise_runtime()
    )
    assert [item["provider_id"] for item in catalog["providers"]] == [
        "azure_ai_search"
    ]
    assert discovery["instances"][0]["tenant_id"] == "tenant"
    assert discovery["instances"][0]["project_id"] == "project"
    assert validation["readiness"] == "ready"
    assert health["readiness"] == "ready"
    assert invocation["operation_id"] == "search.documents.query"
    assert requests[0].headers["authorization"] == "Bearer provider-token"

    with pytest.raises(HTTPException, match="Authenticated provider caller"):
        asyncio.run(
            runtime.service.discover(
                "azure_ai_search",
                _request(principal_id=None),
            )
        )


def test_production_default_app_uses_async_provider_registry() -> None:
    assert isinstance(app.state.provider_service.registry, AsyncProviderRegistry)


def test_async_provider_adapter_deadline_and_cancellation_are_cooperative() -> None:
    timeout_state = _blocking_state()
    timeout_adapter = AsyncProviderAdapter(
        CooperativeBlockingProvider({"timeout": timeout_state})
    )

    async def timeout_scenario() -> None:
        with pytest.raises(ProviderTimeoutError, match="deadline"):
            await timeout_adapter.discover(
                _invocation_context("timeout", deadline_seconds=0.03)
            )
        assert await asyncio.to_thread(timeout_state.cancelled.wait, 1)
        assert not timeout_state.continued.is_set()

    asyncio.run(timeout_scenario())

    cancellation_state = _blocking_state()
    cancellation_adapter = AsyncProviderAdapter(
        CooperativeBlockingProvider({"cancelled": cancellation_state})
    )

    async def cancellation_scenario() -> None:
        task = asyncio.create_task(
            cancellation_adapter.discover(_invocation_context("cancelled"))
        )
        assert await asyncio.to_thread(cancellation_state.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(cancellation_state.cancelled.wait, 1)
        assert not cancellation_state.continued.is_set()

    asyncio.run(cancellation_scenario())


def test_async_provider_calls_isolate_cooperative_cancellation_tokens() -> None:
    cancelled_state = _blocking_state()
    completed_state = _blocking_state()
    adapter = AsyncProviderAdapter(
        CooperativeBlockingProvider(
            {
                "cancelled": cancelled_state,
                "completed": completed_state,
            }
        )
    )

    async def scenario() -> None:
        cancelled_task = asyncio.create_task(
            adapter.discover(_invocation_context("cancelled"))
        )
        completed_task = asyncio.create_task(
            adapter.discover(_invocation_context("completed"))
        )
        assert await asyncio.to_thread(cancelled_state.started.wait, 1)
        assert await asyncio.to_thread(completed_state.started.wait, 1)
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task
        completed_state.release.set()
        result = await completed_task
        assert result.instances
        assert await asyncio.to_thread(cancelled_state.cancelled.wait, 1)
        assert not completed_state.cancelled.is_set()
        assert completed_state.continued.is_set()

    asyncio.run(scenario())


def test_async_provider_registry_wraps_sync_management_providers() -> None:
    provider = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/events",
            "tenant",
            "publish",
            health_method=None,
        )
    )
    adapter = AsyncProviderAdapter(provider)
    registry = AsyncProviderRegistry((adapter,))
    assert registry.get("webhook") is adapter
    with pytest.raises(KeyError, match="Unknown provider"):
        registry.get("missing")
    with pytest.raises(ValueError, match="unique"):
        AsyncProviderRegistry((adapter, adapter))
    discoveries = asyncio.run(
        registry.discover_all(_invocation_context("registry"))
    )
    assert discoveries["webhook"].instances


def test_environment_validates_provider_and_runtime_configuration() -> None:
    all_providers = _environment(
        FOUNDRY_PROJECT_ENDPOINT="https://foundry.test",
        RESEARCH_FOUNDRY_MODELS_PATH="/models",
        AZURE_SEARCH_ENDPOINT="https://search.test",
        AZURE_STORAGE_BLOB_ENDPOINT="https://storage.test",
        RESEARCH_GRAPH_ENDPOINT="https://graph.test/v1.0",
        RESEARCH_FUNCTIONS_ENDPOINT="https://functions.test",
        RESEARCH_FUNCTIONS_TOKEN_SCOPE="api://functions/.default",
        RESEARCH_FUNCTIONS_DISCOVERY_URL="https://functions.test/admin/functions",
        RESEARCH_MCP_ENDPOINT="https://mcp.test",
        RESEARCH_OPENAPI_ENDPOINT="https://openapi.test",
        RESEARCH_OPENAPI_DOCUMENT_URL="https://openapi.test/openapi.json",
        RESEARCH_WEBHOOK_ENDPOINT="https://webhook.test/events",
        RESEARCH_WEBHOOK_OPERATION_ID="publish",
    )
    environment = provider_environment_from_environment(all_providers)
    assert environment is not None
    assert len(environment.providers) == 8
    assert (
        provider_environment_from_environment(
            _environment(FOUNDRY_PROJECT_ENDPOINT="https://foundry.test")
        )
        is None
    )

    invalid_cases = (
        {"AZURE_SEARCH_ENDPOINT": "ftp://invalid"},
        {"RESEARCH_FUNCTIONS_ENDPOINT": "https://functions.test"},
        {"RESEARCH_OPENAPI_ENDPOINT": "https://openapi.test"},
        {"RESEARCH_WEBHOOK_ENDPOINT": "https://webhook.test"},
    )
    for invalid in invalid_cases:
        with pytest.raises(ValueError):
            provider_environment_from_environment(_environment(**invalid))
    with pytest.raises(ValueError, match="TENANT"):
        provider_environment_from_environment({"AZURE_SEARCH_ENDPOINT": "https://search.test"})
    with pytest.raises(ValueError, match="PROJECT"):
        build_provider_runtime(
            {
                "RESEARCH_WORKSPACE_TENANT_ID": "tenant",
                "AZURE_SEARCH_ENDPOINT": "https://search.test",
                PROVIDER_RELEASE_ID_ENV: "release",
            },
            credential_factory=FakeCredential,
        )
    with pytest.raises(ValueError, match="RELEASE"):
        build_provider_runtime(
            {
                "RESEARCH_WORKSPACE_TENANT_ID": "tenant",
                "RESEARCH_WORKSPACE_PROJECT_ID": "project",
                "AZURE_SEARCH_ENDPOINT": "https://search.test",
            },
            credential_factory=FakeCredential,
        )
    for value in ("invalid", "0", "301"):
        with pytest.raises(ValueError, match=PROVIDER_DEADLINE_SECONDS_ENV):
            build_provider_runtime(
                _environment(
                    AZURE_SEARCH_ENDPOINT="https://search.test",
                    **{PROVIDER_DEADLINE_SECONDS_ENV: value},
                ),
                credential_factory=FakeCredential,
            )
    runtime = build_provider_runtime(
        _environment(
            AZURE_SEARCH_ENDPOINT="https://search.test",
            **{PROVIDER_DEADLINE_SECONDS_ENV: "12.5"},
        ),
        credential_factory=FakeCredential,
        transport=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
    )
    _, runtime_context = runtime.service._provider_context("azure_ai_search", _request())
    assert runtime_context.remaining_seconds(provider_id="azure_ai_search") is not None
    assert (
        asyncio.run(runtime_context.consume_approval(_consumption_request())).status
        is ApprovalConsumptionStatus.UNAVAILABLE
    )
    with pytest.raises(ValueError, match="configured together"):
        build_provider_runtime(
            _environment(
                AZURE_SEARCH_ENDPOINT="https://search.test",
                **{APPROVAL_CONSUMPTION_URL_ENV: "https://approval.test/consume"},
            ),
            credential_factory=FakeCredential,
        )


def test_durable_approval_client_binds_exact_request_and_fails_closed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "consumed",
                "consumption_record_id": "record-1",
                "consumed_at": "2026-07-23T15:00:00Z",
            },
        )

    client = DurableApprovalConsumptionClient(
        "https://approval.test/consume",
        "api://approval/.default",
        FakeCredential(),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client(_consumption_request()))
    assert result.status is ApprovalConsumptionStatus.CONSUMED
    assert requests[0].headers["authorization"] == "Bearer provider-token"
    payload = json.loads(requests[0].content)
    assert payload["binding_id"] == "binding"
    assert payload["provider_contract_version"] == "research-assistant.integration-provider.v7"
    assert payload["descriptor_version"] == "1"
    assert payload["policy_id"] == "policy"
    assert payload["policy_version"] == "1.0.0"
    assert payload["policy_digest"] == policy_reference("policy").policy_digest
    assert "policy_ref" not in payload
    assert "credential" not in payload

    for response in (
        httpx.Response(503),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"status": "unknown"}),
    ):
        unavailable = DurableApprovalConsumptionClient(
            "https://approval.test/consume",
            "scope",
            FakeCredential(),
            transport=httpx.MockTransport(lambda _request, response=response: response),
        )
        assert (
            asyncio.run(unavailable(_consumption_request())).status
            is ApprovalConsumptionStatus.UNAVAILABLE
        )
    with pytest.raises(ValueError, match="token scope"):
        DurableApprovalConsumptionClient(
            "https://approval.test/consume",
            "",
            FakeCredential(),
        )

    class EmptyCredential:
        def get_token(self, *_scopes: str) -> Any:
            return SimpleNamespace(token="")

    empty_token = DurableApprovalConsumptionClient(
        "https://approval.test/consume",
        "scope",
        EmptyCredential(),
    )
    assert (
        asyncio.run(empty_token(_consumption_request())).status
        is ApprovalConsumptionStatus.UNAVAILABLE
    )


def test_runtime_default_credentials_and_configured_approval_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_assistant_connector_adapter.provider_runtime as runtime_module

    created: list[str | None] = []

    def _credential(client_id: str | None = None) -> FakeCredential:
        created.append(client_id)
        return FakeCredential()

    monkeypatch.setattr(runtime_module, "azure_credential", _credential)
    transport = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    build_provider_runtime(
        _environment(AZURE_SEARCH_ENDPOINT="https://search.test", AZURE_CLIENT_ID="client"),
        transport=transport,
    )
    build_provider_runtime(
        _environment(AZURE_SEARCH_ENDPOINT="https://search.test"),
        transport=transport,
    )
    assert created == ["client", None]

    approval_transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "status": "consumed",
                "consumption_record_id": "record",
                "consumed_at": "2026-07-23T15:00:00Z",
            },
        )
    )
    runtime = build_provider_runtime(
        _environment(
            AZURE_SEARCH_ENDPOINT="https://search.test",
            **{
                APPROVAL_CONSUMPTION_URL_ENV: "https://approval.test/consume",
                "RESEARCH_APPROVAL_CONSUMPTION_TOKEN_SCOPE": "scope",
            },
        ),
        credential_factory=FakeCredential,
        transport=transport,
        approval_transport=approval_transport,
    )
    _, invocation_context = runtime.service._provider_context(
        "azure_ai_search",
        _request(),
    )
    assert (
        asyncio.run(invocation_context.consume_approval(_consumption_request())).status
        is ApprovalConsumptionStatus.CONSUMED
    )
