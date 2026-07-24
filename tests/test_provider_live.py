from __future__ import annotations

import os
import time

import httpx
import pytest
from research_assistant_connectors.providers import (
    AccessToken,
    AuthConfig,
    AuthMode,
    AzureAISearchProvider,
    AzureBlobProvider,
    AzureFunctionsProvider,
    BlobConfig,
    FoundryConfig,
    FoundryProvider,
    FunctionsConfig,
    GitHubConfig,
    GitHubProvider,
    GraphConfig,
    InvocationContext,
    MCPConfig,
    MCPStreamableHTTPProvider,
    MicrosoftGraphProvider,
    OpenAPIConfig,
    OpenAPIProvider,
    Readiness,
    SearchConfig,
    WebhookConfig,
    WebhookProvider,
)
from research_assistant_connectors.providers.contracts import PlatformProvider


class LiveCredential:
    def __init__(self, token: str | None, secret: str | None) -> None:
        self._token = token
        self._secret = secret

    def get_token(self, *_scopes: str) -> AccessToken:
        if not self._token:
            raise RuntimeError("PROVIDER_INTEGRATION_TOKEN is required for OAuth integration checks")
        return AccessToken(self._token, int(time.time()) + 300)

    def get_secret(self, _name: str) -> str:
        if not self._secret:
            raise RuntimeError("PROVIDER_INTEGRATION_SECRET is required for API-key integration checks")
        return self._secret


def required(name: str) -> str:
    value = os.getenv(name)
    assert value, f"Live provider test configuration is missing: {name}"
    return value


def auth_config() -> AuthConfig:
    mode = AuthMode(os.getenv("PROVIDER_INTEGRATION_AUTH_MODE", "oauth"))
    if mode is AuthMode.NONE:
        return AuthConfig(mode)
    if mode in {AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.GITHUB_APP}:
        return AuthConfig(mode, token_scope=required("PROVIDER_INTEGRATION_TOKEN_SCOPE"))
    if mode is AuthMode.API_KEY:
        return AuthConfig(
            mode,
            secret_name="integration-secret",
            header_name=required("PROVIDER_INTEGRATION_KEY_HEADER"),
        )
    raise AssertionError(f"Unsupported live integration auth mode: {mode.value}")


def live_provider(kind: str, endpoint: str, tenant_id: str, auth: AuthConfig) -> PlatformProvider:
    if kind == "mcp":
        return MCPStreamableHTTPProvider(MCPConfig(endpoint, tenant_id, auth))
    if kind == "openapi":
        return OpenAPIProvider(
            OpenAPIConfig(
                endpoint,
                tenant_id,
                auth,
                document_url=required("PROVIDER_INTEGRATION_DOCUMENT_URL"),
                document_auth=auth,
            )
        )
    if kind == "webhook":
        return WebhookProvider(WebhookConfig(endpoint, tenant_id, "integration.health", auth))
    if kind == "github":
        return GitHubProvider(
            GitHubConfig(
                endpoint,
                tenant_id,
                auth,
                owner=os.getenv("PROVIDER_INTEGRATION_OWNER"),
            )
        )
    if kind == "graph":
        return MicrosoftGraphProvider(GraphConfig(endpoint, tenant_id, auth, discover_items=False))
    if kind == "search":
        return AzureAISearchProvider(SearchConfig(endpoint, tenant_id, auth))
    if kind == "blob":
        return AzureBlobProvider(BlobConfig(endpoint, tenant_id, auth))
    if kind == "functions":
        return AzureFunctionsProvider(
            FunctionsConfig(
                endpoint,
                tenant_id,
                auth,
                discovery_url=required("PROVIDER_INTEGRATION_DISCOVERY_URL"),
                discovery_style=os.getenv("PROVIDER_INTEGRATION_DISCOVERY_STYLE", "http"),
                discovery_auth=auth,
            )
        )
    if kind == "foundry":
        return FoundryProvider(
            FoundryConfig(
                endpoint,
                tenant_id,
                auth,
                models_path=required("PROVIDER_INTEGRATION_DISCOVERY_PATH"),
            )
        )
    raise AssertionError(f"Unsupported PROVIDER_INTEGRATION_KIND: {kind}")


@pytest.mark.skipif(
    os.getenv("RUN_PROVIDER_INTEGRATION") != "1",
    reason="Set RUN_PROVIDER_INTEGRATION=1 to opt in to live provider checks.",
)
def test_live_provider_discovery_and_health() -> None:
    kind = required("PROVIDER_INTEGRATION_KIND")
    endpoint = required("PROVIDER_INTEGRATION_ENDPOINT")
    tenant_id = required("PROVIDER_INTEGRATION_TENANT_ID")
    auth = auth_config()
    provider = live_provider(kind, endpoint, tenant_id, auth)
    with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
        context = InvocationContext(
            tenant_id=tenant_id,
            principal_id="provider-integration-test",
            project_id=required("PROVIDER_INTEGRATION_PROJECT_ID"),
            credential=LiveCredential(
                os.getenv("PROVIDER_INTEGRATION_TOKEN"),
                os.getenv("PROVIDER_INTEGRATION_SECRET"),
            ),
            transport=client,
            correlation_id="provider-integration-test",
            trace_id="provider-integration-test",
            sleep=time.sleep,
            release_id="provider-integration-test",
            invocation_id="provider-integration-test",
        )
        capabilities = provider.discover(context)
        assert capabilities.instances, "Live provider discovery returned no capability instances"
        target = capabilities.instances[0]
        validation = provider.validate(target, context)
        assert validation.readiness is Readiness.READY, validation.reasons
        health = provider.health(target, context)
    assert capabilities, "Live provider discovery returned no capability descriptors"
    assert health.readiness is Readiness.READY, health.evidence
