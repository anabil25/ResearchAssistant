"""The platform's single Azure credential and token surface.

Every component authenticates to Azure the same way -- managed identity when the
platform provides one, developer credentials otherwise -- so that resolution
lives here rather than being re-derived per module. Two properties are easy to
get wrong independently and are therefore fixed here:

* Managed identity is detected from ``AZURE_CLIENT_ID`` *or* the platform-injected
  ``IDENTITY_ENDPOINT``/``MSI_ENDPOINT``. Checking only the client id makes a
  deployment silently fall back to developer credentials.
* Credentials do not cache tokens; ``BearerTokenCredentialPolicy`` does. Passing a
  credential to an Azure SDK client is therefore already cached, but calling
  ``get_token`` by hand is not. :func:`token_provider` supplies the cached path,
  and both the credential and the provider are memoized so the cache survives
  across requests.

``agents/shared/credentials.py`` is a deliberate vendored twin of the credential
half: Hosted Agent containers are built from ``agents/requirements.txt`` alone and
cannot import this package.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from threading import Lock
from typing import cast

from azure.core.credentials import TokenCredential
from azure.core.credentials_async import AsyncTokenCredential

__all__ = [
    "async_azure_credential",
    "async_token_provider",
    "azure_credential",
    "managed_identity_client_id",
    "token_provider",
]

_LOCK = Lock()
_CREDENTIALS: dict[tuple[bool, str | None], object] = {}
_TOKEN_PROVIDERS: dict[tuple[bool, str | None, str], Callable[..., object]] = {}


def managed_identity_client_id(client_id: str | None = None) -> str | None:
    """Return the client id to authenticate with, or ``None`` for system-assigned."""
    return client_id or os.environ.get("AZURE_CLIENT_ID") or None


def _managed_identity_is_available(client_id: str | None) -> bool:
    return bool(
        client_id or os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")
    )


def azure_credential(client_id: str | None = None) -> TokenCredential:
    """Return the process-wide credential for this deployment.

    The instance is cached because discarding it discards the token cache with it.
    """
    resolved = managed_identity_client_id(client_id)
    key = (False, resolved)
    cached = _CREDENTIALS.get(key)
    if cached is None:
        with _LOCK:
            cached = _CREDENTIALS.get(key)
            if cached is None:
                from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

                cached = (
                    ManagedIdentityCredential(client_id=resolved)
                    if _managed_identity_is_available(resolved)
                    else DefaultAzureCredential()
                )
                _CREDENTIALS[key] = cached
    return cast(TokenCredential, cached)


def async_azure_credential(client_id: str | None = None) -> AsyncTokenCredential:
    """Async counterpart of :func:`azure_credential`."""
    resolved = managed_identity_client_id(client_id)
    key = (True, resolved)
    cached = _CREDENTIALS.get(key)
    if cached is None:
        with _LOCK:
            cached = _CREDENTIALS.get(key)
            if cached is None:
                from azure.identity.aio import (
                    DefaultAzureCredential,
                    ManagedIdentityCredential,
                )

                cached = (
                    ManagedIdentityCredential(client_id=resolved)
                    if _managed_identity_is_available(resolved)
                    else DefaultAzureCredential()
                )
                _CREDENTIALS[key] = cached
    return cast(AsyncTokenCredential, cached)


def token_provider(scope: str, *, client_id: str | None = None) -> Callable[[], str]:
    """Return a cached bearer-token callable for ``scope``.

    Use this instead of ``credential.get_token`` whenever a token is needed outside
    an Azure SDK client, such as for a raw HTTP call.
    """
    resolved = managed_identity_client_id(client_id)
    key = (False, resolved, scope)
    cached = _TOKEN_PROVIDERS.get(key)
    if cached is None:
        # Resolved before the lock is taken: this caches under the same lock.
        credential = azure_credential(resolved)
        with _LOCK:
            cached = _TOKEN_PROVIDERS.get(key)
            if cached is None:
                from azure.identity import get_bearer_token_provider

                cached = get_bearer_token_provider(credential, scope)
                _TOKEN_PROVIDERS[key] = cached
    return cast(Callable[[], str], cached)


def async_token_provider(scope: str, *, client_id: str | None = None) -> Callable[[], object]:
    """Async counterpart of :func:`token_provider`; the returned callable is awaitable."""
    resolved = managed_identity_client_id(client_id)
    key = (True, resolved, scope)
    cached = _TOKEN_PROVIDERS.get(key)
    if cached is None:
        # Resolved before the lock is taken: this caches under the same lock.
        credential = async_azure_credential(resolved)
        with _LOCK:
            cached = _TOKEN_PROVIDERS.get(key)
            if cached is None:
                from azure.identity.aio import get_bearer_token_provider

                cached = get_bearer_token_provider(credential, scope)
                _TOKEN_PROVIDERS[key] = cached
    return cached
