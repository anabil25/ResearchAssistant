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
    approval_decision,
    capability_binding,
)


def context(handler: Any) -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant",
        principal_id="principal",
        project_id="project",
        credential=None,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        correlation_id="correlation",
        trace_id="trace",
        sleep=lambda _: None,
        consume_approval=lambda _: True,
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
    discovered = provider.discover(base_context)
    capability = discovered[0]
    descriptor = discovered.descriptor_for(capability)
    operation = descriptor.operations[0]
    binding = capability_binding(
        binding_id="binding",
        instance=capability,
        descriptor=descriptor,
        operation=operation,
        policy_ref=base_context.policy_release,
    )
    instance_decision = approval_decision(
        base_context,
        target=capability,
        instance=capability,
        descriptor=descriptor,
        operation=operation,
        arguments={"event": "updated"},
        decision_id="api-approval",
        expires_at="2999-01-01T00:00:00Z",
    )
    binding_decision = approval_decision(
        base_context,
        target=binding,
        instance=capability,
        descriptor=descriptor,
        operation=operation,
        arguments={"event": "updated"},
        decision_id="api-binding-approval",
        expires_at="2999-01-01T00:00:00Z",
    )
    approved_context = replace(
        base_context,
        approval_decisions=(instance_decision, binding_decision),
    )
    app.state.provider_service = ProviderService(
        ProviderRegistry((provider,)),
        lambda provider_id, _request: approved_context if provider_id == "webhook" else base_context,
        lambda _provider_id, _binding_id, _request: binding,
    )
    with TestClient(app) as test_client:
        yield test_client
    app.state.provider_service = original


def test_provider_api_catalog_discovery_validation_health_and_invocation(
    client: TestClient,
) -> None:
    catalog = client.get("/v1/providers")
    discovery = client.get("/v1/providers/webhook/capabilities")
    descriptor = discovery.json()["descriptors"][0]
    instance = discovery.json()["instances"][0]
    validation = client.get(f"/v1/providers/webhook/instances/{instance['instance_id']}/validation")
    health = client.get(f"/v1/providers/webhook/instances/{instance['instance_id']}/health")
    invoked = client.post(
        "/v1/providers/webhook/invoke",
        headers={"Idempotency-Key": "event-1"},
        json={
            "instance_id": instance["instance_id"],
            "operation_id": "publish",
            "arguments": {"event": "updated"},
        },
    )

    assert catalog.json()["schema_version"] == "research-assistant.integration-provider.v6"
    assert catalog.json()["providers"][0]["provider_id"] == "webhook"
    assert instance["bindability"] == [{"operation_id": "publish", "bindable": True, "reason_codes": []}]
    assert instance["config_fingerprint"]
    assert instance["scope"] == {"tenant_id": "tenant", "project_id": "project"}
    assert instance["provider_resource_id"] == "https://hooks.test"
    assert instance["auth_mode"] == "none"
    assert instance["connection_scopes"] == []
    assert instance["descriptor_ref"] == {
        "descriptor_id": descriptor["descriptor_id"],
        "descriptor_version": descriptor["descriptor_version"],
        "descriptor_digest": descriptor["descriptor_digest"],
    }
    assert len(instance["instance_fingerprint"]) == 64
    assert descriptor["operations"][0]["maturity"] == "ga"
    assert descriptor["operations"][0]["lifecycle"] == "active"
    assert descriptor["operations"][0]["version"] == "1.0.0"
    assert descriptor["operations"][0]["provider_version"] == "1.0.0"
    assert len(descriptor["operations"][0]["input_schema_digest"]) == 64
    assert len(descriptor["operations"][0]["output_schema_digest"]) == 64
    assert descriptor["operations"][0]["operation_class"] == "privileged"
    assert descriptor["operations"][0]["approval_policy"] == "required"
    assert descriptor["operations"][0]["side_effect_destinations"][0].startswith(
        "https://hooks.test#url-sha256="
    )
    assert validation.json()["readiness"] == "ready"
    assert health.json()["readiness"] == "ready"
    assert invoked.status_code == 200
    assert invoked.json()["output"]["idempotency_key"] == "event-1"
    assert invoked.json()["audit_metadata"]["principal_id"] == "principal"


def test_provider_api_targets_trusted_capability_binding(client: TestClient) -> None:
    validation = client.get(
        "/v1/providers/webhook/instances/webhook.publish/validation",
        params={"binding_id": "binding"},
    )
    health = client.get(
        "/v1/providers/webhook/instances/webhook.publish/health",
        params={"binding_id": "binding"},
    )
    invoked = client.post(
        "/v1/providers/webhook/invoke",
        headers={"Idempotency-Key": "binding-event-1"},
        json={
            "instance_id": "webhook.publish",
            "binding_id": "binding",
            "operation_id": "publish",
            "arguments": {"event": "updated"},
        },
    )

    assert validation.json()["binding_id"] == "binding"
    assert validation.json()["readiness"] == "ready"
    assert health.json()["binding_id"] == "binding"
    assert health.json()["readiness"] == "ready"
    assert invoked.status_code == 200
    assert invoked.json()["audit_metadata"]["binding_id"] == "binding"


