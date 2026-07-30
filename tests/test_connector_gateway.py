from __future__ import annotations

from time import time
from types import TracebackType
from typing import Self

import httpx
import pytest
import research_assistant_api.connector_gateway as connector_gateway
from azure.core.credentials import AccessToken
from research_assistant_api.config import Settings
from research_assistant_api.connector_gateway import (
    ConnectorGatewayError,
    ConnectorGatewayNotConfiguredError,
    DisabledConnectorGateway,
    HttpConnectorGateway,
    build_connector_gateway,
)
from research_assistant_core.connector_gateway import PublicConnectorSource
from research_assistant_core.models import Capability


class FakeCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.closed = False

    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: object,
    ) -> AccessToken:
        del claims, tenant_id, enable_cae, kwargs
        self.scopes.extend(scopes)
        return AccessToken("test-token", int(time()) + 3600)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        del exc_type, exc_value, traceback


@pytest.mark.asyncio
async def test_gateway_routes_source_specific_endpoint_and_uses_managed_identity_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "source": "grants_gov",
                "query": "open science",
                "records": [{"id": "grant-1"}],
                "terms_url": "https://grants.gov/terms",
                "retrieved_from": "https://api.grants.gov/v1/api/search2",
                "warnings": [],
            },
        )

    credential = FakeCredential()
    client = httpx.AsyncClient(
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpConnectorGateway(
        "https://gateway.example",
        credential=credential,
        token_scope="https://management.azure.com/.default",
        client=client,
    )

    result = await gateway.search(
        Capability.GRANT,
        "grants_gov",
        "open science",
        limit=3,
    )
    await gateway.close()
    await client.aclose()

    assert result.records == [{"id": "grant-1"}]
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/v1/connectors/grants_gov/search"
    assert dict(requests[0].url.params) == {"query": "open science", "limit": "3"}
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert credential.scopes == ["https://management.azure.com/.default"]
    assert credential.closed is True


@pytest.mark.asyncio
async def test_gateway_rejects_unknown_source_without_network_call() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpConnectorGateway("https://gateway.example", client=client)

    with pytest.raises(ConnectorGatewayError, match="not in the public gateway"):
        await gateway.search(Capability.GRANT, "arbitrary_scraper", "query", limit=3)
    await client.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_gateway_does_not_select_a_domain_route_from_capability() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "source": "pubmed",
                "query": "query",
                "records": [],
                "terms_url": "https://terms.example/pubmed",
                "retrieved_from": "https://api.example/pubmed",
                "warnings": [],
            },
        )

    client = httpx.AsyncClient(
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpConnectorGateway("https://gateway.example", client=client)

    result = await gateway.search(Capability.DATASET, "pubmed", "query", limit=3)
    await client.aclose()

    assert result.source is PublicConnectorSource.PUBMED
    assert requests[0].url.path == "/v1/connectors/pubmed/search"


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_or_failed_responses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"source": "pubmed", "records": []})

    client = httpx.AsyncClient(
        base_url="https://gateway.example",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpConnectorGateway("https://gateway.example", client=client)

    with pytest.raises(ConnectorGatewayError, match="gateway request"):
        await gateway.search(Capability.LITERATURE, "pubmed", "query", limit=3)
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_closes_owned_client_without_credential() -> None:
    gateway = HttpConnectorGateway("https://gateway.example")
    closed = False

    async def fake_aclose() -> None:
        nonlocal closed
        closed = True

    gateway._client.aclose = fake_aclose  # type: ignore[method-assign]

    await gateway.close()

    assert closed is True


def test_gateway_configuration_requires_https_or_local_loopback() -> None:
    disabled = build_connector_gateway(Settings())
    local = build_connector_gateway(
        Settings(connector_gateway_url="http://127.0.0.1:8200")
    )

    assert isinstance(disabled, DisabledConnectorGateway)
    assert isinstance(local, HttpConnectorGateway)
    with pytest.raises(ValueError, match="must use HTTPS"):
        Settings(connector_gateway_url="http://gateway.example")


def test_gateway_configuration_builds_managed_identity_for_token_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str | None] = {}

    def _credential(client_id: str | None = None) -> object:
        captured["client_id"] = client_id
        return object()

    monkeypatch.setattr(
        connector_gateway,
        "async_azure_credential",
        _credential,
    )

    gateway = build_connector_gateway(
        Settings(
            connector_gateway_url="https://gateway.example",
            connector_gateway_token_scope="api://gateway/.default",
            managed_identity_client_id="managed-client",
        )
    )

    assert isinstance(gateway, HttpConnectorGateway)
    assert captured == {"client_id": "managed-client"}


@pytest.mark.asyncio
async def test_disabled_gateway_reports_configuration_instead_of_provider_failure() -> None:
    gateway = DisabledConnectorGateway()

    with pytest.raises(
        ConnectorGatewayNotConfiguredError,
        match="gateway is not configured",
    ):
        await gateway.search(Capability.LITERATURE, "pubmed", "query", limit=1)
