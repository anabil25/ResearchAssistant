from __future__ import annotations

import os
from functools import cache

from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.identity.aio import ManagedIdentityCredential as AsyncManagedIdentityCredential


def _managed_identity_client_id(client_id: str | None) -> str | None:
    client_id = client_id or os.getenv("AZURE_CLIENT_ID")
    if client_id or os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"):
        return client_id or ""
    return None


def get_credential(client_id: str | None = None) -> TokenCredential:
    resolved = _managed_identity_client_id(client_id)
    if resolved is None:
        return DefaultAzureCredential()
    return ManagedIdentityCredential(client_id=resolved or None)


@cache
def get_async_credential(client_id: str | None = None) -> AsyncTokenCredential:
    """The async twin of :func:`get_credential`, one instance per process.

    Anything acquiring a token from inside an event loop must use this. The sync
    credentials issue blocking HTTP calls, which stall every other task on the
    loop for the duration of the token request.

    Cached because each async credential owns an ``aiohttp`` session and its own
    token cache; building one per client would multiply both.
    """
    resolved = _managed_identity_client_id(client_id)
    if resolved is None:
        return AsyncDefaultAzureCredential()
    return AsyncManagedIdentityCredential(client_id=resolved or None)
