from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Annotated, cast
from urllib.error import HTTPError, URLError
from uuid import uuid4
from xml.etree.ElementTree import ParseError

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from research_assistant_connectors import ResearchConnectorRegistry, connector_catalog
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

from research_assistant_connector_adapter.auth import (
    GatewayAuthorizationError,
    build_gateway_validator,
)

logger = logging.getLogger(__name__)
RegistryFactory = Callable[[], ResearchConnectorRegistry]

app = FastAPI(
    title="Research Assistant Connector Adapter",
    description="Bounded public metadata operations for APIM and MCP exposure.",
    version="1.0.0",
)
app.state.registry_factory = ResearchConnectorRegistry
app.state.gateway_validator = build_gateway_validator()


@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid4().hex[:16]}"
    if request.url.path.startswith("/v1/") and request.app.state.gateway_validator:
        try:
            request.app.state.gateway_validator.validate(
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
