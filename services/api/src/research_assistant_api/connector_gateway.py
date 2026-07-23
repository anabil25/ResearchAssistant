from __future__ import annotations

from typing import Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import ManagedIdentityCredential
from pydantic import ValidationError
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability

from research_assistant_api.config import Settings

_CAPABILITY_PATHS = {
    Capability.LITERATURE: "v1/literature/search",
    Capability.GRANT: "v1/grants/search",
    Capability.MATCHING: "v1/matching/search",
}


class ConnectorGatewayError(RuntimeError):
    pass


class ConnectorGatewayNotConfiguredError(ConnectorGatewayError):
    pass


class ConnectorGateway(Protocol):
    async def search(
        self,
        capability: Capability,
        source: str,
        query: str,
        *,
        limit: int,
    ) -> ConnectorSearchResponse: ...

    async def close(self) -> None: ...


class HttpConnectorGateway:
    def __init__(
        self,
        base_url: str,
        *,
        credential: AsyncTokenCredential | None = None,
        token_scope: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credential = credential
        self._token_scope = token_scope
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
        )

    async def search(
        self,
        capability: Capability,
        source: str,
        query: str,
        *,
        limit: int,
    ) -> ConnectorSearchResponse:
        path = _CAPABILITY_PATHS.get(capability)
        if path is None:
            raise ConnectorGatewayError(f"Capability {capability.value} has no public connector gateway.")
        try:
            normalized_source = PublicConnectorSource(source)
        except ValueError as exc:
            raise ConnectorGatewayError(f"Connector {source} is not in the public gateway contract.") from exc
        headers: dict[str, str] = {}
        if self._credential and self._token_scope:
            token = await self._credential.get_token(self._token_scope)
            headers["Authorization"] = f"Bearer {token.token}"
        try:
            response = await self._client.post(
                path,
                json={
                    "source": normalized_source.value,
                    "query": query,
                    "limit": limit,
                },
                headers=headers,
            )
            response.raise_for_status()
            return ConnectorSearchResponse.model_validate(response.json())
        except (httpx.HTTPError, ValidationError, ValueError) as exc:
            raise ConnectorGatewayError(
                f"Connector gateway request for {normalized_source.value} failed."
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._credential:
            await self._credential.close()


class DisabledConnectorGateway:
    async def search(
        self,
        capability: Capability,
        source: str,
        query: str,
        *,
        limit: int,
    ) -> ConnectorSearchResponse:
        del capability, source, query, limit
        raise ConnectorGatewayNotConfiguredError(
            "The connector gateway is not configured."
        )

    async def close(self) -> None:
        return None


def build_connector_gateway(settings: Settings) -> ConnectorGateway:
    if not settings.connector_gateway_url:
        return DisabledConnectorGateway()
    credential: AsyncTokenCredential | None = None
    if settings.connector_gateway_token_scope:
        credential = ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return HttpConnectorGateway(
        settings.connector_gateway_url,
        credential=credential,
        token_scope=settings.connector_gateway_token_scope,
    )
