from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from research_assistant_connector_adapter.app import app
from research_assistant_connector_adapter.provider_api import (
    CAPABILITY_BINDING_SCHEMA_ID,
    CapabilityBinding,
    ProviderService,
    contract_app,
    provider_error_response,
)
from research_assistant_connectors.providers import (
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    ApprovalConsumptionStatus,
    AsyncProviderAdapter,
    AsyncProviderRegistry,
    AuthMode,
    InvocationContext,
    RateLimitError,
    WebhookConfig,
    WebhookProvider,
    approval_decision,
    capability_binding,
    capability_binding_payload,
)

Draft202012Validator: Any = import_module("jsonschema").Draft202012Validator


def async_registry(*providers: Any) -> AsyncProviderRegistry:
    return AsyncProviderRegistry(
        tuple(AsyncProviderAdapter(provider) for provider in providers)
    )


async def consume_approval(
    _request: ApprovalConsumptionRequest,
) -> ApprovalConsumptionResult:
    return ApprovalConsumptionResult(
        ApprovalConsumptionStatus.CONSUMED,
        "consumption-record",
        "2026-07-23T14:00:00Z",
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
        release_id="release-1",
        invocation_id="invocation-1",
        consume_approval=consume_approval,
        logical_agent_id="agent-1",
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
        logical_agent_id="agent-1",
        instance=capability,
        descriptor=descriptor,
        operation=operation,
        policy_ref=base_context.policy_ref,
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
        async_registry(provider),
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
    instance_id = instance["instance_id"]
    validation = client.get(f"/v1/providers/webhook/instances/{instance_id}/validation")
    health = client.get(f"/v1/providers/webhook/instances/{instance_id}/health")
    invoked = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": instance_id,
            "operation_id": "publish",
            "arguments": {"event": "updated"},
            "idempotency_key": "event-1",
        },
    )

    assert catalog.json()["provider_contract_version"] == "research-assistant.integration-provider.v7"
    assert catalog.json()["canonicalization_version"] == "research-assistant.canonical-json.v1"
    assert catalog.json()["providers"][0]["provider_id"] == "webhook"
    assert catalog.json()["warnings"] == []
    assert instance["bindability"] == [{"operation_id": "publish", "bindable": True, "reason_codes": []}]
    assert instance["tenant_id"] == "tenant"
    assert instance["project_id"] == "project"
    assert instance["descriptor_id"] == descriptor["descriptor_id"]
    assert instance["descriptor_version"] == descriptor["descriptor_version"]
    assert instance["descriptor_digest"] == descriptor["descriptor_digest"]
    assert instance["provider_id"] == "webhook"
    assert len(instance["instance_fingerprint"]) == 64
    assert instance["connection_ref"] is None
    assert instance["auth_mode"] == "none"
    assert len(instance["connection_authorization_digest"]) == 64
    assert len(instance["config_hash"]) == 64
    assert "configuration_fingerprint" not in instance
    assert descriptor["operations"][0]["maturity"] == "ga"
    assert descriptor["operations"][0]["lifecycle"] == "active"
    operation_wire = descriptor["operations"][0]
    assert operation_wire["operation_version"] == "1.0.0"
    assert len(operation_wire["input_schema_digest"]) == 64
    assert len(operation_wire["output_schema_digest"]) == 64
    assert "provider_version" not in descriptor["operations"][0]
    assert descriptor["operations"][0]["operation_class"] == "privileged"
    assert descriptor["operations"][0]["approval_policy"] == "required"
    assert descriptor["operations"][0]["side_effect_destinations"][0].startswith(
        "https://hooks.test/events#url-sha256="
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
        json={
            "instance_id": "webhook.publish",
            "binding_id": "binding",
            "operation_id": "publish",
            "arguments": {"event": "updated"},
            "idempotency_key": "binding-event-1",
        },
    )

    assert validation.json()["binding_id"] == "binding"
    assert validation.json()["readiness"] == "ready"
    assert health.json()["binding_id"] == "binding"
    assert health.json()["readiness"] == "ready"
    assert invoked.status_code == 200
    assert invoked.json()["audit_metadata"]["binding_id"] == "binding"


