from __future__ import annotations

import base64
import json
from typing import Any

from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptorRef,
    CapabilityOperationRef,
    DeploymentEnvironment,
)
from research_assistant_api.agent_studio.runtime_authz import RuntimeAuthPolicy
from research_assistant_api.agent_studio.runtime_control_router import build_runtime_control_app
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDestinationHashPolicy,
    RuntimeMappingLifecycleState,
)
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore
from research_assistant_api.config import Settings

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://research-assistant-runtime"
RUNTIME_ROLE = "research-assistant.runtime"
CLIENT_APP_ID = "client-app-1"
RETRIEVE_URL = "/internal/v1/runtime/mappings/dep-1/retrieve"


def _mapping(
    *,
    deployment_id: str = "dep-1",
    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE,
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=CapabilityDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=CapabilityOperationRef(id="search", version="1"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="backend-release-1",
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=CLIENT_APP_ID, app_role=RUNTIME_ROLE),
        ),
        lifecycle_state=lifecycle_state,
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


def _client(mapping: RuntimeDeploymentMapping | None) -> TestClient:
    store = InMemoryRuntimeDeploymentMappingStore()
    if mapping is not None:
        store.put(mapping)
    settings = Settings(trust_platform_identity_headers=True, entra_auth_enforced=True)
    app = build_runtime_control_app(mapping_store=store, auth_policy=_policy(), settings=settings)
    return TestClient(app)


def _body(mapping: RuntimeDeploymentMapping) -> dict[str, str]:
    return {"mapping_ref": mapping.mapping_ref, "mapping_digest": mapping.mapping_digest}


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
    response = client.post(
        RETRIEVE_URL,
        json={"mapping_ref": "runtime-deployment-mapping:v1:dep-1", "mapping_digest": "x"},
        headers={"x-ms-client-principal": _principal_header()},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "The requested runtime deployment is not available."


def test_retrieve_digest_mismatch_is_uniform_404() -> None:
    mapping = _mapping()
    client = _client(mapping)
    response = client.post(
        RETRIEVE_URL,
        json={"mapping_ref": mapping.mapping_ref, "mapping_digest": "runtime-deployment-mapping:v1:sha256:deadbeef"},
        headers={"x-ms-client-principal": _principal_header()},
    )
    assert response.status_code == 404


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
