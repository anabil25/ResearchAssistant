# mypy: disable-error-code=import-untyped
"""Artifact-backed golden test for the provider-v7 discovery adapter.

This pins the adapter to the *exact* public flat-v7 OpenAPI contract published
by the integration provider (commit ``b2745459...``). It reads only the
committed wire-contract JSON artifact and this backend's own adapter -- it never
imports the provider's Python package (or any GPL/unlicensed source), so the
backend's field mapping stays verified against the frozen public contract
without coupling to provider-internal code.

The artifact is committed byte-for-byte (CRLF, treated as binary via
``.gitattributes``) so its SHA-256 stays byte-identical to the provider-owner
pin on every platform.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from research_assistant_api.agent_studio.capability_discovery import (
    EXPECTED_CANONICALIZATION_VERSION,
    EXPECTED_PROVIDER_CONTRACT_VERSION,
    CapabilityDiscoveryRequest,
    HttpCapabilityDiscoverySource,
)
from research_assistant_api.agent_studio.models import InstanceReadiness, OperationClass
from research_assistant_api.agent_studio.schema_ref_resolver import compute_schema_digest
from research_assistant_api.agent_studio.scope import ScopeContext

#: The exact provider-owner pin for the flat-v7 OpenAPI artifact (SHA-256 of the
#: committed CRLF bytes). Authoritative source: provider commit
#: b2745459bfdeae1625f35a9503e5b5fcc3478c9d.
GOLDEN_V7_OPENAPI_SHA256 = "4b3830bcbddb97e9379c1f464d0167ea65c379da429ef33a93bb4cb509620495"

GOLDEN_PATH = Path(__file__).parent / "golden" / "provider-adapter-openapi.v7.json"


def _golden_bytes() -> bytes:
    return GOLDEN_PATH.read_bytes()


def test_golden_artifact_matches_provider_owner_pinned_sha256() -> None:
    digest = hashlib.sha256(_golden_bytes()).hexdigest()
    assert digest == GOLDEN_V7_OPENAPI_SHA256


def test_golden_artifact_declares_the_contract_generation_the_adapter_translates() -> None:
    document = json.loads(_golden_bytes())
    assert document["info"]["version"] == EXPECTED_PROVIDER_CONTRACT_VERSION
    # The two discovery endpoints the adapter calls must exist in the contract.
    assert "/v1/providers" in document["paths"]
    assert "/v1/providers/{provider_id}/capabilities" in document["paths"]


def test_golden_artifact_requires_every_field_the_adapter_preserves_verbatim() -> None:
    """Bind the adapter's raw-pin assumptions to the committed contract.

    If a future contract stopped requiring one of these provider-owned pins, the
    adapter's verbatim-preservation guarantee would silently weaken; this test
    fails closed on that drift.
    """

    schemas = json.loads(_golden_bytes())["components"]["schemas"]

    operation_required = set(schemas["OperationDescriptorResponse"]["required"])
    assert {
        "operation_id",
        "operation_version",
        "idempotency",
        "approval_policy",
        "input_schema_digest",
        "output_schema_digest",
        "input_schema",
        "output_schema",
    } <= operation_required

    descriptor_required = set(schemas["CapabilityDescriptorResponse"]["required"])
    assert {"descriptor_id", "descriptor_version", "descriptor_digest"} <= descriptor_required

    instance_required = set(schemas["CapabilityInstanceResponse"]["required"])
    assert {
        "instance_id",
        "provider_resource_id",
        "config_hash",
        "instance_fingerprint",
        "descriptor_digest",
        "connection_authorization_digest",
        "allowed_destinations_digest",
    } <= instance_required


def _request() -> CapabilityDiscoveryRequest:
    return CapabilityDiscoveryRequest(
        scope=ScopeContext(tenant_id="tenant-1", project_id="project-1"),
        principal="user-1",
        correlation_id="correlation-1",
        timeout_seconds=5.0,
    )


def _contract_conformant_capabilities() -> dict[str, Any]:
    """A capabilities payload whose keys conform to the committed v7 schema."""

    operation = {
        "operation_id": "search",
        "operation_version": "3",
        "maturity": "ga",
        "lifecycle": "active",
        "operation_class": "read",
        "approval_policy": "required",
        "idempotency": "caller_key",
        "external_side_effect": False,
        "side_effect_destinations": [],
        "timeout_seconds": 30,
        "max_retries": 1,
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "output_schema": {"type": "object"},
        "input_schema_digest": "1" * 64,
        "output_schema_digest": "2" * 64,
        "least_privilege_scopes": [],
        "least_privilege_roles": [],
        "docs": ["https://example.com/docs"],
        "audit_events": [],
    }
    descriptor = {
        "descriptor_id": "file_search",
        "descriptor_version": "7",
        "descriptor_digest": "3" * 64,
        "family": "microsoft_foundry",
        "resource_kind": "search_index",
        "name": "File Search",
        "auth_modes": ["managed_identity"],
        "operations": [operation],
        "provenance": [],
        "observability": [],
        "audit": [],
        "metadata": {},
    }
    instance: dict[str, Any] = {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "provider_id": "foundry",
        "instance_id": "instance-1",
        "descriptor_id": "file_search",
        "descriptor_version": "7",
        "descriptor_digest": "3" * 64,
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "provider_resource_id": "/subscriptions/x/resource",
        "discovered_provider_version": "2024-05-01",
        "discovered_resource_version": "1",
        "name": "File Search Instance",
        "readiness": "ready",
        "health": "ready",
        "last_checked_at": "2024-05-01T00:00:00+00:00",
        "configuration": {},
        "config_hash": "4" * 64,
        "connection_ref": None,
        "connection_version": "1",
        "auth_mode": "managed_identity",
        "connection_identity_mode": "managed_identity",
        "connection_scopes": [],
        "connection_roles": [],
        "connection_authorization_digest": "5" * 64,
        "instance_fingerprint": "6" * 64,
        "bindability": [],
        "config_validated": True,
        "allowed_destination_constraints": [],
        "allowed_destinations_digest": "7" * 64,
        "status_evidence": [],
    }
    return {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "canonicalization_version": EXPECTED_CANONICALIZATION_VERSION,
        "provider_id": "foundry",
        "descriptors": [descriptor],
        "instances": [instance],
        "warnings": [],
        "refreshed_at": "2024-05-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_adapter_roundtrips_verbatim_pins_from_contract_conformant_payload() -> None:
    catalog = {
        "provider_contract_version": EXPECTED_PROVIDER_CONTRACT_VERSION,
        "canonicalization_version": EXPECTED_CANONICALIZATION_VERSION,
        "providers": [
            {
                "provider_id": "foundry",
                "family": "microsoft_foundry",
                "name": "foundry",
                "description": "foundry provider",
                "auth_modes": ["managed_identity"],
                "provenance": [],
                "capability_descriptors": [],
            }
        ],
        "warnings": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/providers":
            return httpx.Response(200, json=catalog)
        assert request.url.path == "/v1/providers/foundry/capabilities"
        return httpx.Response(200, json=_contract_conformant_capabilities())

    client = httpx.AsyncClient(base_url="https://provider.example", transport=httpx.MockTransport(handler))
    source = HttpCapabilityDiscoverySource("https://provider.example", client=client)
    result = await source.discover(_request())
    await client.aclose()

    assert result.available is True
    assert result.warnings == ()

    operation = result.descriptors[0].operations[0]
    assert operation.operation_class == OperationClass.READ
    # caller_key is conditional -> not unconditionally idempotent.
    assert operation.idempotent is False
    # Backend operation digests are this backend's own canonical digest of the
    # wire schema objects, distinct from the provider's own digests.
    assert operation.input_schema_digest == compute_schema_digest(
        {"type": "object", "properties": {"q": {"type": "string"}}}
    )
    assert operation.output_schema_digest == compute_schema_digest({"type": "object"})

    instance = result.instances[0]
    assert instance.readiness == InstanceReadiness.READY
    # Backend recomputes these downstream; adapter leaves them unset.
    assert instance.descriptor_digest is None
    assert instance.instance_fingerprint is None

    # Every provider-owned pin is preserved verbatim, separately named.
    descriptor_pin = result.descriptor_pins[0]
    assert descriptor_pin.descriptor_digest == "3" * 64
    operation_pin = descriptor_pin.operations[0]
    assert operation_pin.idempotency == "caller_key"
    assert operation_pin.approval_policy == "required"
    assert operation_pin.input_schema_digest == "1" * 64
    assert operation_pin.output_schema_digest == "2" * 64

    instance_pin = result.instance_pins[0]
    assert instance_pin.provider_resource_id == "/subscriptions/x/resource"
    assert instance_pin.config_hash == "4" * 64
    assert instance_pin.connection_authorization_digest == "5" * 64
    assert instance_pin.instance_fingerprint == "6" * 64
    assert instance_pin.allowed_destinations_digest == "7" * 64
    assert instance_pin.descriptor_digest == "3" * 64
