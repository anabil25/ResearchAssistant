"""Production composition for integration providers."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import sleep
from typing import Any
from uuid import uuid4

import httpx
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from fastapi import HTTPException, Request
from research_assistant_connectors.providers import (
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    ApprovalConsumptionStatus,
    AsyncProviderRegistry,
    AuthMode,
    DiscoveryWarning,
    InvocationContext,
    ProviderEnvironment,
)
from research_assistant_connectors.providers._http import require_endpoint
from research_assistant_connectors.providers.config import (
    AuthConfig,
    BlobConfig,
    FoundryConfig,
    FunctionsConfig,
    GraphConfig,
    MCPConfig,
    OpenAPIConfig,
    SearchConfig,
    WebhookConfig,
)

from research_assistant_connector_adapter.provider_api import ProviderService

PROVIDER_DEADLINE_SECONDS_ENV = "RESEARCH_PROVIDER_DEADLINE_SECONDS"
PROVIDER_RELEASE_ID_ENV = "RESEARCH_PROVIDER_RELEASE_ID"
APPROVAL_CONSUMPTION_URL_ENV = "RESEARCH_APPROVAL_CONSUMPTION_URL"
APPROVAL_CONSUMPTION_SCOPE_ENV = "RESEARCH_APPROVAL_CONSUMPTION_TOKEN_SCOPE"
_DEFAULT_DEADLINE_SECONDS = 30.0


def _optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return value.strip() if value and value.strip() else None


def _managed_identity_auth(
    *,
    scope: str,
    connection_ref: str,
    roles: tuple[str, ...],
) -> AuthConfig:
    return AuthConfig(
        AuthMode.MANAGED_IDENTITY,
        scope,
        connection_ref=connection_ref,
        identity_mode="managed_identity",
        authorized_roles=roles,
    )


def provider_environment_from_environment(
    environ: Mapping[str, str],
) -> ProviderEnvironment | None:
    tenant_id = _optional(environ, "RESEARCH_WORKSPACE_TENANT_ID")
    providers: list[Any] = []

    foundry_endpoint = _optional(environ, "FOUNDRY_PROJECT_ENDPOINT")
    foundry_models = _optional(environ, "RESEARCH_FOUNDRY_MODELS_PATH")
    foundry_deployments = _optional(environ, "RESEARCH_FOUNDRY_DEPLOYMENTS_PATH")
    foundry_agents = _optional(environ, "RESEARCH_FOUNDRY_AGENTS_PATH")
    foundry_connections = _optional(environ, "RESEARCH_FOUNDRY_CONNECTIONS_PATH")
    foundry_vector_stores = _optional(environ, "RESEARCH_FOUNDRY_VECTOR_STORES_PATH")
    foundry_responses = _optional(environ, "RESEARCH_FOUNDRY_RESPONSES_PATH")
    if foundry_endpoint and any(
        (
            foundry_models,
            foundry_deployments,
            foundry_agents,
            foundry_connections,
            foundry_vector_stores,
            foundry_responses,
        )
    ):
        providers.append(
            FoundryConfig(
                foundry_endpoint,
                tenant_id,
                _managed_identity_auth(
                    scope="https://ai.azure.com/.default",
                    connection_ref="foundry-project-managed-identity",
                    roles=("Azure AI User",),
                ),
                models_path=foundry_models,
                deployments_path=foundry_deployments,
                agents_path=foundry_agents,
                connections_path=foundry_connections,
                vector_stores_path=foundry_vector_stores,
                responses_path=foundry_responses,
            )
        )

    search_endpoint = _optional(environ, "AZURE_SEARCH_ENDPOINT")
    if search_endpoint:
        providers.append(
            SearchConfig(
                search_endpoint,
                tenant_id,
                _managed_identity_auth(
                    scope="https://search.azure.com/.default",
                    connection_ref="azure-search-managed-identity",
                    roles=("Search Index Data Reader",),
                ),
            )
        )

    blob_endpoint = _optional(environ, "AZURE_STORAGE_BLOB_ENDPOINT")
    if blob_endpoint:
        providers.append(
            BlobConfig(
                blob_endpoint,
                tenant_id,
                _managed_identity_auth(
                    scope="https://storage.azure.com/.default",
                    connection_ref="azure-blob-managed-identity",
                    roles=("Storage Blob Data Contributor",),
                ),
            )
        )

    graph_endpoint = _optional(environ, "RESEARCH_GRAPH_ENDPOINT")
    if graph_endpoint:
        providers.append(
            GraphConfig(
                graph_endpoint,
                tenant_id,
                _managed_identity_auth(
                    scope="https://graph.microsoft.com/.default",
                    connection_ref="microsoft-graph-managed-identity",
                    roles=("Sites.Selected",),
                ),
            )
        )

    functions_endpoint = _optional(environ, "RESEARCH_FUNCTIONS_ENDPOINT")
    if functions_endpoint:
        token_scope = _optional(environ, "RESEARCH_FUNCTIONS_TOKEN_SCOPE")
        discovery_url = _optional(environ, "RESEARCH_FUNCTIONS_DISCOVERY_URL")
        if not token_scope:
            raise ValueError("RESEARCH_FUNCTIONS_TOKEN_SCOPE is required with RESEARCH_FUNCTIONS_ENDPOINT")
        providers.append(
            FunctionsConfig(
                functions_endpoint,
                tenant_id,
                _managed_identity_auth(
                    scope=token_scope,
                    connection_ref="azure-functions-managed-identity",
                    roles=("Function invocation role",),
                ),
                discovery_url,
            )
        )

    mcp_endpoint = _optional(environ, "RESEARCH_MCP_ENDPOINT")
    if mcp_endpoint:
        providers.append(MCPConfig(mcp_endpoint, tenant_id))

    openapi_endpoint = _optional(environ, "RESEARCH_OPENAPI_ENDPOINT")
    if openapi_endpoint:
        document_url = _optional(environ, "RESEARCH_OPENAPI_DOCUMENT_URL")
        if not document_url:
            raise ValueError("RESEARCH_OPENAPI_DOCUMENT_URL is required with RESEARCH_OPENAPI_ENDPOINT")
        providers.append(OpenAPIConfig(openapi_endpoint, tenant_id, document_url=document_url))

    webhook_endpoint = _optional(environ, "RESEARCH_WEBHOOK_ENDPOINT")
    if webhook_endpoint:
        operation_id = _optional(environ, "RESEARCH_WEBHOOK_OPERATION_ID")
        if not operation_id:
            raise ValueError("RESEARCH_WEBHOOK_OPERATION_ID is required with RESEARCH_WEBHOOK_ENDPOINT")
        providers.append(WebhookConfig(webhook_endpoint, tenant_id, operation_id))

    if not providers:
        return None
    if not tenant_id:
        raise ValueError("RESEARCH_WORKSPACE_TENANT_ID is required when providers are configured")
    for config in providers:
        endpoint = getattr(config, "endpoint", None) or getattr(config, "base_url", None) or getattr(
            config, "destination_url", None
        )
        require_endpoint(endpoint)
    return ProviderEnvironment(
        _optional(environ, "RESEARCH_ENVIRONMENT") or "connector-adapter",
        tenant_id,
        tuple(providers),
    )


def _deadline_seconds(environ: Mapping[str, str]) -> float:
    raw = _optional(environ, PROVIDER_DEADLINE_SECONDS_ENV)
    if raw is None:
        return _DEFAULT_DEADLINE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{PROVIDER_DEADLINE_SECONDS_ENV} must be numeric") from exc
    if not 0 < value <= 300:
        raise ValueError(f"{PROVIDER_DEADLINE_SECONDS_ENV} must be between 0 and 300")
    return value


def _approval_payload(request: ApprovalConsumptionRequest) -> dict[str, Any]:
    return {
        "decision_id": request.decision_id,
        "provider_contract_version": request.provider_contract_version,
        "tenant_id": request.tenant_id,
        "project_id": request.project_id,
        "principal_id": request.principal_id,
        "binding_id": request.binding_id,
        "instance_fingerprint": request.instance_fingerprint,
        "descriptor_id": request.descriptor_id,
        "descriptor_version": request.descriptor_version,
        "operation_id": request.operation_id,
        "operation_version": request.operation_version,
        "arguments_hash": request.arguments_hash,
        "resolved_destination_hash": request.resolved_destination_hash,
        "policy_id": request.policy_ref.policy_id,
        "policy_version": request.policy_ref.policy_version,
        "policy_digest": request.policy_ref.policy_digest,
        "release_id": request.release_id,
        "invocation_id": request.invocation_id,
        "idempotency_key": request.idempotency_key,
        "use_policy": request.use_policy.value,
        "max_uses": request.max_uses,
    }


class DurableApprovalConsumptionClient:
    """Calls an atomic durable approval store and fails closed on ambiguity."""

    def __init__(
        self,
        endpoint: str,
        token_scope: str,
        credential: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = require_endpoint(endpoint)
        if not token_scope:
            raise ValueError("Approval consumption token scope is required")
        self._token_scope = token_scope
        self._credential = credential
        self._transport = transport

    async def __call__(
        self,
        request: ApprovalConsumptionRequest,
    ) -> ApprovalConsumptionResult:
        try:
            token = await asyncio.to_thread(self._credential.get_token, self._token_scope)
            if not token.token:
                return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=5.0,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {token.token}"},
                    json=_approval_payload(request),
                )
            if response.status_code != 200:
                return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)
            body = response.json()
            if not isinstance(body, dict):
                return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)
            raw_status = body.get("status")
            if not isinstance(raw_status, str):
                return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)
            return ApprovalConsumptionResult(
                ApprovalConsumptionStatus(raw_status),
                body.get("consumption_record_id"),
                body.get("consumed_at"),
            )
        except (httpx.HTTPError, ValueError, TypeError):
            return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)


async def _unavailable_approval_store(
    _request: ApprovalConsumptionRequest,
) -> ApprovalConsumptionResult:
    return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class ProviderRuntime:
    service: ProviderService
    warning: DiscoveryWarning | None


def build_provider_runtime(
    environ: Mapping[str, str] | None = None,
    *,
    credential_factory: Callable[[], Any] | None = None,
    transport: httpx.Client | None = None,
    approval_transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderRuntime:
    values = os.environ if environ is None else environ
    environment = provider_environment_from_environment(values)
    if environment is None:
        warning = DiscoveryWarning(
            "provider_not_configured",
            "No integration provider endpoints are configured for this deployment.",
            "integration_provider",
        )
        return ProviderRuntime(
            ProviderService(AsyncProviderRegistry(), warnings=(warning,)),
            warning,
        )

    project_id = _optional(values, "RESEARCH_WORKSPACE_PROJECT_ID")
    release_id = _optional(values, PROVIDER_RELEASE_ID_ENV) or _optional(values, "CONTAINER_APP_REVISION")
    if not project_id:
        raise ValueError("RESEARCH_WORKSPACE_PROJECT_ID is required when providers are configured")
    if not release_id:
        raise ValueError(
            f"{PROVIDER_RELEASE_ID_ENV} or CONTAINER_APP_REVISION is required when providers are configured"
        )
    deadline_seconds = _deadline_seconds(values)
    if credential_factory is None:
        client_id = _optional(values, "AZURE_CLIENT_ID")
        credential_factory = (
            (lambda: ManagedIdentityCredential(client_id=client_id))
            if client_id
            else DefaultAzureCredential
        )
    credential = credential_factory()
    provider_transport = transport or httpx.Client(follow_redirects=False)

    approval_url = _optional(values, APPROVAL_CONSUMPTION_URL_ENV)
    approval_scope = _optional(values, APPROVAL_CONSUMPTION_SCOPE_ENV)
    if bool(approval_url) != bool(approval_scope):
        raise ValueError(
            f"{APPROVAL_CONSUMPTION_URL_ENV} and {APPROVAL_CONSUMPTION_SCOPE_ENV} must be configured together"
        )
    approval_client = (
        DurableApprovalConsumptionClient(
            approval_url,
            approval_scope,
            credential,
            transport=approval_transport,
        )
        if approval_url and approval_scope
        else None
    )

    def context_factory(_provider_id: str, request: Request) -> InvocationContext:
        principal_id = getattr(request.state, "authenticated_principal_id", None)
        if not principal_id:
            raise HTTPException(status_code=401, detail="Authenticated provider caller identity is required.")
        deadline_at = (
            datetime.now(UTC) + timedelta(seconds=deadline_seconds)
        ).isoformat()
        return InvocationContext(
            tenant_id=environment.tenant_id,
            principal_id=principal_id,
            project_id=project_id,
            credential=credential,
            transport=provider_transport,
            correlation_id=getattr(request.state, "request_id", f"req-{uuid4().hex}"),
            trace_id=f"trace-{uuid4().hex}",
            sleep=sleep,
            release_id=release_id,
            invocation_id=f"inv-{uuid4().hex}",
            deadline_at=deadline_at,
            is_cancelled=lambda: bool(getattr(request.state, "provider_cancelled", False)),
            consume_approval=approval_client or _unavailable_approval_store,
        )

    service = ProviderService(
        AsyncProviderRegistry.from_environment(environment),
        context_factory,
    )
    return ProviderRuntime(service, None)


__all__ = [
    "APPROVAL_CONSUMPTION_SCOPE_ENV",
    "APPROVAL_CONSUMPTION_URL_ENV",
    "PROVIDER_DEADLINE_SECONDS_ENV",
    "PROVIDER_RELEASE_ID_ENV",
    "DurableApprovalConsumptionClient",
    "ProviderRuntime",
    "build_provider_runtime",
    "provider_environment_from_environment",
]
