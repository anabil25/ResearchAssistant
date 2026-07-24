# mypy: disable-error-code=import-untyped

from __future__ import annotations

import asyncio
from time import time
from types import TracebackType
from typing import Any, Self

import httpx
import pytest
from azure.core.credentials import AccessToken
from research_assistant_api.agent_studio.capability_discovery import (
    EXPECTED_CANONICALIZATION_VERSION,
    EXPECTED_PROVIDER_CONTRACT_VERSION,
    CapabilityDiscoveryRequest,
    CapabilityProviderProtocolError,
    HttpCapabilityDiscoverySource,
    NullCapabilityDiscoverySource,
    build_capability_discovery_source,
    discover_with_timeout,
)
from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry
from research_assistant_api.agent_studio.models import HealthStatus, InstanceReadiness, OperationClass
from research_assistant_api.agent_studio.schema_ref_resolver import compute_schema_digest
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.closed = False

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: object,
    ) -> AccessToken:
        del claims, tenant_id, enable_cae, kwargs
        self.scopes.extend(scopes)
        return AccessToken("test-token", int(time()) + 3600)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        del exc_type, exc_value, traceback


def _request(*, timeout_seconds: float = 5.0) -> CapabilityDiscoveryRequest:
    return CapabilityDiscoveryRequest(
        scope=ScopeContext(tenant_id="tenant-1", project_id="project-1"),
        principal="user-1",
        correlation_id="correlation-1",
        timeout_seconds=timeout_seconds,
    )


def _operation_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operation_id": "search",
        "operation_version": "1",
        "input_schema_digest": "a" * 64,
        "output_schema_digest": "b" * 64,
        "maturity": "ga",
        "lifecycle": "active",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "operation_class": "read",
        "approval_policy": "never",
        "external_side_effect": False,
        "side_effect_destinations": [],
        "timeout_seconds": 30,
        "max_retries": 1,
        "idempotency": "none",
        "least_privilege_scopes": [],
        "least_privilege_roles": [],
        "docs": ["https://example.com/docs"],
        "audit_events": [],
    }
    payload.update(overrides)
    return payload


def _descriptor_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "descriptor_id": "file_search",
        "descriptor_version": "1",
        "descriptor_digest": "c" * 64,
        "family": "microsoft_foundry",
        "resource_kind": "search_index",
        "name": "File Search",
        "auth_modes": ["managed_identity"],
        "operations": [_operation_payload()],
        "provenance": [],
        "observability": [],
        "audit": [],
        "metadata": {},
    }
    payload.update(overrides)
    return payload


def _instance_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "provider_id": "foundry",
        "instance_id": "instance-1",
        "descriptor_id": "file_search",
        "descriptor_version": "1",
        "descriptor_digest": "c" * 64,
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "provider_resource_id": "res-1",
        "discovered_provider_version": "2024-01-01",
        "discovered_resource_version": "1",
        "name": "File Search Instance",
        "readiness": "ready",
        "health": "ready",
        "last_checked_at": "2024-01-01T00:00:00+00:00",
        "configuration": {},
        "config_hash": "d" * 64,
        "connection_ref": None,
        "connection_version": "1",
        "auth_mode": "managed_identity",
        "connection_identity_mode": "managed_identity",
        "connection_scopes": [],
        "connection_roles": [],
        "connection_authorization_digest": "e" * 64,
        "instance_fingerprint": "f" * 64,
        "bindability": [],
        "config_validated": True,
        "allowed_destination_constraints": [],
        "allowed_destinations_digest": "0" * 64,
        "status_evidence": [],
    }
    payload.update(overrides)
    return payload


def _catalog_payload(provider_ids: list[str], *, warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "canonicalization_version": EXPECTED_CANONICALIZATION_VERSION,
        "providers": [
            {
                "provider_id": provider_id,
                "family": "microsoft_foundry",
                "name": provider_id,
                "description": f"{provider_id} provider",
                "auth_modes": ["managed_identity"],
                "provenance": [],
                "capability_descriptors": [],
            }
            for provider_id in provider_ids
        ],
        "warnings": warnings or [],
    }


def _capabilities_payload(
    provider_id: str,
    *,
    descriptors: list[dict[str, Any]] | None = None,
    instances: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "canonicalization_version": EXPECTED_CANONICALIZATION_VERSION,
        "provider_id": provider_id,
        "descriptors": descriptors if descriptors is not None else [_descriptor_payload()],
        "instances": instances if instances is not None else [_instance_payload(provider_id=provider_id)],
        "warnings": warnings or [],
        "refreshed_at": "2024-01-01T00:00:00+00:00",
    }


