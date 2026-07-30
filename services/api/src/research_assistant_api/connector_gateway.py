from __future__ import annotations

from typing import Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import ManagedIdentityCredential, get_bearer_token_provider
from pydantic import ValidationError
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability

from research_assistant_api.config import Settings


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
        self._token = (
            get_bearer_token_provider(credential, token_scope)
            if credential is not None and token_scope
            else None
        )
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
        del capability
        try:
            normalized_source = PublicConnectorSource(source)
        except ValueError as exc:
            raise ConnectorGatewayError(f"Connector {source} is not in the public gateway contract.") from exc
        headers: dict[str, str] = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {await self._token()}"
        try:
            response = await self._client.get(
                f"v1/connectors/{normalized_source.value}/search",
                params={
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