def test_provider_openapi_contract_is_separate_from_agent_tool_import(
    client: TestClient,
) -> None:
    root = Path(__file__).resolve().parents[1]
    legacy_path = root / "packages" / "contracts" / "provider-adapter-openapi.json"
    legacy_bytes = legacy_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    assert hashlib.sha256(legacy_bytes).hexdigest() == (
        "354716da381fbb0d71ee58fbfccbc737066debaf238403964f28112898cdb24c"
    )
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["info"]["version"] == "1.0.0"
    committed = json.loads(
        (root / "packages" / "contracts" / "provider-adapter-openapi.v7.json").read_text(encoding="utf-8")
    )
    generated = contract_app.openapi()
    assert committed == generated
    assert contract_app.openapi() is generated
    assert committed["info"]["version"] == "research-assistant.integration-provider.v7"
    binding_schema = committed["components"]["schemas"]["CapabilityBinding"]
    assert binding_schema["$id"] == CAPABILITY_BINDING_SCHEMA_ID
    v7_payload: dict[str, Any] = {
        "binding_id": "binding",
        "provider_contract_version": "research-assistant.integration-provider.v7",
        "canonicalization_version": "research-assistant.canonical-json.v1",
        "provider_id": "provider",
        "descriptor_id": "descriptor",
        "descriptor_version": "1",
        "descriptor_digest": "0" * 64,
        "operation_id": "operation",
        "operation_version": "1",
        "tenant_id": "tenant",
        "project_id": "project",
        "instance_id": "instance",
        "discovered_provider_version": "1",
        "discovered_resource_version": None,
        "instance_fingerprint": "1" * 64,
        "input_schema_digest": "2" * 64,
        "output_schema_digest": "3" * 64,
        "configuration_id": None,
        "configuration_digest": "4" * 64,
        "connection_id": None,
        "connection_auth_mode": None,
        "connection_authorization_digest": None,
        "policy_id": "policy",
        "policy_version": "1",
        "policy_digest": "5" * 64,
        "allowed_destination_constraints": [],
        "allowed_destinations_digest": "6" * 64,
    }
    assert CapabilityBinding.model_validate(v7_payload).model_dump(mode="json") == v7_payload
    artifact_binding_schema = {
        "$ref": "#/components/schemas/CapabilityBinding",
        "components": committed["components"],
    }
    Draft202012Validator(artifact_binding_schema).validate(v7_payload)
    assert set(binding_schema["properties"]) == set(v7_payload)
    assert set(binding_schema["required"]) == set(binding_schema["properties"])
    assert binding_schema["additionalProperties"] is False
    for forbidden in (
        "agent_id",
        "logical_agent_id",
        "provider_version",
        "pinned_provider_version",
        "instance_ref",
        "config_ref",
        "config",
        "config_hash",
        "configuration_ref",
        "operations_digest",
        "provider_resource_id",
        "connection_ref",
        "connection_version",
        "connection_identity_mode",
        "connection_scopes",
        "connection_roles",
        "auth_mode",
        "descriptor_ref",
        "operation_ref",
        "policy_ref",
    ):
        assert forbidden not in binding_schema["properties"]
    with pytest.raises(ValueError):
        CapabilityBinding.model_validate(
            {
                **v7_payload,
                "provider_contract_version": "research-assistant.integration-provider.v6",
            }
        )
    with pytest.raises(ValueError):
        CapabilityBinding.model_validate({**v7_payload, "logical_agent_id": "agent"})
    provider = WebhookProvider(
        WebhookConfig("https://wire.test/events", "tenant", "publish", health_method=None)
    )
    request_context = context(lambda _: httpx.Response(202, json={"accepted": True}))
    discovered = provider.discover(request_context)
    instance = discovered[0]
    descriptor = discovered.descriptor_for(instance)
    runtime_binding = capability_binding(
        binding_id="wire-binding",
        logical_agent_id="persisted-agent-only",
        instance=instance,
        descriptor=descriptor,
        operation=descriptor.operations[0],
        policy_ref=request_context.policy_ref,
    )
    serialized_binding = capability_binding_payload(runtime_binding)
    assert CapabilityBinding.model_validate(serialized_binding).model_dump(mode="json") == serialized_binding
    assert "logical_agent_id" not in serialized_binding
    assert serialized_binding["configuration_id"] is None
    assert "config" not in serialized_binding
    assert "config_hash" not in serialized_binding
    assert serialized_binding["connection_id"] is None
    assert serialized_binding["connection_auth_mode"] is None
    assert serialized_binding["connection_authorization_digest"] is None
    assert not {
        "CapabilityBindingV4",
        "DescriptorReference",
        "OperationReference",
        "InstanceReference",
        "ConfigurationReference",
        "ConnectionReference",
        "PolicyReference",
        "DestinationConstraints",
    }.intersection(committed["components"]["schemas"])
    governed_schemas = {
        "BindabilityDecisionResponse",
        "CapabilityBinding",
        "CapabilityDescriptorResponse",
        "CapabilityInstanceResponse",
        "DiscoveryResultResponse",
        "DiscoveryWarningResponse",
        "HealthResultResponse",
        "HttpErrorResponse",
        "OperationDescriptorResponse",
        "ProvenanceResponse",
        "ProviderCatalogResponse",
        "ProviderDescriptorResponse",
        "ProviderErrorDetailResponse",
        "ProviderErrorEnvelopeResponse",
        "ProviderInvokePayload",
        "ProviderInvokeResultResponse",
        "RequestValidationErrorResponse",
        "RequestValidationIssueResponse",
        "ValidationResultResponse",
    }
    assert governed_schemas == set(committed["components"]["schemas"])
    for schema_name in governed_schemas:
        assert committed["components"]["schemas"][schema_name]["additionalProperties"] is False
    for schema_name in (
        "CapabilityBinding",
        "HealthResultResponse",
        "ProviderErrorEnvelopeResponse",
        "ProviderInvokePayload",
        "ProviderInvokeResultResponse",
        "ValidationResultResponse",
    ):
        schema = committed["components"]["schemas"][schema_name]
        assert schema["examples"]
        artifact_schema = {
            "$ref": f"#/components/schemas/{schema_name}",
            "components": committed["components"],
        }
        validator = Draft202012Validator(artifact_schema)
        for example in schema["examples"]:
            validator.validate(example)
    assert {
        "allowed_destination_constraints",
        "allowed_destinations_digest",
        "binding_id",
        "canonicalization_version",
        "configuration_digest",
        "configuration_id",
        "connection_auth_mode",
        "connection_authorization_digest",
        "connection_id",
    }.issubset(binding_schema["required"])
    successful_response_schemas = {
        "listIntegrationProviders": "ProviderCatalogResponse",
        "discoverIntegrationCapabilities": "DiscoveryResultResponse",
        "validateIntegrationCapabilityInstance": "ValidationResultResponse",
        "healthIntegrationCapabilityInstance": "HealthResultResponse",
        "invokeIntegrationCapability": "ProviderInvokeResultResponse",
    }
    operations_by_id = {
        operation["operationId"]: operation
        for path_item in committed["paths"].values()
        for operation in path_item.values()
    }
    for operation_id, schema_name in successful_response_schemas.items():
        response_schema = operations_by_id[operation_id]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {"$ref": f"#/components/schemas/{schema_name}"}
        for status in ("401", "403", "409", "422", "429", "502", "503", "504"):
            error_schema = operations_by_id[operation_id]["responses"][status]["content"][
                "application/json"
            ]["schema"]
            assert "additionalProperties" not in error_schema
            for reference in (
                [error_schema]
                if "$ref" in error_schema
                else error_schema["anyOf"]
            ):
                referenced = reference["$ref"].rsplit("/", 1)[-1]
                assert (
                    committed["components"]["schemas"][referenced][
                        "additionalProperties"
                    ]
                    is False
                )
    discovery_payload = client.get("/v1/providers/webhook/capabilities").json()
    instance_id = discovery_payload["instances"][0]["instance_id"]
    successful_payloads = {
        "ProviderCatalogResponse": client.get("/v1/providers").json(),
        "DiscoveryResultResponse": discovery_payload,
        "ValidationResultResponse": client.get(
            f"/v1/providers/webhook/instances/{instance_id}/validation"
        ).json(),
        "HealthResultResponse": client.get(
            f"/v1/providers/webhook/instances/{instance_id}/health"
        ).json(),
        "ProviderInvokeResultResponse": client.post(
            "/v1/providers/webhook/invoke",
            json={
                "instance_id": instance_id,
                "operation_id": "publish",
                "arguments": {"event": "updated"},
                "idempotency_key": "artifact-roundtrip",
            },
        ).json(),
    }
    for schema_name, payload in successful_payloads.items():
        schema = committed["components"]["schemas"][schema_name]
        assert set(payload) == set(schema["properties"])
        Draft202012Validator(
            {
                "$ref": f"#/components/schemas/{schema_name}",
                "components": committed["components"],
            }
        ).validate(payload)
    provider_error = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": instance_id,
            "operation_id": "publish",
            "arguments": {"event": "updated"},
        },
    ).json()
    Draft202012Validator(
        {
            "$ref": "#/components/schemas/ProviderErrorEnvelopeResponse",
            "components": committed["components"],
        }
    ).validate(provider_error)
    request_validation_error = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": instance_id,
            "operation_id": "publish",
            "arguments": {"event": "updated"},
            "idempotency_key": "bad key",
        },
    ).json()
    Draft202012Validator(
        {
            "$ref": "#/components/schemas/RequestValidationErrorResponse",
            "components": committed["components"],
        }
    ).validate(request_validation_error)
    not_found_error = client.get("/v1/providers/missing/capabilities").json()
    Draft202012Validator(
        {
            "$ref": "#/components/schemas/HttpErrorResponse",
            "components": committed["components"],
        }
    ).validate(not_found_error)
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
    missing_idempotency = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": "webhook.publish",
            "operation_id": "publish",
            "arguments": {"event": "updated"},
        },
    )
    malformed_idempotency = client.post(
        "/v1/providers/webhook/invoke",
        json={
            "instance_id": "webhook.publish",
            "operation_id": "publish",
            "arguments": {"event": "updated"},
            "idempotency_key": "bad key",
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
    assert missing_idempotency.status_code == 403
    assert malformed_idempotency.status_code == 422
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
        logical_agent_id="agent-1",
        instance=old_instance,
        descriptor=old_descriptor,
        operation=old_descriptor.operations[0],
        policy_ref=request_context.policy_ref,
    )
    original = app.state.provider_service
    app.state.provider_service = ProviderService(
        async_registry(current_provider),
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
    assert payload["old_fingerprint"] == stale_binding.instance_ref.instance_fingerprint
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
        async_registry(
            WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send"))
        )
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
        async_registry(provider),
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
