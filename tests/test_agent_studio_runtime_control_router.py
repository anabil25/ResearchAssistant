from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.approval_context import StoreBackedApprovalContextResolver
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    CapabilityBinding,
    CapabilityConnectionRef,
    CapabilityDescriptorRef,
    CapabilityInstanceRef,
    CapabilityOperationRef,
    CapabilityPolicyRef,
    DeploymentEnvironment,
    ReleaseStatus,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.runtime_authz import RuntimeAuthPolicy
from research_assistant_api.agent_studio.runtime_client_binding import (
    InMemoryClientDeploymentBindingIndex,
    RuntimeBindingStatus,
)
from research_assistant_api.agent_studio.runtime_control_router import build_runtime_control_app
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeMappingLifecycleState,
    RuntimeOperationRef,
)
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://research-assistant-runtime"
RUNTIME_ROLE = "research-assistant.runtime"
CLIENT_APP_ID = "client-app-1"
RETRIEVE_URL = "/internal/v1/runtime/mappings/dep-1/retrieve"
CONTEXT_URL = "/internal/v1/runtime/context"
SCOPE = ScopeContext(tenant_id="tenant-1", project_id="project-1")
REQUEST_DIGEST = "f" * 64


def _mapping(
    *,
    deployment_id: str = "dep-1",
    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE,
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=RuntimeOperationRef(id="search", version="1"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-context-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="release-1",
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=CLIENT_APP_ID, app_role=RUNTIME_ROLE),
        ),
        lifecycle_state=lifecycle_state,
        revision_sequence=1,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="release-service",
    )


def _policy() -> RuntimeAuthPolicy:
    return RuntimeAuthPolicy(expected_issuer=ISSUER, expected_audience=AUDIENCE, required_app_role=RUNTIME_ROLE)


def _principal_header(
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    role: str = RUNTIME_ROLE,
    client_app_id: str = CLIENT_APP_ID,
) -> str:
    payload: dict[str, Any] = {
        "userId": "sp-1",
        "claims": [
            {"typ": "iss", "val": issuer},
            {"typ": "aud", "val": audience},
            {"typ": "roles", "val": role},
            {"typ": "appid", "val": client_app_id},
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _empty_resolver() -> StoreBackedApprovalContextResolver:
    return StoreBackedApprovalContextResolver(AgentStudioStore())


def _seeded_resolver(*, approval_state: ApprovalState = ApprovalState.APPROVED) -> StoreBackedApprovalContextResolver:
    """An approval-context resolver over a store seeded with a version/release
    and (optionally approved) capability-operation approval matching the
    mapping's release-1 / binding-1 / search operation on descriptor-1."""
    store = AgentStudioStore()
    binding = CapabilityBinding(
        binding_id="binding-1",
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id="descriptor-1"),
        operation_ref=CapabilityOperationRef(id="search"),
        instance_ref=CapabilityInstanceRef(provider_id="provider-1", id="instance-1", fingerprint="fp-1"),
        connection_ref=CapabilityConnectionRef(id="connection-1"),
        policy_ref=CapabilityPolicyRef(id="policy-1"),
        attached_by="user-1",
    )
    manifest = AgentManifest(
        logical_agent_id="agent-context-1",
        tenant_id="tenant-1",
        project_id="project-1",
        display_name="Context Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        capabilities=(binding,),
    )
    store.create_version(
        SCOPE,
        AgentVersion(
            id="version-1",
            logical_agent_id="agent-context-1",
            tenant_id="tenant-1",
            project_id="project-1",
            sequence=1,
            manifest=manifest,
            manifest_hash="sha256:" + "a" * 64,
            created_by="user-1",
        ),
    )
    store.create_release(
        SCOPE,
        AgentRelease(
            id="release-1",
            version_id="version-1",
            logical_agent_id="agent-context-1",
            tenant_id="tenant-1",
            project_id="project-1",
            status=ReleaseStatus.GATED,
            environment=DeploymentEnvironment.DEVELOPMENT,
            manifest_hash="sha256:" + "a" * 64,
            created_by="user-1",
        ),
    )
    from datetime import UTC, datetime

    store.create_approval(
        SCOPE,
        StudioApprovalRecord(
            id="approval-1",
            version_id="version-1",
            tenant_id="tenant-1",
            project_id="project-1",
            kind=ApprovalKind.CAPABILITY_OPERATION,
            state=approval_state,
            gated_action="invoke_capability_operation",
            destination="descriptor-1.search",
            requested_by="user-1",
            evidence_summary="Evidence.",
            risk="medium",
            idempotency_key="approval-key-1",
            approver_id="user-1" if approval_state is ApprovalState.APPROVED else None,
            decided_at=datetime.now(UTC) if approval_state is ApprovalState.APPROVED else None,
            decision_revision=1 if approval_state is ApprovalState.APPROVED else 0,
        ),
    )
    return StoreBackedApprovalContextResolver(store)


def _client(
    mapping: RuntimeDeploymentMapping | None,
    *,
    context_resolver: StoreBackedApprovalContextResolver | None = None,
) -> TestClient:
    store = InMemoryRuntimeDeploymentMappingStore()
    resolver = InMemoryClientDeploymentBindingIndex()
    if mapping is not None:
        store.put(mapping)
    # The authenticated runtime client is server-bound to exactly this
    # deployment's current revision (or "dep-1"/a placeholder revision when there
    # is no mapping, to exercise the bound-client-but-no-mapping path).
    resolver.repoint(
        CLIENT_APP_ID,
        mapping.deployment_id if mapping is not None else "dep-1",
        mapping.revision_sequence if mapping is not None else 1,
        mapping.revision_id if mapping is not None else "no-such-revision",
        RuntimeBindingStatus.ACTIVE,
        expected_current_sequence=None,
    )
    settings = Settings(trust_platform_identity_headers=True, entra_auth_enforced=True)
    app = build_runtime_control_app(
        mapping_store=store,
        client_binding_resolver=resolver,
        auth_policy=_policy(),
        settings=settings,
        context_resolver=context_resolver if context_resolver is not None else _empty_resolver(),
    )
    return TestClient(app)


def _ref(mapping: RuntimeDeploymentMapping, *, digest: str | None = None) -> dict[str, object]:
    return {
        "id": mapping.deployment_id,
        "schema_version": mapping.schema_version,
        "revision": mapping.revision_sequence,
        "digest": digest if digest is not None else mapping.mapping_digest,
    }


def _body(mapping: RuntimeDeploymentMapping) -> dict[str, object]:
    return {"mapping_ref": _ref(mapping)}


def test_retrieve_returns_runtime_safe_view_for_authorized_runtime() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(RETRIEVE_URL, json=_body(mapping), headers={"x-ms-client-principal": _principal_header()})
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] == "dep-1"
    assert body["tenant_id"] == "tenant-1"
    assert body["binding"]["operation_id"] == "search"
    assert body["binding"]["destination_hash_algorithm"] == "destination:v1:sha256"
    # The server-side allowlist must never appear in the runtime-facing view.
    assert "allowed_client_app_role_bindings" not in body
    assert "allowlist" not in json.dumps(body).lower()


