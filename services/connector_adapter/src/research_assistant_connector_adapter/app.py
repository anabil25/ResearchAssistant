from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Annotated, cast
from urllib.error import HTTPError, URLError
from uuid import uuid4
from xml.etree.ElementTree import ParseError

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from research_assistant_connectors import ResearchConnectorRegistry, connector_catalog
from research_assistant_connectors.providers import ProviderError
from research_assistant_connectors.providers._http import base64_encoded_length
from research_assistant_connectors.providers.config import DEFAULT_UPLOAD_BYTES
from research_assistant_core.connector_gateway import (
    ConnectorCatalogResponse,
    ConnectorDescriptor,
    ConnectorHealthResponse,
    ConnectorSearchResponse,
    GrantSearchRequest,
    LiteratureSearchRequest,
    MatchingSearchRequest,
    PublicConnectorSource,
)
from starlette.middleware.base import RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from research_assistant_connector_adapter.auth import (
    GatewayAuthorizationError,
    build_gateway_validator,
)
from research_assistant_connector_adapter.provider_api import (
    provider_error_response,
)
from research_assistant_connector_adapter.provider_api import (
    router as provider_router,
)
from research_assistant_connector_adapter.provider_runtime import (
    build_provider_runtime,
)

logger = logging.getLogger(__name__)
RegistryFactory = Callable[[], ResearchConnectorRegistry]
REQUEST_BODY_LIMIT_ENV = "CONNECTOR_ADAPTER_MAX_REQUEST_BODY_BYTES"
REQUEST_JSON_OVERHEAD_BYTES = 64 * 1024
DEFAULT_MAX_REQUEST_BODY_BYTES = (
    base64_encoded_length(DEFAULT_UPLOAD_BYTES) + REQUEST_JSON_OVERHEAD_BYTES
)
_REQUEST_TOO_LARGE_BODY = b'{"detail":"Request body too large."}'


class RequestBodyTooLargeError(Exception):
    pass


def request_body_limit_from_environment() -> int:
    configured = os.getenv(REQUEST_BODY_LIMIT_ENV)
    if configured is None:
        return DEFAULT_MAX_REQUEST_BODY_BYTES
    try:
        limit = int(configured)
    except ValueError as exc:
        raise ValueError(f"{REQUEST_BODY_LIMIT_ENV} must be an integer") from exc
    if limit <= 0:
        raise ValueError(f"{REQUEST_BODY_LIMIT_ENV} must be positive")
    return limit


async def _send_request_too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_REQUEST_TOO_LARGE_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REQUEST_TOO_LARGE_BODY})


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if max_body_bytes <= 0:
            raise ValueError("Request body limit must be positive")
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        for name, value in scope.get("headers", ()):
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(value)
            except ValueError:
                continue
            if declared_length > self._max_body_bytes:
                await _send_request_too_large(send)
                return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.disconnect":
                scope.setdefault("state", {})["provider_cancelled"] = True
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_body_bytes:
                    raise RequestBodyTooLargeError
            return message

        try:
            await self._app(scope, limited_receive, send)
        except RequestBodyTooLargeError:
            await _send_request_too_large(send)


app = FastAPI(
    title="Research Assistant Connector Adapter",
    description="Bounded public metadata operations for APIM and MCP exposure.",
    version="1.0.0",
)
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=request_body_limit_from_environment(),
)
app.state.registry_factory = ResearchConnectorRegistry
app.state.gateway_validator = build_gateway_validator()
app.state.provider_runtime = build_provider_runtime()
app.state.provider_service = app.state.provider_runtime.service
app.include_router(provider_router, include_in_schema=False)


@app.exception_handler(ProviderError)
async def handle_provider_error(
    _request: Request,
    error: ProviderError,
) -> JSONResponse:
    return provider_error_response(error)


