"""Connector API keys are written straight to API Management, so the ARM token
path is the only auth surface here and it must not re-authenticate per request."""

from __future__ import annotations

import httpx
import pytest
from azure.core.credentials import AccessToken

from research_assistant_api import connector_credentials
from research_assistant_api.connector_credentials import (
    ConnectorCredentialNotConfiguredError,
    set_connector_api_key,
)


class _CountingCredential:
    def __init__(self) -> None:
        self.calls = 0

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        self.calls += 1
        return AccessToken("token-value", 4_102_444_800)


@pytest.fixture
def apim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1")
    monkeypatch.setenv("AZURE_RESOURCE_GROUP", "rg-1")
    monkeypatch.setenv("AZURE_API_MANAGEMENT_NAME", "apim-1")


def _transport(recorder: list[httpx.Request]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={})

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.mark.usefixtures("apim_env")
def test_arm_token_is_reused_across_requests() -> None:
    """A credential does not cache on its own; the bearer provider must be kept."""
    credential = _CountingCredential()
    requests: list[httpx.Request] = []
    with _transport(requests) as client:
        for _ in range(3):
            set_connector_api_key("nv-1", "secret", credential=credential, client=client)

    assert len(requests) == 3
    assert credential.calls == 1
    assert requests[0].headers["Authorization"] == "Bearer token-value"


@pytest.mark.usefixtures("apim_env")
def test_managed_identity_is_used_when_only_the_identity_endpoint_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Container Apps injects IDENTITY_ENDPOINT without AZURE_CLIENT_ID."""
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "https://identity.local/token")
    built: list[str | None] = []

    class _Managed:
        def __init__(self, client_id: str | None = None) -> None:
            built.append(client_id)

    monkeypatch.setattr(connector_credentials, "ManagedIdentityCredential", _Managed)
    monkeypatch.setattr(
        connector_credentials,
        "DefaultAzureCredential",
        lambda: pytest.fail("managed identity was available"),
    )

    assert isinstance(connector_credentials._azure_credential(), _Managed)
    assert built == [None]


def test_missing_api_management_configuration_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_API_MANAGEMENT_NAME", raising=False)
    with pytest.raises(ConnectorCredentialNotConfiguredError):
        set_connector_api_key("nv-1", "secret")