def _source(handler: Any, *, credential: FakeCredential | None = None, token_scope: str | None = None) -> tuple[
    HttpCapabilityDiscoverySource, httpx.AsyncClient
]:
    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    source = HttpCapabilityDiscoverySource(
        "https://provider.example", credential=credential, token_scope=token_scope, client=client
    )
    return source, client


def _bounded_source(handler: Any, **bounds: Any) -> tuple[HttpCapabilityDiscoverySource, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    source = HttpCapabilityDiscoverySource("https://provider.example", client=client, **bounds)
    return source, client


@pytest.mark.asyncio
async def test_discover_maps_single_provider_descriptor_and_instance() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        assert request.url.path == "/v1/providers/foundry/capabilities"
        return httpx.Response(200, json=_capabilities_payload("foundry"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.warnings == ()
    assert len(result.descriptors) == 1
    descriptor = result.descriptors[0]
    assert descriptor.id == "foundry:file_search"
    assert descriptor.provider == "foundry"
    assert descriptor.title == "File Search"
    assert descriptor.description == "File Search (microsoft_foundry/search_index)"
    assert descriptor.managed_foundry_native is True
    assert descriptor.risk_tier == "low"
    assert descriptor.data_boundary == "project"
    assert descriptor.config_schema == {}
    assert len(descriptor.operations) == 1
    operation = descriptor.operations[0]
    assert operation.name == "search"
    assert operation.maturity.value == "ga"
    assert operation.lifecycle.value == "active"
    assert operation.operation_class == OperationClass.READ
    assert operation.risk == "low"
    assert operation.requires_approval is False
    assert operation.approval_policy_ref is None
    assert operation.idempotent is False
    assert operation.source_url == "https://example.com/docs"
    # Backend operation schema digests are this backend's own canonical digest
    # of the wire schema objects (separately named from the provider's own
    # RFC-8785 digests), never the provider's prefixed value.
    assert operation.input_schema_digest == compute_schema_digest({"type": "object"})
    assert operation.output_schema_digest == compute_schema_digest({"type": "object"})

    assert len(result.instances) == 1
    instance = result.instances[0]
    assert instance.id == "foundry:instance-1"
    assert instance.descriptor_id == "foundry:file_search"
    assert instance.tenant_id == "tenant-1"
    assert instance.project_id == "project-1"
    assert instance.readiness == InstanceReadiness.READY
    assert instance.health_status == HealthStatus.HEALTHY
    assert instance.unavailable_reason is None
    assert instance.registered_by == "user-1"
    # Backend authoritative digests are recomputed by the registry, so the
    # adapter deliberately leaves them unset on the domain object.
    assert instance.descriptor_digest is None
    assert instance.instance_fingerprint is None

    # The provider's own wire pins are preserved verbatim, separately named,
    # alongside (never in place of) the backend digests above.
    assert len(result.descriptor_pins) == 1
    descriptor_pin = result.descriptor_pins[0]
    assert descriptor_pin.provider_id == "foundry"
    assert descriptor_pin.descriptor_backend_id == "foundry:file_search"
    assert descriptor_pin.descriptor_id == "file_search"
    assert descriptor_pin.descriptor_version == "1"
    assert descriptor_pin.descriptor_digest == "c" * 64
    assert len(descriptor_pin.operations) == 1
    operation_pin = descriptor_pin.operations[0]
    assert operation_pin.operation_id == "search"
    assert operation_pin.operation_version == "1"
    assert operation_pin.idempotency == "none"
    assert operation_pin.approval_policy == "never"
    assert operation_pin.input_schema_digest == "a" * 64
    assert operation_pin.output_schema_digest == "b" * 64

    assert len(result.instance_pins) == 1
    instance_pin = result.instance_pins[0]
    assert instance_pin.provider_id == "foundry"
    assert instance_pin.instance_backend_id == "foundry:instance-1"
    assert instance_pin.instance_id == "instance-1"
    assert instance_pin.provider_resource_id == "res-1"
    assert instance_pin.config_hash == "d" * 64
    assert instance_pin.instance_fingerprint == "f" * 64
    assert instance_pin.descriptor_digest == "c" * 64
    assert instance_pin.connection_authorization_digest == "e" * 64
    assert instance_pin.allowed_destinations_digest == "0" * 64

    # Refresh interface metadata is populated on a successful pass.
    assert result.refresh_metadata is not None
    assert result.refresh_metadata.provider_contract_version == EXPECTED_PROVIDER_CONTRACT_VERSION
    assert result.refresh_metadata.canonicalization_version == EXPECTED_CANONICALIZATION_VERSION
    assert result.refresh_metadata.provider_ids == ("foundry",)


@pytest.mark.asyncio
async def test_discover_derives_high_risk_from_privileged_operation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(
            operations=[
                _operation_payload(operation_id="read_op", operation_class="read"),
                _operation_payload(
                    operation_id="danger_op", operation_class="privileged", approval_policy="required"
                ),
            ]
        )
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors[0].risk_tier == "high"
    ops_by_name = {op.name: op for op in result.descriptors[0].operations}
    assert ops_by_name["danger_op"].risk == "high"
    assert ops_by_name["danger_op"].requires_approval is True
    assert ops_by_name["danger_op"].approval_policy_ref == "required"
    assert ops_by_name["read_op"].risk == "low"
    assert ops_by_name["read_op"].requires_approval is False


@pytest.mark.asyncio
async def test_discover_derives_medium_risk_from_write_reversible_operation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(
            operations=[_operation_payload(operation_class="write_reversible", approval_policy="required")]
        )
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    op = result.descriptors[0].operations[0]
    assert result.descriptors[0].risk_tier == "medium"
    assert op.risk == "medium"
    assert op.requires_approval is True
    assert op.approval_policy_ref == "required"
    assert op.idempotent is False


@pytest.mark.asyncio
async def test_discover_marks_non_foundry_family_as_not_managed_foundry_native() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["custom"]))
        descriptor = _descriptor_payload(family="custom_connector")
        return httpx.Response(200, json=_capabilities_payload("custom", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors[0].managed_foundry_native is False


@pytest.mark.asyncio
async def test_discover_maps_idempotent_operation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(idempotency="provider_native")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors[0].operations[0].idempotent is True


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_request_fails() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert result.unavailable_reason is not None
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_is_not_json_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=["not", "an", "object"])

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "JSON object" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_reports_unavailable_on_catalog_contract_version_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload(["foundry"])
        payload["provider_contract_version"] = "research-assistant.integration-provider.v5"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "provider_contract_version" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_reports_unavailable_on_catalog_canonicalization_version_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload(["foundry"])
        payload["canonicalization_version"] = "research-assistant.canonical-json.v2"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "canonicalization_version" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_honest_empty_success_when_catalog_has_no_providers() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_catalog_payload([]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_surfaces_catalog_level_warnings() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload(
            [], warnings=[{"reason_code": "x", "message": "catalog warning", "provider_id": "p"}]
        )
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert "catalog warning" in result.warnings


@pytest.mark.asyncio
async def test_discover_degrades_one_failing_provider_to_a_warning() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["good", "bad"]))
        if request.url.path == "/v1/providers/bad/capabilities":
            return httpx.Response(500)
        return httpx.Response(200, json=_capabilities_payload("good"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert len(result.descriptors) == 1
    assert result.descriptors[0].provider == "good"
    assert any("bad" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_rejects_provider_capabilities_response_not_json_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        return httpx.Response(200, json=["nope"])

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("JSON object" in warning for warning in result.warnings)
    assert result.descriptors == ()


@pytest.mark.asyncio
async def test_discover_rejects_provider_capabilities_contract_version_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry")
        payload["provider_contract_version"] = "wrong"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("provider_contract_version" in warning for warning in result.warnings)
    assert result.descriptors == ()


@pytest.mark.asyncio
async def test_discover_rejects_provider_capabilities_canonicalization_version_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry")
        payload["canonicalization_version"] = "wrong"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("canonicalization_version" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_rejects_provider_capabilities_provider_id_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("someone-else")
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("provider_id mismatch" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_surfaces_per_provider_discovery_warnings() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload(
            "foundry", warnings=[{"reason_code": "x", "message": "discovery warning", "provider_id": "foundry"}]
        )
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert "discovery warning" in result.warnings


@pytest.mark.asyncio
async def test_discover_skips_descriptor_with_unrecognized_maturity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(maturity="bogus")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_descriptor_with_no_operations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_operation_with_unrecognized_approval_policy() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(approval_policy="bogus")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_operation_with_unrecognized_idempotency() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(idempotency="bogus")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_operation_with_unrecognized_lifecycle() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(lifecycle="bogus")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()


@pytest.mark.asyncio
async def test_discover_skips_operation_with_unrecognized_operation_class() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(operation_class="bogus")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()


@pytest.mark.asyncio
async def test_discover_skips_operation_with_malformed_digest() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(input_schema_digest="NOT-HEX")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_treats_out_of_range_timeout_as_none_not_fabricated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(timeout_seconds=999999)])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors[0].operations[0].timeout_seconds is None


@pytest.mark.asyncio
async def test_discover_skips_instance_with_unrecognized_readiness() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(readiness="bogus")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_with_unrecognized_health() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(health="bogus")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_skips_instance_with_invalid_timestamp() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(last_checked_at="not-a-timestamp")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_derives_unavailable_reason_from_status_evidence_when_not_ready() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(
            readiness="unauthorized", health="unauthorized", status_evidence=["missing role assignment"]
        )
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    instance = result.instances[0]
    assert instance.readiness == InstanceReadiness.UNAUTHORIZED
    assert instance.health_status == HealthStatus.UNHEALTHY
    assert instance.unavailable_reason == "missing role assignment"


@pytest.mark.asyncio
async def test_discover_derives_generic_unavailable_reason_when_no_evidence_given() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(readiness="degraded", health="degraded", status_evidence=[])
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    instance = result.instances[0]
    assert instance.readiness == InstanceReadiness.DEGRADED
    assert instance.health_status == HealthStatus.DEGRADED
    assert instance.unavailable_reason == "Provider reported readiness=degraded."


@pytest.mark.asyncio
async def test_discover_skips_instance_referencing_a_skipped_descriptor() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        bad_descriptor = _descriptor_payload(descriptor_id="broken", operations=[])
        instance = _instance_payload(descriptor_id="broken", instance_id="orphan")
        return httpx.Response(
            200, json=_capabilities_payload("foundry", descriptors=[bad_descriptor], instances=[instance])
        )

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert result.instances == ()
    assert any("references descriptor" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_collapses_duplicate_provider_ids_in_catalog_to_a_warning() -> None:
    """A catalog listing the same ``provider_id`` twice (a malformed catalog)
    must not crash -- the duplicate is dropped before fan-out (so the provider
    is not requested twice) and recorded as a warning."""

    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/v1/providers":
            payload = _catalog_payload(["foundry"])
            payload["providers"] = payload["providers"] * 2
            return httpx.Response(200, json=payload)
        calls += 1
        return httpx.Response(200, json=_capabilities_payload("foundry"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert len(result.descriptors) == 1
    assert len(result.instances) == 1
    assert calls == 1  # the duplicate was never fanned out
    assert any("more than once" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_uses_managed_identity_bearer_token_when_configured() -> None:
    seen_auth: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload([]))
        return httpx.Response(200, json=_capabilities_payload("foundry"))

    credential = FakeCredential()
    source, client = _source(handler, credential=credential, token_scope="https://management.azure.com/.default")
    await source.discover(_request())
    await source.close()
    await client.aclose()

    assert seen_auth == ["Bearer test-token"]
    assert credential.scopes == ["https://management.azure.com/.default"]
    assert credential.closed is True


@pytest.mark.asyncio
async def test_discover_sends_no_authorization_header_when_not_configured() -> None:
    seen_auth: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json=_catalog_payload([]))

    source, client = _source(handler)
    await source.discover(_request())
    await client.aclose()

    assert seen_auth == [""]


@pytest.mark.asyncio
async def test_discover_preserves_absent_operation_digest_pin_as_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        operation = _operation_payload()
        operation["input_schema_digest"] = None
        descriptor = _descriptor_payload(operations=[operation])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    # The backend digest is still computed from the wire input_schema object;
    # an absent provider digest does not blank the backend's own digest.
    assert result.descriptors[0].operations[0].input_schema_digest == compute_schema_digest({"type": "object"})
    # The raw provider pin honestly records the absent digest as None (an
    # absent value, never a dropped-present value).
    assert result.descriptor_pins[0].operations[0].input_schema_digest is None
    assert result.descriptor_pins[0].operations[0].output_schema_digest == "b" * 64


@pytest.mark.asyncio
async def test_close_closes_internally_owned_client_and_credential() -> None:
    credential = FakeCredential()
    source = HttpCapabilityDiscoverySource(
        "https://provider.example",
        credential=credential,
        token_scope="https://management.azure.com/.default",
    )

    await source.close()

    assert credential.closed is True


@pytest.mark.asyncio
async def test_close_closes_internally_owned_client_without_credential() -> None:
    source = HttpCapabilityDiscoverySource("https://provider.example")

    await source.close()


def test_build_capability_discovery_source_returns_null_when_unconfigured() -> None:
    source = build_capability_discovery_source(Settings())

    assert isinstance(source, NullCapabilityDiscoverySource)


def test_build_capability_discovery_source_returns_http_adapter_when_configured() -> None:
    source = build_capability_discovery_source(
        Settings(
            agent_studio_capability_provider_url="https://provider.example",
            agent_studio_capability_provider_token_scope="https://management.azure.com/.default",
        )
    )

    assert isinstance(source, HttpCapabilityDiscoverySource)


def test_build_capability_discovery_source_without_token_scope_has_no_credential() -> None:
    source = build_capability_discovery_source(
        Settings(agent_studio_capability_provider_url="https://provider.example")
    )

    assert isinstance(source, HttpCapabilityDiscoverySource)
    assert source._credential is None


def test_capability_provider_url_requires_https_or_local_loopback() -> None:
    local = Settings(agent_studio_capability_provider_url="http://127.0.0.1:9000")
    assert local.agent_studio_capability_provider_url == "http://127.0.0.1:9000"

    with pytest.raises(ValueError, match="must use HTTPS"):
        Settings(agent_studio_capability_provider_url="http://provider.example")


def test_capability_provider_url_rejects_embedded_credentials_or_query() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        Settings(agent_studio_capability_provider_url="https://user:pass@provider.example")
    with pytest.raises(ValueError, match="must not contain credentials"):
        Settings(agent_studio_capability_provider_url="https://provider.example?x=1")


# --- provider-owner findings: idempotency enum + verbatim pins ---------------


@pytest.mark.asyncio
async def test_discover_treats_caller_key_idempotency_as_conditional_not_true() -> None:
    """``caller_key`` is idempotent only when the caller supplies a key, so it
    must never be collapsed into an unconditional idempotent boolean; the exact
    enum is preserved verbatim on the operation pin instead."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(idempotency="caller_key")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors[0].operations[0].idempotent is False
    assert result.descriptor_pins[0].operations[0].idempotency == "caller_key"


@pytest.mark.asyncio
async def test_discover_backend_operation_digest_is_none_without_wire_schema_object() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        operation = _operation_payload()
        del operation["input_schema"]
        descriptor = _descriptor_payload(operations=[operation])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    operation = result.descriptors[0].operations[0]
    # No wire schema object -> no backend digest; the still-present output schema
    # object still yields its own backend digest.
    assert operation.input_schema_digest is None
    assert operation.output_schema_digest == compute_schema_digest({"type": "object"})
    # The raw provider digest is preserved verbatim regardless of the backend digest.
    assert result.descriptor_pins[0].operations[0].input_schema_digest == "a" * 64


@pytest.mark.asyncio
async def test_discover_skips_instance_with_malformed_config_hash_pin() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(config_hash="NOT-A-HEX-DIGEST")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert result.instance_pins == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_with_empty_provider_resource_id_pin() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(provider_resource_id="")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_descriptor_with_malformed_descriptor_digest_pin() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(descriptor_digest="short")
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert result.descriptor_pins == ()
    assert any("could not be translated" in warning for warning in result.warnings)


# --- provider-owner findings: bounds (bytes/cardinality/concurrency/deadline) -


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_exceeds_byte_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_catalog_payload(["foundry"]))

    source, client = _bounded_source(handler, max_response_bytes=16)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "exceeded" in (result.unavailable_reason or "")
    assert result.descriptors == ()


@pytest.mark.asyncio
async def test_discover_degrades_provider_with_oversized_response_to_a_warning() -> None:
    filler = "x" * 200_000

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry")
        payload["warnings"] = [{"reason_code": "big", "message": filler, "provider_id": "foundry"}]
        return httpx.Response(200, json=payload)

    source, client = _bounded_source(handler, max_response_bytes=50_000)
    result = await source.discover(_request())
    await client.aclose()

    # Catalog is small enough to pass; only the oversized provider degrades.
    assert result.available is True
    assert result.descriptors == ()
    assert any("exceeded" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_exceeds_provider_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["one", "two", "three"]))
        return httpx.Response(200, json=_capabilities_payload(request.url.path.split("/")[3], instances=[]))

    source, client = _bounded_source(handler, max_providers=2)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "exceeding the adapter cap" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_skips_provider_exceeding_descriptor_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptors = [_descriptor_payload(descriptor_id="d1"), _descriptor_payload(descriptor_id="d2")]
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=descriptors, instances=[]))

    source, client = _bounded_source(handler, max_descriptors_per_provider=1)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("exceeding the adapter cap" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_provider_exceeding_instance_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instances = [_instance_payload(instance_id="i1"), _instance_payload(instance_id="i2")]
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=instances))

    source, client = _bounded_source(handler, max_instances_per_provider=1)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("exceeding the adapter cap" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_descriptor_exceeding_operation_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(
            operations=[_operation_payload(operation_id="a"), _operation_payload(operation_id="b")]
        )
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _bounded_source(handler, max_operations_per_descriptor=1)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning and "exceeding" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_overall_deadline_exceeded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json=_catalog_payload([]))

    source, client = _bounded_source(handler, deadline_seconds=0.02)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "deadline" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_bounds_per_provider_concurrency_to_configured_max() -> None:
    in_flight = 0
    max_seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_seen
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload([f"p{index}" for index in range(6)]))
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        provider_id = request.url.path.split("/")[3]
        return httpx.Response(200, json=_capabilities_payload(provider_id, descriptors=[], instances=[]))

    source, client = _bounded_source(handler, max_concurrency=2)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    # Without the semaphore all six providers would run at once.
    assert max_seen <= 2


@pytest.mark.asyncio
async def test_discover_empty_catalog_still_returns_refresh_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=_catalog_payload([]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.refresh_metadata is not None
    assert result.refresh_metadata.provider_ids == ()


def test_build_capability_discovery_source_wires_settings_bounds() -> None:
    source = build_capability_discovery_source(
        Settings(
            agent_studio_capability_provider_url="https://provider.example",
            agent_studio_capability_provider_max_response_bytes=123,
            agent_studio_capability_provider_max_providers=4,
            agent_studio_capability_provider_max_descriptors_per_provider=5,
            agent_studio_capability_provider_max_instances_per_provider=6,
            agent_studio_capability_provider_max_operations_per_descriptor=7,
            agent_studio_capability_provider_max_concurrency=3,
            agent_studio_capability_provider_deadline_seconds=9.0,
        )
    )

    assert isinstance(source, HttpCapabilityDiscoverySource)
    assert source._max_response_bytes == 123
    assert source._max_providers == 4
    assert source._max_descriptors_per_provider == 5
    assert source._max_instances_per_provider == 6
    assert source._max_operations_per_descriptor == 7
    assert source._max_concurrency == 3
    assert source._deadline_seconds == 9.0


# --- provider-owner review follow-ups: stricter fail-closed hardening ---------


@pytest.mark.asyncio
async def test_discover_skips_operation_with_non_object_wire_schema() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(input_schema="not-an-object")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_descriptor_with_duplicate_operation_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(
            operations=[_operation_payload(operation_id="dup"), _operation_payload(operation_id="dup")]
        )
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("duplicate operation ids" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_with_mismatched_contract_version() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(provider_contract_version="research-assistant.integration-provider.v6")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("provider_contract_version" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_whose_provider_id_does_not_match_enclosing_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(provider_id="someone-else")
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("does not match its enclosing provider" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_refuses_to_request_an_unsafe_provider_id() -> None:
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["../admin"]))
        return httpx.Response(200, json=_capabilities_payload("../admin"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors == ()
    assert any("not a safe opaque identifier" in warning for warning in result.warnings)
    # The unsafe provider id was never interpolated into a request path.
    assert requested_paths == ["/v1/providers"]
    # ...and never reaches refresh metadata, which exists to seed a future
    # refresh scheduler and must not carry unsanitised wire values.
    assert result.refresh_metadata is not None
    assert result.refresh_metadata.provider_ids == ()


@pytest.mark.asyncio
async def test_discover_one_provider_rejects_unsafe_id_as_defence_in_depth() -> None:
    """The catalog boundary makes this unreachable via ``discover()``; the guard
    is retained so a future direct caller cannot reintroduce path injection."""

    async def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("no request may be issued for an unsafe provider id")

    source, client = _source(handler)
    with pytest.raises(CapabilityProviderProtocolError, match="safe opaque identifier"):
        await source._discover_one_provider("../admin", {}, "user-1")
    await client.aclose()


@pytest.mark.asyncio
async def test_discover_rejects_instance_whose_null_provider_id_coerces_to_the_string_none() -> None:
    """``str(None) == "None"``, so a provider legitimately named "None" plus a
    null echo would satisfy a coercing cross-check. Both sides must be real
    strings."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["None"]))
        instance = _instance_payload(provider_id=None)
        return httpx.Response(200, json=_capabilities_payload("None", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert result.instance_pins == ()
    assert any("does not match its enclosing provider" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_rejects_capabilities_response_whose_null_provider_id_coerces_to_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["None"]))
        payload = _capabilities_payload("None", instances=[])
        payload["provider_id"] = None
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("provider_id mismatch" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "project_id"])
async def test_discover_skips_instance_with_non_string_scope_field(field: str) -> None:
    """A null scope value must not become the literal string "None".

    Previously this reached ``CapabilityInstance`` as ``"None"`` and was caught
    only downstream by ``CapabilityRegistry.from_source``'s scope check; it now
    fails closed at the source."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(**{field: None})
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert result.instance_pins == ()
    assert any(f"{field} must be a non-empty string" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_disagreeing_with_descriptor_digest() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(descriptor_digest="a" * 64)  # descriptor pins "c" * 64
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert len(result.descriptors) == 1
    assert result.instances == ()
    assert any("disagrees with its descriptor" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_disagreeing_with_descriptor_version() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(descriptor_version="9")  # descriptor version is "1"
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert len(result.descriptors) == 1
    assert result.instances == ()
    assert any("disagrees with its descriptor" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_providers_is_not_a_list() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["providers"] = None
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "was not a JSON array" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_reports_unavailable_on_non_200_catalog_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(302, json={"redirected": True}, headers={"location": "https://evil.example"})

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "non-200" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_rejects_every_occurrence_of_a_duplicate_descriptor_id() -> None:
    """Duplicate identities are resolved on content, not arrival position.

    Keeping the first (or last) occurrence would let wire ordering decide which
    descriptor is retained and therefore every downstream digest, so every
    occurrence is rejected instead."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptors = [
            _descriptor_payload(descriptor_id="dup", descriptor_digest="a" * 64),
            _descriptor_payload(descriptor_id="dup", descriptor_digest="b" * 64),
        ]
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=descriptors, instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert result.descriptor_pins == ()
    assert any("declared more than once" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_rejects_every_occurrence_of_a_duplicate_instance_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instances = [_instance_payload(instance_id="dup"), _instance_payload(instance_id="dup")]
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=instances))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert result.instance_pins == ()
    assert any("declared more than once" in warning for warning in result.warnings)


def _duplicate_descriptor_capabilities(reverse: bool) -> dict[str, Any]:
    """Two same-id descriptors differing in content, plus an instance matching
    only the second one. Ordering is the single variable under test."""

    first = _descriptor_payload(descriptor_id="dup", descriptor_digest="a" * 64, name="Alpha")
    second = _descriptor_payload(descriptor_id="dup", descriptor_digest="b" * 64, name="Bravo")
    descriptors = [second, first] if reverse else [first, second]
    instance = _instance_payload(descriptor_id="dup", descriptor_digest="b" * 64)
    return _capabilities_payload("foundry", descriptors=descriptors, instances=[instance])


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_discover_duplicate_resolution_is_independent_of_wire_order(reverse: bool) -> None:
    """Regression lock for an arrival-order nondeterminism.

    Under the previous keep-first rule this exact payload retained a different
    descriptor per ordering -- and, worse, the instance became bindable in one
    ordering and was dropped in the other. Both orderings must now produce the
    identical (empty, fail-closed) outcome."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        return httpx.Response(200, json=_duplicate_descriptor_capabilities(reverse))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert result.descriptor_pins == ()
    # The instance must not become bindable just because the duplicate it agreed
    # with happened to arrive first.
    assert result.instances == ()
    assert result.instance_pins == ()
    assert any("declared more than once" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_treats_absent_operation_array_field_as_empty() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        operation = _operation_payload()
        del operation["least_privilege_scopes"]
        descriptor = _descriptor_payload(operations=[operation])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors[0].operations[0].least_privilege_scopes == ()


@pytest.mark.asyncio
async def test_discover_skips_operation_with_non_array_security_field() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        # A bare string where an array is required would silently explode into
        # per-character destinations; the adapter must fail closed instead.
        descriptor = _descriptor_payload(operations=[_operation_payload(side_effect_destinations="prod-db")])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_with_non_object_configuration() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        instance = _instance_payload(configuration=None)
        return httpx.Response(200, json=_capabilities_payload("foundry", instances=[instance]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.instances == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_operation_with_non_string_array_member() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        # A non-string member must fail closed, not be str()-coerced into "123".
        descriptor = _descriptor_payload(operations=[_operation_payload(least_privilege_roles=[123])])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert any("could not be translated" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_filters_catalog_entries_without_a_string_provider_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            payload = _catalog_payload(["foundry"])
            payload["providers"].append(
                {
                    "provider_id": None,
                    "family": "microsoft_foundry",
                    "name": "broken",
                    "description": "no id",
                    "auth_modes": [],
                    "provenance": [],
                    "capability_descriptors": [],
                }
            )
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_capabilities_payload("foundry"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    # The null-provider_id entry is filtered but honestly surfaced as a warning,
    # so an available result never hides an incomplete catalog.
    assert result.available is True
    assert len(result.descriptors) == 1
    assert result.refresh_metadata is not None
    assert result.refresh_metadata.provider_ids == ("foundry",)
    assert any("no usable string provider_id" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_discover_skips_instance_disagreeing_with_its_unambiguous_descriptor() -> None:
    """The descriptor-correlation guard still applies for non-duplicate ids.

    (Superseded the old keep-first "retained duplicate" test: duplicate ids are
    now rejected wholesale, so correlation only ever runs against an
    unambiguous descriptor.)"""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(descriptor_id="solo", descriptor_digest="a" * 64)
        instance = _instance_payload(descriptor_id="solo", descriptor_digest="b" * 64)
        return httpx.Response(
            200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[instance])
        )

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert len(result.descriptors) == 1
    assert result.descriptor_pins[0].descriptor_digest == "a" * 64
    assert result.instances == ()
    assert any("disagrees with its descriptor" in warning for warning in result.warnings)


# --- second-pass reviewer findings: skipped-not-failed audit -----------------
#
# Both defects below sat on FULLY COVERED lines: the catalog warnings generator
# runs on every well-formed catalog, and operation identity is built for every
# operation. Neither introduced an arc to miss, so line+branch coverage was
# structurally incapable of seeing them -- they need adversarial input, not more
# coverage.


MALFORMED_WARNINGS = ["boom", [1, 2, 3], ["a", "b"], [None], {"a": 1}, [[1]]]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_warnings", MALFORMED_WARNINGS)
async def test_discover_contains_malformed_catalog_warnings(bad_warnings: Any) -> None:
    """FINDING A. ``catalog['warnings']`` is untrusted; a non-array-of-objects
    shape previously raised AttributeError -- a type no caller in this module
    catches -- so it escaped ``discover``, ``discover_with_timeout`` AND
    ``CapabilityRegistry.from_source``, letting a provider decide whether the
    module honoured its own fail-closed contract."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["warnings"] = bad_warnings
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    # Contained, and the malformation is surfaced rather than silently dropped.
    assert result.available is True
    assert result.warnings != ()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_warnings", MALFORMED_WARNINGS)
async def test_discover_with_timeout_contains_malformed_catalog_warnings(bad_warnings: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["warnings"] = bad_warnings
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await discover_with_timeout(source, _request())
    await client.aclose()

    assert result.available is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_warnings", MALFORMED_WARNINGS)
async def test_registry_from_source_contains_malformed_catalog_warnings(bad_warnings: Any) -> None:
    """The trust boundary that matters: nothing may cross into the registry."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["warnings"] = bad_warnings
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    registry = await CapabilityRegistry.from_source(source, _request())
    await client.aclose()

    assert registry.available is True


@pytest.mark.asyncio
async def test_discover_contains_malformed_per_provider_warnings() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry", instances=[])
        payload["warnings"] = "boom"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    # Degrades to a warning, and the provider's descriptors still translate.
    assert result.available is True
    assert len(result.descriptors) == 1
    assert any("not a JSON array" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_id", None),
        ("operation_id", 7),
        ("operation_id", True),
        ("operation_id", 1.5),
        ("operation_version", None),
        ("operation_version", 3),
    ],
)
async def test_discover_rejects_synthetic_operation_identity(field: str, value: Any) -> None:
    """FINDING B. Operation identity feeds approval/policy lookup via
    ``CapabilityDescriptor.operation(name)`` and audit correlation via
    ``RawOperationPins.operation_id``, so a coerced ``'None'``/``'7'``/``'True'``
    is a synthetic identity in a governance path. ``Field(min_length=1)`` cannot
    catch it because ``'None'`` is four characters."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        descriptor = _descriptor_payload(operations=[_operation_payload(**{field: value})])
        return httpx.Response(200, json=_capabilities_payload("foundry", descriptors=[descriptor], instances=[]))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.descriptors == ()
    assert result.descriptor_pins == ()
    assert any(f"{field} must be a non-empty string" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["descriptors", "instances"])
async def test_discover_skips_provider_whose_collection_is_not_an_array(collection: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry", instances=[])
        payload[collection] = "not-an-array"
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("was not a JSON array" in warning for warning in result.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["descriptors", "instances"])
async def test_discover_skips_non_object_entries_without_failing_the_provider(collection: str) -> None:
    """A non-object entry must degrade that ENTRY, not the whole provider.

    Previously it raised AttributeError inside the mapper, which the per-item
    handler did not catch, so one bad entry cost the entire provider."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry")
        payload[collection] = ["i-am-a-string", *payload[collection]]
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("was not a JSON object" in warning for warning in result.warnings)
    # The well-formed sibling entry still translated.
    assert len(result.descriptors) == 1


@pytest.mark.asyncio
async def test_discover_treats_absent_catalog_warnings_as_empty() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        del payload["warnings"]
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.warnings == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("collection", ["descriptors", "instances"])
async def test_discover_treats_absent_provider_collection_as_empty(collection: str) -> None:
    """An absent collection is legitimately empty; only a present non-array fails."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=_catalog_payload(["foundry"]))
        payload = _capabilities_payload("foundry", descriptors=[], instances=[])
        del payload[collection]
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors == ()
    assert result.instances == ()
    assert result.warnings == ()