@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid4().hex[:16]}"
    request.state.request_id = request_id
    request.state.provider_cancelled = False
    if request.url.path.startswith("/v1/") and request.app.state.gateway_validator:
        try:
            request.state.authenticated_principal_id = request.app.state.gateway_validator.validate(
                request.headers.get("Authorization")
            )
        except GatewayAuthorizationError as exc:
            return JSONResponse(
                status_code=401,
                content={"detail": str(exc)},
                headers={
                    "X-Request-ID": request_id,
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


def registry_factory(request: Request) -> RegistryFactory:
    return cast(RegistryFactory, request.app.state.registry_factory)


def _search(
    *,
    source: PublicConnectorSource,
    query: str,
    limit: int,
    factory: RegistryFactory,
) -> ConnectorSearchResponse:
    registry = factory()
    try:
        result = registry.search(source.value, query, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (
        httpx.HTTPError,
        HTTPError,
        URLError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
        ParseError,
    ) as exc:
        logger.warning("Connector %s failed: %s", source.value, exc)
        raise HTTPException(status_code=502, detail=f"Connector {source.value} is unavailable.") from exc
    finally:
        registry.close()
    return ConnectorSearchResponse.model_validate(
        {
            "source": result.source,
            "query": result.query,
            "records": result.records,
            "terms_url": result.terms_url,
            "retrieved_from": result.retrieved_from,
            "warnings": result.warnings,
        }
    )


@app.get("/health", response_model=ConnectorHealthResponse, tags=["operations"])
def health() -> ConnectorHealthResponse:
    return ConnectorHealthResponse(status="healthy")


@app.get("/ready", response_model=ConnectorHealthResponse, tags=["operations"])
def ready() -> ConnectorHealthResponse:
    return ConnectorHealthResponse(status="ready")


@app.get(
    "/v1/connectors",
    response_model=ConnectorCatalogResponse,
    operation_id="listResearchConnectors",
    tags=["catalog"],
)
def connectors() -> ConnectorCatalogResponse:
    capabilities = {
        "pubmed": ["literature"],
        "europe_pmc": ["literature"],
        "crossref": ["literature", "grant"],
        "openalex": ["literature", "grant", "matching"],
        "arxiv": ["literature"],
        "clinical_trials": ["literature"],
        "grants_gov": ["grant"],
        "nih_reporter": ["grant", "matching"],
        "datacite": ["literature"],
        "orcid": ["matching"],
        "ror": ["matching"],
        "semantic_scholar": ["literature"],
    }
    return ConnectorCatalogResponse(
        connectors=[
            ConnectorDescriptor(
                id=PublicConnectorSource(source),
                description=description,
                capabilities=capabilities[source],
            )
            for source, description in connector_catalog().items()
        ]
    )


@app.post(
    "/v1/literature/search",
    response_model=ConnectorSearchResponse,
    operation_id="searchLiteratureMetadata",
    tags=["literature"],
)
def search_literature(
    payload: LiteratureSearchRequest,
    factory: Annotated[RegistryFactory, Depends(registry_factory)],
) -> ConnectorSearchResponse:
    return _search(
        source=PublicConnectorSource(payload.source.value),
        query=payload.query,
        limit=payload.limit,
        factory=factory,
    )


@app.post(
    "/v1/grants/search",
    response_model=ConnectorSearchResponse,
    operation_id="searchGrantOpportunities",
    tags=["grants"],
)
def search_grants(
    payload: GrantSearchRequest,
    factory: Annotated[RegistryFactory, Depends(registry_factory)],
) -> ConnectorSearchResponse:
    return _search(
        source=PublicConnectorSource(payload.source.value),
        query=payload.query,
        limit=payload.limit,
        factory=factory,
    )


@app.post(
    "/v1/matching/search",
    response_model=ConnectorSearchResponse,
    operation_id="searchMatchingMetadata",
    tags=["matching"],
)
def search_matching(
    payload: MatchingSearchRequest,
    factory: Annotated[RegistryFactory, Depends(registry_factory)],
) -> ConnectorSearchResponse:
    return _search(
        source=PublicConnectorSource(payload.source.value),
        query=payload.query,
        limit=payload.limit,
        factory=factory,
    )