def test_retrieve_without_principal_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(RETRIEVE_URL, json=_body(mapping))
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_retrieve_wrong_role_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(
        RETRIEVE_URL,
        json=_body(mapping),
        headers={"x-ms-client-principal": _principal_header(role="some.other.role")},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_retrieve_unknown_deployment_is_uniform_404() -> None:
    # Store empty -> mapping not found; identical response to a forbidden case.
    client = _client(mapping=None)
    ref = {"id": "dep-1", "schema_version": "runtime-deployment-mapping:v1", "revision": 1, "digest": "x"}
    response = client.post(
        RETRIEVE_URL,
        json={"mapping_ref": ref},
        headers={"x-ms-client-principal": _principal_header()},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_retrieve_digest_mismatch_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(
        RETRIEVE_URL,
        json={"mapping_ref": _ref(mapping, digest="runtime-deployment-mapping:v1:sha256:deadbeef")},
        headers={"x-ms-client-principal": _principal_header()},
    )
    assert response.status_code == 404


def test_retrieve_ref_id_not_matching_path_is_uniform_404() -> None:
    # Ruling A: the in-body mapping_ref.id must match the path deployment_id; a
    # mismatch is the same uniform 404 (never a 400), so the body can never
    # redirect the request to a different deployment than the path names.
    mapping = _mapping()
    client = _client(mapping)
    ref = {
        "id": "dep-elsewhere",
        "schema_version": mapping.schema_version,
        "revision": mapping.revision_sequence,
        "digest": mapping.mapping_digest,
    }
    response = client.post(
        RETRIEVE_URL, json={"mapping_ref": ref}, headers={"x-ms-client-principal": _principal_header()}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_retrieve_client_not_allowlisted_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(
        RETRIEVE_URL,
        json=_body(mapping),
        headers={"x-ms-client-principal": _principal_header(client_app_id="stranger-app")},
    )
    assert response.status_code == 404


def test_retrieve_superseded_mapping_is_uniform_404() -> None:
    mapping = _mapping(lifecycle_state=RuntimeMappingLifecycleState.SUPERSEDED)
    client = _client(mapping)
    response = client.post(RETRIEVE_URL, json=_body(mapping), headers={"x-ms-client-principal": _principal_header()})
    assert response.status_code == 404


def test_internal_routes_carry_the_internal_base_path() -> None:
    client = _client(_mapping())
    paths = [route.path for route in client.app.routes]  # type: ignore[attr-defined]
    assert any(path.startswith("/internal/v1/runtime/") for path in paths)


# --- context endpoint ------------------------------------------------------


def _context_body(mapping: RuntimeDeploymentMapping, *, operation_id: str = "search") -> dict[str, object]:
    return {
        "mapping_ref": _ref(mapping),
        "operation_id": operation_id,
        "request_digest": REQUEST_DIGEST,
    }


def _context_headers(mapping: RuntimeDeploymentMapping) -> dict[str, str]:
    return {"x-ms-client-principal": _principal_header()}


def test_context_resolved_returns_mapping_derived_approval() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver())
    response = client.post(CONTEXT_URL, json=_context_body(mapping), headers=_context_headers(mapping))
    assert response.status_code == 200
    body = response.json()
    assert body["approval_id"] == "approval-1"
    # Monotonic INTEGER decision-record revision (ordered, rollback-detectable),
    # never the pinned agent version_id; the decision digest is a SEPARATE field.
    assert body["approval_version"] == 1
    assert body["approval_version"] != "version-1"
    assert body["approval_decision_digest"].startswith("approval-decision:v1:sha256:")
    assert body["invocation_id"].startswith("inv-")
    assert body["request_digest"] == REQUEST_DIGEST
    assert body["tenant_id"] == "tenant-1"
    # No non-resolved decision field leaks into the success wire.
    assert "decision" not in body


def test_context_not_approved_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver(approval_state=ApprovalState.PENDING))
    response = client.post(CONTEXT_URL, json=_context_body(mapping), headers=_context_headers(mapping))
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_context_operation_mismatch_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver())
    response = client.post(
        CONTEXT_URL, json=_context_body(mapping, operation_id="write"), headers=_context_headers(mapping)
    )
    assert response.status_code == 404


