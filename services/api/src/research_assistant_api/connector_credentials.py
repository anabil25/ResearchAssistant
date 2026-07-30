"""Persist optional connector API keys as APIM named values.

The gateway is the only component that needs the secret, so it is written
straight to API Management and never stored in the workspace or returned.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from weakref import WeakKeyDictionary

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from research_assistant_core.connector_catalog import UNCONFIGURED_CREDENTIAL

ARM_SCOPE = "https://management.azure.com/.default"
APIM_API_VERSION = "2024-05-01"


class ConnectorCredentialError(RuntimeError):
    """Raised when a connector secret cannot be written to the gateway."""


class ConnectorCredentialNotConfiguredError(ConnectorCredentialError):
    """Raised when the deployment has no API Management target configured."""


def _azure_credential() -> TokenCredential:
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT"):
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


_DEFAULT_CREDENTIAL: TokenCredential | None = None
_TOKEN_PROVIDERS: WeakKeyDictionary[TokenCredential, Callable[[], str]] = WeakKeyDictionary()


def _arm_token(credential: TokenCredential | None) -> str:
    """Return an ARM token, reusing the cached one until it expires.

    Credentials do not cache on their own -- caching lives in the bearer token
    provider -- so both the credential and the provider must outlive the request.
    """
    global _DEFAULT_CREDENTIAL
    if credential is None:
        if _DEFAULT_CREDENTIAL is None:
            _DEFAULT_CREDENTIAL = _azure_credential()
        credential = _DEFAULT_CREDENTIAL
    provider = _TOKEN_PROVIDERS.get(credential)
    if provider is None:
        provider = get_bearer_token_provider(credential, ARM_SCOPE)
        _TOKEN_PROVIDERS[credential] = provider
    return provider()


def _named_value_url(named_value: str) -> str:
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID")
    resource_group = os.environ.get("AZURE_RESOURCE_GROUP")
    service = os.environ.get("AZURE_API_MANAGEMENT_NAME")
    if not (subscription and resource_group and service):
        raise ConnectorCredentialNotConfiguredError(
            "API Management is not configured for this deployment, so connector "
            "keys cannot be stored."
        )
    return (
        f"https://management.azure.com/subscriptions/{subscription}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{service}"
        f"/namedValues/{named_value}?api-version={APIM_API_VERSION}"
    )


def set_connector_api_key(
    named_value: str,
    api_key: str | None,
    *,
    credential: TokenCredential | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Upsert ``named_value``; ``None`` restores the unconfigured sentinel."""
    url = _named_value_url(named_value)
    token = _arm_token(credential)
    payload = {
        "properties": {
            "displayName": named_value,
            "value": api_key or UNCONFIGURED_CREDENTIAL,
            "secret": True,
            "tags": ["connector"],
        }
    }
    owned_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0))
    try:
        response = http.put(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "If-Match": "*",
            },
        )
        if response.status_code >= 400:
            raise ConnectorCredentialError(
                f"API Management rejected the connector key update "
                f"(HTTP {response.status_code})."
            )
    except httpx.HTTPError as exc:
        raise ConnectorCredentialError(
            "API Management could not be reached to store the connector key."
        ) from exc
    finally:
        if owned_client:
            http.close()