def test_provider_openapi_contract_is_separate_from_agent_tool_import() -> None:
    root = Path(__file__).resolve().parents[1]
    committed = json.loads(
        (root / "packages" / "contracts" / "provider-adapter-openapi.json").read_text(encoding="utf-8")
    )
    assert committed == contract_app.openapi()
    operations = {
        operation["operationId"] for path_item in committed["paths"].values() for operation in path_item.values()
    }
    assert operations == {
        "discoverIntegrationCapabilities",
        "healthIntegrationCapabilityInstance",
        "invokeIntegrationCapability",
        "listIntegrationProviders",
        "validateIntegrationCapabilityInstance",
    }


def test_provider_api_rejects_unknown_runtime_and_model_supplied_approval(
    client: TestClient,
) -> None:
    unknown = client.get("/v1/providers/missing/capabilities")
    unknown_instance = client.get("/v1/providers/webhook/instances/missing/validation")
    mismatched_binding = client.get(
        "/v1/providers/webhook/instances/missing/validation",
        params={"binding_id": "binding"},
    )
    mismatched_operation = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": "webhook.publish",
            "binding_id": "binding",
            "operation_id": "other",
            "arguments": {},
        },
    )
    non_finite_arguments = client.post(
        "/v1/providers/webhook/invoke",
        content=('{"instance_id":"webhook.publish","operation_id":"publish","arguments":{"event":NaN}}'),
        headers={"content-type": "application/json"},
    )
    server_only_fields = {
        "approved_instance_ids": ["webhook.publish"],
        "approval_decisions": [],
        "context": {},
        "tenant_id": "tenant",
        "principal_id": "principal",
        "project_id": "project",
        "policy_release": "agent-studio-v1",
        "credential": {},
        "destinations": ["https://hooks.test"],
    }
    injected_context = [
        client.post(
            "/v1/providers/webhook/invoke",
            json={
                "instance_id": "webhook.publish",
                "operation_id": "publish",
                "arguments": {},
                field_name: value,
            },
        )
        for field_name, value in server_only_fields.items()
    ]
    original = app.state.provider_service
    app.state.provider_service = object()
    unavailable = client.get("/v1/providers")
    app.state.provider_service = original

    assert unknown.status_code == 404
    assert unknown_instance.status_code == 503
    assert unknown_instance.json()["error"]["code"] == "unavailable"
    assert mismatched_binding.status_code == 403
    assert mismatched_binding.json()["error"]["code"] == "policy"
    assert mismatched_operation.status_code == 422
    assert mismatched_operation.json()["error"]["code"] == "validation"
    assert non_finite_arguments.status_code == 422
    assert non_finite_arguments.json()["error"]["code"] == "validation"
    assert {response.status_code for response in injected_context} == {422}
    assert unavailable.status_code == 503


def test_provider_api_returns_sanitized_stale_binding_conflict(client: TestClient) -> None:
    old_provider = WebhookProvider(
        WebhookConfig("https://old.hooks.test/events", "tenant", "publish", health_method=None)
    )
    current_provider = WebhookProvider(
        WebhookConfig("https://current.hooks.test/events", "tenant", "publish", health_method=None)
    )
    request_context = context(lambda _: httpx.Response(200))
    old_discovery = old_provider.discover(request_context)
    old_instance = old_discovery[0]
    old_descriptor = old_discovery.descriptor_for(old_instance)
    stale_binding = capability_binding(
        binding_id="stale-binding",
        instance=old_instance,
        descriptor=old_descriptor,
        operation=old_descriptor.operations[0],
        policy_ref=request_context.policy_release,
    )
    original = app.state.provider_service
    app.state.provider_service = ProviderService(
        ProviderRegistry((current_provider,)),
        lambda _provider_id, _request: request_context,
        lambda _provider_id, _binding_id, _request: stale_binding,
    )
    try:
        response = client.get(
            "/v1/providers/webhook/instances/webhook.publish/validation",
            params={"binding_id": "stale-binding"},
        )
    finally:
        app.state.provider_service = original

    payload = response.json()["error"]
    assert response.status_code == 409
    assert payload["code"] == "stale_binding"
    assert payload["action"] == "rebind_and_review"
    assert payload["old_fingerprint"] == stale_binding.instance_fingerprint
    assert len(payload["new_fingerprint"]) == 64
    assert payload["changed_categories"] == [
        "descriptor",
        "operations",
        "instance",
        "destinations",
        "configuration",
    ]
    assert "old.hooks.test" not in json.dumps(payload)


def test_provider_api_without_context_and_error_mapping() -> None:
    original = app.state.provider_service
    app.state.provider_service = ProviderService(
        ProviderRegistry((WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send")),))
    )
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


def test_provider_api_requires_server_side_binding_resolver() -> None:
    original = app.state.provider_service
    provider = WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send"))
    app.state.provider_service = ProviderService(
        ProviderRegistry((provider,)),
        lambda _provider_id, _request: context(lambda _: httpx.Response(200)),
    )
    app.state.gateway_validator = None
    with TestClient(app) as client:
        response = client.get(
            "/v1/providers/webhook/instances/webhook.send/validation",
            params={"binding_id": "binding"},
        )
    app.state.provider_service = original

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
