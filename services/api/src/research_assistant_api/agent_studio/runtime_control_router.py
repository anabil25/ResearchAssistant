"""Internal runtime-control ASGI app (``/internal/v1/runtime``).

This is a **separate** FastAPI application, mounted by the composition root at
``/internal/v1/runtime``. Being a distinct app (not routes on the public API)
is what keeps every internal route out of the public ``app.openapi()``
document: the public human OpenAPI never lists an internal runtime-control
route, and this app owns its own, separately-generated runtime OpenAPI.

Every endpoint runs the same mapping-bound authorization first (see
``runtime_authz``): resolve the platform-validated runtime principal, then
``enforce_runtime_authorization`` against the loaded mapping. Any failure -- no
principal, bad issuer/audience/role, mapping absent, client not allowlisted,
ref/digest mismatch -- collapses to a single uniform 404 carrying
``uniform_denial()``, so a probe can never distinguish forbidden from
not-found. The authorized mapping is the sole source of scope/binding facts;
nothing authoritative is ever taken from the request body.

This slice implements the mapping-retrieval endpoint (the runtime-safe view,
excluding the server-side allowlist). Context, consume, idempotency, and
attestation endpoints are added in subsequent reviewed slices that delegate to
the existing ``approval_context``/``approval_consumption``/``idempotency``/
``release_attestation`` services.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status

from research_assistant_api.agent_studio.runtime_authz import (
    RuntimeAuthorizationError,
    RuntimeAuthPolicy,
    enforce_runtime_authorization,
    uniform_denial,
)
from research_assistant_api.agent_studio.runtime_control_schemas import (
    RuntimeBindingView,
    RuntimeMappingRetrieveRequest,
    RuntimeMappingView,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_identity import resolve_runtime_principal
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore
from research_assistant_api.config import Settings

RUNTIME_CONTROL_BASE_PATH = "/internal/v1/runtime"


def build_runtime_control_app(
    *,
    mapping_store: RuntimeDeploymentMappingStore,
    auth_policy: RuntimeAuthPolicy,
    settings: Settings,
) -> FastAPI:
    """Construct the internal runtime-control ASGI app with explicit deps."""

    app = FastAPI(
        title="Research Assistant Runtime Control",
        version="1.0.0",
        description="Internal runtime-control plane (research-assistant.runtime-control.v1).",
    )

    def _authorize(
        request: Request,
        *,
        deployment_id: str,
        mapping_ref: str,
        mapping_digest: str,
    ) -> RuntimeDeploymentMapping:
        """Run the full mapping-bound auth order; raise a uniform 404 on any denial."""
        principal = resolve_runtime_principal(request, settings)
        if principal is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=uniform_denial())
        try:
            return enforce_runtime_authorization(
                policy=auth_policy,
                principal=principal,
                presented_deployment_id=deployment_id,
                presented_mapping_ref=mapping_ref,
                presented_mapping_digest=mapping_digest,
                load_mapping=lambda: mapping_store.get(deployment_id),
            )
        except RuntimeAuthorizationError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=uniform_denial()) from exc

    @app.post(f"{RUNTIME_CONTROL_BASE_PATH}/mappings/{{deployment_id}}/retrieve", response_model=RuntimeMappingView)
    def retrieve_mapping(
        deployment_id: str, payload: RuntimeMappingRetrieveRequest, request: Request
    ) -> RuntimeMappingView:
        mapping = _authorize(
            request,
            deployment_id=deployment_id,
            mapping_ref=payload.mapping_ref,
            mapping_digest=payload.mapping_digest,
        )
        return _mapping_view(mapping)

    return app


def _mapping_view(mapping: RuntimeDeploymentMapping) -> RuntimeMappingView:
    """Project an authorized mapping into its runtime-safe view (no allowlist)."""
    return RuntimeMappingView(
        deployment_id=mapping.deployment_id,
        mapping_ref=mapping.mapping_ref,
        mapping_digest=mapping.mapping_digest,
        tenant_id=mapping.tenant_id,
        project_id=mapping.project_id,
        environment=mapping.environment,
        logical_agent_id=mapping.logical_agent_id,
        backend_release_id=mapping.backend_release_id,
        backend_version=mapping.backend_version,
        provider_contract_version=mapping.provider_contract_version,
        provider_artifact_digest=mapping.provider_artifact_digest,
        binding=RuntimeBindingView(
            binding_id=mapping.binding.binding_id,
            provider_contract_version=mapping.binding.provider_contract_version,
            descriptor_id=mapping.binding.descriptor_ref.id,
            operation_id=mapping.binding.operation_ref.id,
            destination_hash_algorithm=mapping.binding.destination_hash_policy.algorithm,
        ),
        lifecycle_state=mapping.lifecycle_state.value,
    )