def test_context_unknown_release_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_empty_resolver())
    response = client.post(CONTEXT_URL, json=_context_body(mapping), headers=_context_headers(mapping))
    assert response.status_code == 404


def test_context_missing_digest_in_ref_is_rejected() -> None:
    # Ruling A: the digest lives inside the canonical mapping_ref object; a
    # mapping_ref missing its digest is a schema violation (422), not a 404.
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver())
    ref_no_digest = {"id": mapping.deployment_id, "schema_version": mapping.schema_version, "revision": 1}
    response = client.post(
        CONTEXT_URL,
        json={"mapping_ref": ref_no_digest, "operation_id": "search", "request_digest": REQUEST_DIGEST},
        headers={"x-ms-client-principal": _principal_header()},
    )
    assert response.status_code == 422


def test_context_wrong_digest_in_ref_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver())
    body = {
        "mapping_ref": _ref(mapping, digest="runtime-deployment-mapping:v1:sha256:deadbeef"),
        "operation_id": "search",
        "request_digest": REQUEST_DIGEST,
    }
    response = client.post(CONTEXT_URL, json=body, headers={"x-ms-client-principal": _principal_header()})
    assert response.status_code == 404


def test_context_without_principal_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping, context_resolver=_seeded_resolver())
    response = client.post(CONTEXT_URL, json=_context_body(mapping))
    assert response.status_code == 404


def test_all_denial_reasons_produce_identical_response_body() -> None:
    """Every distinct denial cause must be indistinguishable to the caller --
    same status and byte-identical body -- so nothing leaks which check failed."""
    mapping = _mapping()
    client = _client(mapping)
    bodies: list[tuple[int, str]] = []
    # no principal
    bodies.append(_collect(client.post(RETRIEVE_URL, json=_body(mapping))))
    # wrong role
    bodies.append(
        _collect(
            client.post(
                RETRIEVE_URL, json=_body(mapping), headers={"x-ms-client-principal": _principal_header(role="nope")}
            )
        )
    )
    # not allowlisted
    bodies.append(
        _collect(
            client.post(
                RETRIEVE_URL,
                json=_body(mapping),
                headers={"x-ms-client-principal": _principal_header(client_app_id="stranger")},
            )
        )
    )
    # digest mismatch
    bodies.append(
        _collect(
            client.post(
                RETRIEVE_URL,
                json={"mapping_ref": _ref(mapping, digest="runtime-deployment-mapping:v1:sha256:00")},
                headers={"x-ms-client-principal": _principal_header()},
            )
        )
    )
    # unknown deployment
    empty = _client(mapping=None)
    bodies.append(
        _collect(
            empty.post(
                RETRIEVE_URL,
                json={
                    "mapping_ref": {
                        "id": "dep-1",
                        "schema_version": "runtime-deployment-mapping:v1",
                        "revision": 1,
                        "digest": "x",
                    }
                },
                headers={"x-ms-client-principal": _principal_header()},
            )
        )
    )
    # distinct lifecycle faults (expired/revoked/superseded/retired) -- each a
    # different internal audit reason but the SAME uniform external body.
    past = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    for update in (
        {"lifecycle_state": RuntimeMappingLifecycleState.SUPERSEDED},
        {"lifecycle_state": RuntimeMappingLifecycleState.RETIRED},
        {"revoked_at": past},
        {"expires_at": past},
        {"revision_created_at": future},  # not-yet-effective
    ):
        faulted = _mapping().model_copy(update=update)
        faulted_client = _client(faulted)
        bodies.append(
            _collect(
                faulted_client.post(
                    RETRIEVE_URL, json=_body(faulted), headers={"x-ms-client-principal": _principal_header()}
                )
            )
        )
    assert len(set(bodies)) == 1, bodies


def _collect(response: object) -> tuple[int, str]:
    import json as _json

    status_code = response.status_code  # type: ignore[attr-defined]
    return status_code, _json.dumps(response.json(), sort_keys=True)  # type: ignore[attr-defined]

