"""Composition-root wiring tests for Agent Studio's ``_init_agent_studio``.

These specifically close the gap the harness flagged: proving that when the
Agent Studio metadata store is available, ``app.state.agent_studio_*``
collaborators -- most importantly ``agent_studio_approval_context_resolver``,
the port a hosted runtime must call through ``POST /approvals/context`` to
resolve which approval currently authorizes a capability operation (e.g.
``dataset.compute``) -- are wired to concrete, store-backed implementations,
never left at ``None``. ``None`` is only ever the correct, fail-closed value
when the metadata store itself is genuinely unavailable (no Cosmos endpoint
configured); this file asserts both branches explicitly rather than relying
on incidental coverage from unrelated route tests.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from research_assistant_api.agent_studio.approval_consumption import StoreBackedApprovalConsumptionPort
from research_assistant_api.agent_studio.approval_context import StoreBackedApprovalContextResolver
from research_assistant_api.agent_studio.builder_service import BuilderService
from research_assistant_api.agent_studio.deployment_service import DeploymentService
from research_assistant_api.agent_studio.idempotency import StoreBackedIdempotencyPort
from research_assistant_api.agent_studio.release_attestation import StoreBackedReleaseAttestationPort
from research_assistant_api.agent_studio.release_service import ReleaseService
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError
from research_assistant_api.config import Settings

# ``research_assistant_api/__init__.py`` does ``from .app import app``, which
# rebinds the package's ``app`` attribute to the FastAPI *instance* -- so
# ``import research_assistant_api.app as app_module`` (attribute traversal)
# would silently resolve to that instance rather than the module. Look the
# actual module up in ``sys.modules`` via ``importlib`` instead so
# ``app_module.build_agent_studio_store`` (a module-level import inside
# ``app.py``) and ``app_module._init_agent_studio`` are reachable/patchable.
app_module = importlib.import_module("research_assistant_api.app")


def test_init_agent_studio_wires_concrete_collaborators_when_store_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AgentStudioStore()
    monkeypatch.setattr(app_module, "build_agent_studio_store", lambda settings: store)

    # ``_init_agent_studio`` also independently builds the (unrelated)
    # memory store from ``settings.cosmos_endpoint`` -- leaving it unset
    # keeps this test isolated to the metadata-store wiring under test
    # (memory store construction fails closed to ``None``, which is
    # correct and not what this test asserts on) instead of requiring a
    # second, real Cosmos client mock.
    application = FastAPI()
    app_module._init_agent_studio(application, Settings(cosmos_endpoint=None))

    resolver = application.state.agent_studio_approval_context_resolver
    assert isinstance(resolver, StoreBackedApprovalContextResolver)
    # Bound to the exact store this composition constructed -- not a stray
    # default or a second, disconnected instance that would silently
    # resolve against empty state.
    assert resolver._store is store

    assert application.state.agent_studio_store is store
    assert isinstance(application.state.agent_studio_release_service, ReleaseService)
    assert isinstance(application.state.agent_studio_deployment_service, DeploymentService)
    assert isinstance(application.state.agent_studio_builder_service, BuilderService)
    assert isinstance(
        application.state.agent_studio_approval_consumption_port, StoreBackedApprovalConsumptionPort
    )
    assert isinstance(application.state.agent_studio_idempotency_port, StoreBackedIdempotencyPort)
    assert isinstance(
        application.state.agent_studio_release_attestation_port, StoreBackedReleaseAttestationPort
    )


def test_init_agent_studio_leaves_collaborators_none_when_store_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(settings: Settings) -> AgentStudioStore:
        raise AgentStudioStoreError("no cosmos endpoint configured for this test")

    monkeypatch.setattr(app_module, "build_agent_studio_store", _raise)

    application = FastAPI()
    app_module._init_agent_studio(application, Settings(cosmos_endpoint=None))

    assert application.state.agent_studio_store is None
    assert application.state.agent_studio_release_service is None
    assert application.state.agent_studio_deployment_service is None
    assert application.state.agent_studio_builder_service is None
    assert application.state.agent_studio_approval_consumption_port is None
    assert application.state.agent_studio_idempotency_port is None
    assert application.state.agent_studio_approval_context_resolver is None
    assert application.state.agent_studio_release_attestation_port is None


# --- OpenAPI Entra ID / Container Apps EasyAuth security-scheme documentation


def test_custom_openapi_declares_entra_bearer_security_scheme() -> None:
    """Harness integration blocker #2 flagged: "OpenAPI has no security scheme".

    This asserts the generated OpenAPI document honestly documents the
    Entra ID bearer-token boundary that Azure Container Apps' built-in
    authentication (EasyAuth / ``authConfigs``) is responsible for
    enforcing -- see ``research_assistant_api.identity.resolve_identity``
    and ``config.Settings.entra_auth_enforced`` for the corresponding
    request/startup-time trust boundary this documents.
    """
    application = app_module.app
    application.openapi_schema = None  # force regeneration for this test
    schema = application.openapi()

    scheme = schema["components"]["securitySchemes"]["entraManagedIdentity"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT"
    assert schema["security"] == [{"entraManagedIdentity": []}]


def test_custom_openapi_caches_the_generated_schema() -> None:
    application = app_module.app
    application.openapi_schema = None
    first = application.openapi()
    second = application.openapi()
    assert first is second
    application.openapi_schema = None
