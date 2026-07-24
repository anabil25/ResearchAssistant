from __future__ import annotations

from fastapi.testclient import TestClient
from research_assistant_api.agent_studio.approval_context import StoreBackedApprovalContextResolver
from research_assistant_api.agent_studio.runtime_authz import RuntimeAuthPolicy, uniform_denial
from research_assistant_api.agent_studio.runtime_client_binding import InMemoryClientDeploymentBindingIndex
from research_assistant_api.agent_studio.runtime_control_mount import (
    build_fail_closed_runtime_control_app,
    build_runtime_control_mount,
    runtime_trust_is_enforceable,
)
from research_assistant_api.agent_studio.runtime_mapping_store import InMemoryRuntimeDeploymentMappingStore
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://research-assistant-runtime"
RUNTIME_ROLE = "research-assistant.runtime"


def _policy() -> RuntimeAuthPolicy:
    return RuntimeAuthPolicy(expected_issuer=ISSUER, expected_audience=AUDIENCE, required_app_role=RUNTIME_ROLE)


def _resolver() -> StoreBackedApprovalContextResolver:
    return StoreBackedApprovalContextResolver(AgentStudioStore())


def _enforceable_settings() -> Settings:
    return Settings(trust_platform_identity_headers=True, entra_auth_enforced=True)


def _all_deps() -> dict[str, object]:
    return {
        "mapping_store": InMemoryRuntimeDeploymentMappingStore(),
        "client_binding_resolver": InMemoryClientDeploymentBindingIndex(),
        "auth_policy": _policy(),
        "context_resolver": _resolver(),
    }


# --- enforceability --------------------------------------------------------


def test_enforceable_requires_both_flags() -> None:
    assert runtime_trust_is_enforceable(Settings(trust_platform_identity_headers=True, entra_auth_enforced=True))
    assert not runtime_trust_is_enforceable(Settings(trust_platform_identity_headers=True, entra_auth_enforced=False))
    assert not runtime_trust_is_enforceable(Settings(trust_platform_identity_headers=False, entra_auth_enforced=True))
    assert not runtime_trust_is_enforceable(Settings(trust_platform_identity_headers=False, entra_auth_enforced=False))


# --- fail-closed app --------------------------------------------------------


def test_fail_closed_app_denies_every_route_uniformly() -> None:
    client = TestClient(build_fail_closed_runtime_control_app())
    denial = uniform_denial()
    for method, url in [
        ("POST", "/internal/v1/runtime/context"),
        ("POST", "/internal/v1/runtime/mappings/dep-1/retrieve"),
        ("GET", "/internal/v1/runtime/anything"),
        ("PUT", "/whatever"),
    ]:
        response = client.request(method, url)
        assert response.status_code == 404
        assert response.json()["detail"] == denial


# --- mount selection --------------------------------------------------------


def test_mount_builds_real_app_when_enforceable_and_deps_present() -> None:
    app = build_runtime_control_mount(settings=_enforceable_settings(), **_all_deps())  # type: ignore[arg-type]
    paths = [route.path for route in app.routes]  # type: ignore[attr-defined]
    # The real app exposes the concrete internal routes (not a catch-all).
    assert any(p == "/internal/v1/runtime/context" for p in paths)


def test_mount_fails_closed_when_trust_not_enforceable() -> None:
    settings = Settings(trust_platform_identity_headers=False, entra_auth_enforced=True)
    app = build_runtime_control_mount(settings=settings, **_all_deps())  # type: ignore[arg-type]
    # Fail-closed app has no concrete route, only the catch-all deny.
    paths = [route.path for route in app.routes]  # type: ignore[attr-defined]
    assert not any(p == "/internal/v1/runtime/context" for p in paths)
    client = TestClient(app)
    assert client.post("/internal/v1/runtime/context", json={}).status_code == 404


def test_mount_fails_closed_when_any_dependency_missing() -> None:
    settings = _enforceable_settings()
    deps = _all_deps()
    for missing in deps:
        partial = dict(deps)
        partial[missing] = None
        app = build_runtime_control_mount(settings=settings, **partial)  # type: ignore[arg-type]
        paths = [route.path for route in app.routes]  # type: ignore[attr-defined]
        assert not any(p == "/internal/v1/runtime/context" for p in paths), f"{missing} missing should fail closed"
