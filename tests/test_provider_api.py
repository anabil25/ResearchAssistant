from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from research_assistant_connector_adapter.app import app
from research_assistant_connector_adapter.provider_api import (
    ProviderService,
    contract_app,
    provider_error_response,
)
from research_assistant_connectors.providers import (
    AuthMode,
    InvocationContext,
    ProviderRegistry,
    RateLimitError,
    WebhookConfig,
    WebhookProvider,
)


def context(handler: Any, *, approved: frozenset[str] = frozenset()) -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant",
        principal_id="principal",
        approved_instance_ids=approved,
        credential=None,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        correlation_id="correlation",
        trace_id="trace",
        sleep=lambda _: None,
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    original = app.state.provider_service
    app.state.gateway_validator = None
    provider = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/events",
            "tenant",
            "publish",
            health_method=None,
        )
    )
    base_context = context(
        lambda request: httpx.Response(
            202,
            json={
                "accepted": True,
                "idempotency_key": request.headers.get("Idempotency-Key"),
            },
        )
    )
    capability = provider.discover(base_context)[0]
    approved_context = replace(
        base_context,
        approved_instance_ids=frozenset({capability.instance_id}),
    )
    app.state.provider_service = ProviderService(
        ProviderRegistry((provider,)),
        lambda provider_id, _request: (
            approved_context if provider_id == "webhook" else base_context
        ),
    )
    with TestClient(app) as test_client:
        yield test_client
    app.state.provider_service = original


def test_provider_api_catalog_discovery_validation_health_and_invocation(
    client: TestClient,
) -> None:
    catalog = client.get("/v1/providers")
    discovery = client.get("/v1/providers/webhook/capabilities")
    validation = client.get("/v1/providers/webhook/validation")
    health = client.get("/v1/providers/webhook/health")
    descriptor = discovery.json()["descriptors"][0]
    instance = discovery.json()["instances"][0]
    invoked = client.post(
        "/v1/providers/webhook/invoke",
        headers={"Idempotency-Key": "event-1"},
        json={
            "instance_id": instance["instance_id"],
            "operation_id": "publish",
            "arguments": {"event": "updated"},
        },
    )

    assert catalog.json()["schema_version"] == "research-assistant.integration-provider.v2"
    assert catalog.json()["providers"][0]["provider_id"] == "webhook"
    assert instance["attachable_operation_ids"] == ["publish"]
    assert len(instance["instance_fingerprint"]) == 64
    assert descriptor["operations"][0]["maturity"] == "ga"
    assert descriptor["operations"][0]["version"] == "1.0.0"
    assert descriptor["operations"][0]["operation_class"] == "privileged"
    assert descriptor["operations"][0]["approval_policy"] == "required"
    assert descriptor["operations"][0]["side_effect_destinations"] == [
        "https://hooks.test/events"
    ]
    assert validation.json()["readiness"] == "ready"
    assert health.json()["readiness"] == "ready"
    assert invoked.status_code == 200
    assert invoked.json()["output"]["idempotency_key"] == "event-1"
    assert invoked.json()["audit_metadata"]["principal_id"] == "principal"


def test_provider_openapi_contract_is_separate_from_agent_tool_import() -> None:
    root = Path(__file__).resolve().parents[1]
    committed = json.loads(
        (root / "packages" / "contracts" / "provider-adapter-openapi.json").read_text(
            encoding="utf-8"
        )
    )
    assert committed == contract_app.openapi()
    operations = {
        operation["operationId"]
        for path_item in committed["paths"].values()
        for operation in path_item.values()
    }
    assert operations == {
        "discoverIntegrationCapabilities",
        "healthIntegrationProvider",
        "invokeIntegrationCapability",
        "listIntegrationProviders",
        "validateIntegrationProvider",
    }


def test_provider_api_rejects_unknown_runtime_and_model_supplied_approval(
    client: TestClient,
) -> None:
    unknown = client.get("/v1/providers/missing/capabilities")
    injected_approval = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": "webhook.publish",
            "operation_id": "publish",
            "arguments": {},
            "approved_instance_ids": ["webhook.publish"],
        },
    )
    original = app.state.provider_service
    app.state.provider_service = object()
    unavailable = client.get("/v1/providers")
    app.state.provider_service = original

    assert unknown.status_code == 404
    assert injected_approval.status_code == 422
    assert unavailable.status_code == 503


def test_provider_api_without_context_and_error_mapping() -> None:
    original = app.state.provider_service
    app.state.provider_service = ProviderService(ProviderRegistry((WebhookProvider(
        WebhookConfig("https://hooks.test", "tenant", "send")
    ),)))
    app.state.gateway_validator = None
    with TestClient(app) as client:
        response = client.get("/v1/providers/webhook/capabilities")
    app.state.provider_service = original

    assert response.status_code == 503
    error = provider_error_response(
        RateLimitError(
            "rate limited",
            provider_id="provider",
            retry_after=1.9,
        )
    )
    assert error.status_code == 429
    assert error.headers["retry-after"] == "1"
    assert b"rate limited" in error.body
    assert AuthMode.NONE.value == "none"
