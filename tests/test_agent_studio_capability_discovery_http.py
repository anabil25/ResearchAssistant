# mypy: disable-error-code=import-untyped

from __future__ import annotations

from time import time
from types import TracebackType
from typing import Any, Self

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from research_assistant_api.agent_studio.capability_discovery import (
    EXPECTED_CANONICALIZATION_VERSION,
    EXPECTED_PROVIDER_CONTRACT_VERSION,
    CapabilityDiscoveryRequest,
    HttpCapabilityDiscoverySource,
    NullCapabilityDiscoverySource,
    build_capability_discovery_source,
)
from research_assistant_api.agent_studio.models import HealthStatus, InstanceReadiness, OperationClass
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


class FailingCredential:
    """Credential whose ``get_token`` always raises, for fail-closed tests.

    Mirrors ``FakeCredential``'s shape (``get_token``/``close``/async context
    manager) but simulates a token-acquisition failure -- the realistic
    ``ClientAuthenticationError``/``CredentialUnavailableError`` a real
    ``ManagedIdentityCredential`` raises whenever no managed identity is
    reachable, which is the common case for this adapter running outside an
    Azure host with a real MI endpoint.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.closed = False

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: object,
    ) -> AccessToken:
        del scopes, claims, tenant_id, enable_cae, kwargs
        raise self._error

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


def _source(
    handler: Any, *, credential: AsyncTokenCredential | None = None, token_scope: str | None = None
) -> tuple[HttpCapabilityDiscoverySource, httpx.AsyncClient]:
    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    source = HttpCapabilityDiscoverySource(
        "https://provider.example", credential=credential, token_scope=token_scope, client=client
    )
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
    assert operation.input_schema_digest == f"provider-rfc8785-sha256:{'a' * 64}"
    assert operation.output_schema_digest == f"provider-rfc8785-sha256:{'b' * 64}"

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
    # Provider-reported digests are never trusted as backend-authoritative.
    assert instance.descriptor_digest is None
    assert instance.instance_fingerprint is None


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
async def test_discover_reports_unavailable_when_credential_token_acquisition_fails() -> None:
    """Managed-identity token acquisition failure must degrade the same way
    an unreachable catalog endpoint does -- never raise uncaught out of
    ``discover()``. Prior to this fix, ``_headers()`` (and its
    ``credential.get_token`` call) ran *outside* the try/except that only
    covered the catalog HTTP request, so a real
    ``ClientAuthenticationError`` here would have propagated through
    ``discover_with_timeout``/``CapabilityRegistry.from_source`` uncaught --
    crashing whatever composed this adapter (e.g. application startup)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("the catalog endpoint must not be called when token acquisition fails")

    credential = FailingCredential(ClientAuthenticationError("token endpoint unreachable"))
    source, client = _source(handler, credential=credential, token_scope="https://management.azure.com/.default")
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "token endpoint unreachable" in (result.unavailable_reason or "")
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_managed_identity_is_unavailable() -> None:
    """``CredentialUnavailableError`` (no managed identity endpoint found at
    all -- the realistic case for this adapter running anywhere outside an
    Azure host with a real MI) is a subclass of ``ClientAuthenticationError``
    and must degrade identically, not crash startup."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("the catalog endpoint must not be called when no managed identity is available")

    credential = FailingCredential(CredentialUnavailableError("no managed identity endpoint found"))
    source, client = _source(handler, credential=credential, token_scope="https://management.azure.com/.default")
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "no managed identity endpoint found" in (result.unavailable_reason or "")
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_reports_unavailable_when_catalog_providers_field_is_not_a_list() -> None:
    """A catalog whose ``providers`` field is not itself a JSON array is a
    catalog-level schema failure (not a single malformed entry): it must
    degrade the whole pass to ``available=False`` rather than raising while
    iterating a non-iterable/wrongly-shaped value."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["providers"] = {"not": "a list"}
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is False
    assert "providers" in (result.unavailable_reason or "")


@pytest.mark.asyncio
async def test_discover_skips_non_object_provider_catalog_entries() -> None:
    """A malformed (non-object) entry within an otherwise well-formed
    ``providers`` array must be silently skipped, not crash the whole
    catalog pass -- mirroring how a malformed descriptor/instance already
    degrades to a per-item skip rather than a total failure."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            payload = _catalog_payload(["good"])
            payload["providers"].append("not-an-object")
            payload["providers"].append(42)
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_capabilities_payload("good"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert len(result.descriptors) == 1
    assert result.descriptors[0].provider == "good"


@pytest.mark.asyncio
async def test_discover_treats_null_catalog_providers_field_as_empty() -> None:
    """A catalog whose ``providers`` field is explicitly ``null`` is a valid
    (if empty) provider list -- distinct from an outright non-array schema
    failure -- and must degrade to an empty catalog rather than crashing."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["providers"] = None
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_treats_non_list_catalog_warnings_field_as_empty() -> None:
    """A catalog ``warnings`` field that is not itself a JSON array (e.g. a
    bare object) is a malformed-but-non-fatal caveat container: it must
    degrade to "no warnings" rather than raising while iterating a
    non-iterable value."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["warnings"] = {"not": "a list"}
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_discover_stringifies_non_object_catalog_warning_entries() -> None:
    """A catalog ``warnings`` entry that is not itself an object (a
    malformed but still honestly-surfaced caveat) is stringified rather than
    raising ``AttributeError`` from an unguarded ``.get(...)`` call."""

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        payload = _catalog_payload([])
        payload["warnings"] = ["a bare string warning"]
        return httpx.Response(200, json=payload)

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert any("a bare string warning" in warning for warning in result.warnings)


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
    must not crash -- the second occurrence's descriptors/instances collide
    with the first's namespaced identities and are dropped with a warning."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            payload = _catalog_payload(["foundry"])
            payload["providers"] = payload["providers"] * 2
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=_capabilities_payload("foundry"))

    source, client = _source(handler)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert len(result.descriptors) == 1
    assert len(result.instances) == 1
    assert any("collided" in warning for warning in result.warnings)


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
async def test_discover_treats_missing_operation_digest_as_none() -> None:
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

    assert result.descriptors[0].operations[0].input_schema_digest is None


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
