"""Fail-closed production mount for the internal runtime-control app.

The composition root never mounts the real runtime-control endpoints unless the
runtime-trust preconditions are fully met. This is a *structural* fail-closed
gate that sits above the per-request identity gate (``resolve_runtime_principal``
already returns ``None`` unless both trust flags are set): when trust is not
enforceable, or any durable dependency is missing, the mount serves a
fail-closed app that answers EVERY path with the same uniform 404
(``uniform_denial()``) the real plane uses for a denied/absent deployment -- so a
probe cannot distinguish "not configured" from "no such deployment", and a
misconfigured or half-provisioned deployment can never silently expose the
control plane with a permissive default.

Enforceability requires BOTH ``entra_auth_enforced`` and
``trust_platform_identity_headers`` (the same pair the identity layer checks) AND
every durable dependency present. A demo/unset environment therefore yields the
fail-closed app, never the real one.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, status

from research_assistant_api.agent_studio.approval_context import ApprovalContextResolver
from research_assistant_api.agent_studio.runtime_authz import RuntimeAuthPolicy, uniform_denial
from research_assistant_api.agent_studio.runtime_client_binding import ClientDeploymentBindingResolver
from research_assistant_api.agent_studio.runtime_control_router import build_runtime_control_app
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore
from research_assistant_api.config import Settings


def runtime_trust_is_enforceable(settings: Settings) -> bool:
    """True iff the platform trust preconditions for the runtime plane are met.

    Both flags are required: the platform must be validating identity
    (``entra_auth_enforced``) AND the backend must be configured to trust the
    platform-injected principal headers (``trust_platform_identity_headers``).
    Either one alone is insufficient and yields the fail-closed app.
    """
    return bool(settings.entra_auth_enforced and settings.trust_platform_identity_headers)


def build_fail_closed_runtime_control_app() -> FastAPI:
    """An app that denies EVERY route with the uniform runtime-control 404.

    Mounted in place of the real runtime-control app whenever trust is not
    enforceable or a dependency is missing. It exposes no real endpoint and
    leaks nothing: every method and path collapses to the identical
    ``uniform_denial()`` body the real plane returns.
    """
    app = FastAPI(
        title="Research Assistant Runtime Control (fail-closed)",
        version="1.0.0",
        description="Runtime-control plane is not enforceable in this configuration; all routes deny.",
    )

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def _deny(path: str) -> None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=uniform_denial())

    return app


def build_runtime_control_mount(
    *,
    settings: Settings,
    mapping_store: RuntimeDeploymentMappingStore | None,
    client_binding_resolver: ClientDeploymentBindingResolver | None,
    auth_policy: RuntimeAuthPolicy | None,
    context_resolver: ApprovalContextResolver | None,
) -> FastAPI:
    """Return the real runtime-control app, or the fail-closed app.

    The real app is built ONLY when trust is enforceable AND every durable
    dependency is present. Any missing precondition -- trust flags off, or a
    ``None`` store/resolver/policy (e.g. Cosmos not configured) -- yields the
    fail-closed app. There is no permissive fallback.
    """
    if (
        not runtime_trust_is_enforceable(settings)
        or mapping_store is None
        or client_binding_resolver is None
        or auth_policy is None
        or context_resolver is None
    ):
        return build_fail_closed_runtime_control_app()
    return build_runtime_control_app(
        mapping_store=mapping_store,
        client_binding_resolver=client_binding_resolver,
        auth_policy=auth_policy,
        settings=settings,
        context_resolver=context_resolver,
    )
