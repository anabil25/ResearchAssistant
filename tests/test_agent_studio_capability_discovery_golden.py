# mypy: disable-error-code=import-untyped
"""Artifact-backed golden test for the provider-v7 discovery adapter.

This pins the adapter to the *exact* public flat-v7 OpenAPI contract published
by the integration provider (commit ``b2745459...``). It reads only the
committed wire-contract JSON artifact and this backend's own adapter -- it never
imports the provider's Python package (or any GPL/unlicensed source), so the
backend's field mapping stays verified against the frozen public contract
without coupling to provider-internal code.

**Identity is pinned as a PAIR, because neither pin alone is sufficient.**

* The **transport pin** is the SHA-256 of the artifact's exact bytes. It equals
  the digest of the provider's own blob at ``b2745459`` (``7a484f39...``), so the
  baseline is independently verifiable against provider source rather than being
  an artifact of whoever's checkout recorded it. This is what makes the golden a
  *correctness* control rather than merely a *consistency* one.
* The **semantic pin** is a content-canonical digest (parsed JSON, sorted keys,
  compact separators). It is immune to line endings and whitespace, so it cannot
  raise a false alarm from a representation change alone.

The transport pin alone would report phantom drift on any EOL change; the
semantic pin alone would stop detecting real byte-level drift such as key
reordering or duplicate keys (``json.loads`` silently keeps the last duplicate).
Together they distinguish "the contract changed" from "only its representation
changed", and a reviewer can tell which occurred from *which* pin fails.

This file reads only the committed wire-contract JSON and this backend's own
adapter -- it never imports the provider's Python package (or any
GPL/unlicensed source), so the field mapping stays verified against the frozen
public contract without coupling to provider-internal code.

``.gitattributes`` marks the artifact ``-text`` so git performs no EOL
conversion in either direction. That rule is what keeps the transport pin valid
on Windows (``core.autocrlf = true``) as well as on Linux CI; see the rationale
recorded in that file.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
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

#: TRANSPORT pin: SHA-256 of the artifact's exact bytes. Independently
#: verifiable -- this is the digest of the provider's own blob at commit
#: b2745459bfdeae1625f35a9503e5b5fcc3478c9d, so anyone hashing provider source
#: computes this same value. Detects byte-level drift the semantic pin cannot
#: see (key reordering, duplicate keys, whitespace).
GOLDEN_V7_OPENAPI_TRANSPORT_SHA256 = "7a484f394289994572ac99e48296edf6ec5b727c51d4d1d404aafe9dd4f7f76b"

#: SEMANTIC pin: content-canonical digest (parsed, sorted keys, compact
#: separators) -- the same canonicalization this package uses for every other
#: content digest. EOL- and whitespace-immune, so a representation-only change
#: fails the transport pin while this one still passes, making the two
#: distinguishable.
GOLDEN_V7_OPENAPI_CANONICAL_DIGEST = "sha256:878db3f8bd03a413bd7be214495cc89bbbce947e38969d16b2566c413d3600d8"

GOLDEN_PATH = Path(__file__).parent / "golden" / "provider-adapter-openapi.v7.json"


def _golden_bytes() -> bytes:
    return GOLDEN_PATH.read_bytes()


def _canonical_digest(raw: bytes) -> str:
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_golden_transport_pin_matches_the_provider_blob_digest() -> None:
    """Byte identity against the provider's own source, not against our checkout."""

    assert hashlib.sha256(_golden_bytes()).hexdigest() == GOLDEN_V7_OPENAPI_TRANSPORT_SHA256


def test_golden_exemption_survives_repo_wide_normalization() -> None:
    """The `-text` exemption must actually be in effect, not merely present.

    `.gitattributes` resolves by LAST MATCHING LINE, so the golden's `-text` rule
    only holds while it sits AFTER the repository-wide `* text=auto eol=lf` rule.
    Placing it before -- which reads like the natural way to give an exemption
    priority -- silently yields `text: auto`, normalizes the artifact on a
    Windows checkout, and moves the transport pin from 7a484f39... to
    4b3830bc..., i.e. back to the self-referential baseline that was rejected.

    Asserting the resolved attribute makes that ordering machine-enforced. A
    comment can only ask the next maintainer to be careful; this fails the suite.
    """

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "check-attr", "text", "--", "tests/golden/provider-adapter-openapi.v7.json"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )

    # Expected: "...: text: unset". Anything else (notably "auto") means the
    # exemption has been reordered, weakened, or removed.
    assert result.stdout.strip().endswith(": text: unset"), (
        f"golden -text exemption is not in effect ({result.stdout.strip()!r}); check that it appears "
        "AFTER the '* text=auto eol=lf' rule in .gitattributes -- last matching line wins"
    )


def test_golden_semantic_pin_matches_canonical_content_digest() -> None:
    assert _canonical_digest(_golden_bytes()) == GOLDEN_V7_OPENAPI_CANONICAL_DIGEST


def test_golden_is_stored_with_provider_line_endings() -> None:
    """The `-text` .gitattributes rule must keep this file exactly as published.

    If repository-wide EOL normalization ever rewrites the artifact, this fails
    with a precise cause instead of surfacing as an opaque digest mismatch.
    """

    raw = _golden_bytes()
    assert b"\r\n" not in raw, (
        "golden was rewritten to CRLF -- the `-text` rule in .gitattributes was removed or overridden; "
        "this changes the transport pin and will report provider drift that does not exist"
    )


def test_semantic_pin_is_eol_immune_while_transport_pin_is_not() -> None:
    """Documents precisely what each pin does and does not catch."""

    as_lf = _golden_bytes()
    as_crlf = as_lf.replace(b"\n", b"\r\n")

    assert as_lf != as_crlf
    # Representation-only change: semantic pin holds, transport pin moves.
    assert _canonical_digest(as_crlf) == GOLDEN_V7_OPENAPI_CANONICAL_DIGEST
    assert hashlib.sha256(as_crlf).hexdigest() != GOLDEN_V7_OPENAPI_TRANSPORT_SHA256


def test_transport_pin_catches_byte_drift_the_semantic_pin_cannot() -> None:
    """Why the pins are a pair rather than a substitution.

    Key reordering and duplicate keys survive canonicalization -- ``json.loads``
    silently keeps the last duplicate -- so the semantic pin alone would not
    detect them. The transport pin does.
    """

    reordered = json.dumps(json.loads(_golden_bytes()), sort_keys=True).encode("utf-8")
    assert _canonical_digest(reordered) == GOLDEN_V7_OPENAPI_CANONICAL_DIGEST  # semantic pin blind to it
    assert hashlib.sha256(reordered).hexdigest() != GOLDEN_V7_OPENAPI_TRANSPORT_SHA256  # transport catches it

    duplicated = b'{"a": 1, "a": 2}'
    assert json.loads(duplicated) == {"a": 2}  # duplicate key silently collapses
    assert hashlib.sha256(duplicated).hexdigest() != hashlib.sha256(b'{"a": 2}').hexdigest()


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
