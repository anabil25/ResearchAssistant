"""The platform's single Azure credential surface.

Seventeen modules now authenticate through this one path, so the two properties
that were previously re-derived (and re-derived inconsistently) per module are
pinned here: how managed identity is detected, and that tokens stay cached.
"""

from __future__ import annotations

import azure.identity
import azure.identity.aio
import pytest
from azure.core.credentials import AccessToken
from research_assistant_core import azure_auth


@pytest.fixture(autouse=True)
def _isolate_credential_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AZURE_CLIENT_ID", "IDENTITY_ENDPOINT", "MSI_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    azure_auth.reset_credential_cache()
    yield
    azure_auth.reset_credential_cache()


class _Managed:
    def __init__(self, client_id: str | None = None) -> None:
        self.client_id = client_id


class _Default:
    def __init__(self) -> None:
        pass


def _patch_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", _Managed)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", _Default)


def test_developer_credentials_are_used_without_a_platform_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch)
    assert isinstance(azure_auth.azure_credential(), _Default)


@pytest.mark.parametrize(
    ("variable", "value", "expected_client_id"),
    [
        ("AZURE_CLIENT_ID", "client-1", "client-1"),
        # Container Apps injects these without AZURE_CLIENT_ID; checking only the
        # client id makes a deployment silently fall back to developer credentials.
        ("IDENTITY_ENDPOINT", "https://identity.local/token", None),
        ("MSI_ENDPOINT", "https://msi.local/token", None),
    ],
)
def test_managed_identity_is_detected_from_any_platform_signal(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    expected_client_id: str | None,
) -> None:
    _patch_sync(monkeypatch)
    monkeypatch.setenv(variable, value)

    credential = azure_auth.azure_credential()

    assert isinstance(credential, _Managed)
    assert credential.client_id == expected_client_id


def test_an_explicit_client_id_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch)
    monkeypatch.setenv("AZURE_CLIENT_ID", "from-env")

    assert azure_auth.azure_credential("explicit").client_id == "explicit"


def test_the_credential_is_reused_because_it_carries_the_token_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch)
    assert azure_auth.azure_credential() is azure_auth.azure_credential()


def test_distinct_client_ids_get_distinct_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_sync(monkeypatch)
    assert azure_auth.azure_credential("a") is not azure_auth.azure_credential("b")


def test_token_provider_caches_so_a_token_is_acquired_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential does not cache on its own -- the bearer provider does."""
    calls = 0

    class _Counting:
        def __init__(self, client_id: str | None = None) -> None:
            pass

        def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            nonlocal calls
            calls += 1
            return AccessToken("token-value", 4_102_444_800)

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", _Counting)
    monkeypatch.setenv("IDENTITY_ENDPOINT", "https://identity.local/token")

    provider = azure_auth.token_provider("https://management.azure.com/.default")
    tokens = [provider() for _ in range(3)]

    assert tokens == ["token-value"] * 3
    assert calls == 1


def test_token_providers_are_shared_per_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Also covers the first call resolving its credential without self-deadlocking.

    Both caches are guarded by one non-reentrant lock, so building a provider while
    holding it used to block forever whenever the credential was not already cached.
    """
    _patch_sync(monkeypatch)
    scope = "https://management.azure.com/.default"

    assert azure_auth.token_provider(scope) is azure_auth.token_provider(scope)
    assert azure_auth.token_provider(scope) is not azure_auth.token_provider("other/.default")


def test_async_credentials_are_cached_separately_from_sync_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing one cache key would hand an async credential to a sync client."""
    _patch_sync(monkeypatch)

    class _AsyncDefault:
        def __init__(self) -> None:
            pass

    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", _AsyncDefault)

    assert isinstance(azure_auth.azure_credential(), _Default)
    assert isinstance(azure_auth.async_azure_credential(), _AsyncDefault)
    assert azure_auth.async_azure_credential() is azure_auth.async_azure_credential()
