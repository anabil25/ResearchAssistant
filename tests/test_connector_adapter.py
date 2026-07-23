from __future__ import annotations

import json
from collections.abc import Iterator
from http.client import HTTPMessage
from json import JSONDecodeError
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError, URLError
from xml.etree.ElementTree import ParseError

import httpx
import pytest
from fastapi.testclient import TestClient
from research_assistant_connector_adapter.app import app
from research_assistant_connectors import ConnectorResult

ROOT = Path(__file__).resolve().parents[1]


class FakeRegistry:
    calls: ClassVar[list[tuple[str, str, int]]] = []
    closed: ClassVar[int] = 0

    def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
        self.calls.append((source, query, limit))
        return ConnectorResult(
            source=source,
            query=query,
            records=[{"id": f"{source}-1", "title": "Bounded metadata"}],
            terms_url=f"https://terms.example/{source}",
            retrieved_from=f"https://provider.example/{source}",
        )

    def close(self) -> None:
        type(self).closed += 1


@pytest.fixture
def client() -> Iterator[TestClient]:
    FakeRegistry.calls = []
    FakeRegistry.closed = 0
    app.state.registry_factory = FakeRegistry
    app.state.gateway_validator = None
    with TestClient(app) as test_client:
        yield test_client


def test_adapter_exposes_narrow_stable_openapi_operations(client: TestClient) -> None:
    specification = client.get("/openapi.json")

    assert specification.status_code == 200
    operations = {
        details[method]["operationId"]
        for path, details in specification.json()["paths"].items()
        for method in details
        if path.startswith("/v1/")
    }
    assert operations == {
        "listResearchConnectors",
        "searchLiteratureMetadata",
        "searchGrantOpportunities",
        "searchMatchingMetadata",
    }
    committed = json.loads(
        (
            ROOT
            / "packages"
            / "contracts"
            / "connector-adapter-openapi.json"
        ).read_text(encoding="utf-8")
    )
    assert committed == specification.json()


def test_literature_search_returns_typed_metadata_and_closes_client(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/literature/search",
        json={"source": "pubmed", "query": "auditable synthesis", "limit": 2},
    )

    assert response.status_code == 200
    assert response.json()["records"] == [
        {"id": "pubmed-1", "title": "Bounded metadata"}
    ]
    assert response.json()["notice"].startswith("Metadata only")
    assert FakeRegistry.calls == [("pubmed", "auditable synthesis", 2)]
    assert FakeRegistry.closed == 1


def test_capability_routes_reject_cross_boundary_sources_and_extra_fields(
    client: TestClient,
) -> None:
    wrong_source = client.post(
        "/v1/grants/search",
        json={"source": "pubmed", "query": "open opportunity", "limit": 2},
    )
    extra_field = client.post(
        "/v1/matching/search",
        json={
            "source": "ror",
            "query": "research facility",
            "limit": 2,
            "destination": "https://attacker.example",
        },
    )

    assert wrong_source.status_code == 422
    assert extra_field.status_code == 422
    assert FakeRegistry.calls == []


def test_provider_failure_is_sanitized_at_the_adapter_boundary(
    client: TestClient,
) -> None:
    class FailingRegistry(FakeRegistry):
        def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
            request = httpx.Request("GET", "https://provider.example/private")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError(
                "provider secret response",
                request=request,
                response=response,
            )

    app.state.registry_factory = FailingRegistry
    response = client.post(
        "/v1/grants/search",
        json={"source": "grants_gov", "query": "open opportunity", "limit": 1},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Connector grants_gov is unavailable."}
    assert "secret" not in response.text
    assert FailingRegistry.closed == 1


def test_malformed_provider_json_is_sanitized_as_an_upstream_failure(
    client: TestClient,
) -> None:
    class MalformedRegistry(FakeRegistry):
        def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
            del source, query, limit
            raise JSONDecodeError("secret provider payload", "{", 1)

    app.state.registry_factory = MalformedRegistry
    response = client.post(
        "/v1/grants/search",
        json={"source": "grants_gov", "query": "open opportunity", "limit": 1},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Connector grants_gov is unavailable."}
    assert "secret" not in response.text
    assert MalformedRegistry.closed == 1


@pytest.mark.parametrize(
    "provider_error",
    [
        HTTPError(
            "https://provider.example/private",
            503,
            "secret HTTP response",
            HTTPMessage(),
            None,
        ),
        URLError("secret URL failure"),
        OSError("secret socket failure"),
        TimeoutError("secret provider timeout"),
        ParseError("secret XML payload"),
    ],
)
def test_all_supported_provider_failures_are_sanitized(
    client: TestClient,
    provider_error: Exception,
) -> None:
    class FailingRegistry(FakeRegistry):
        def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
            del source, query, limit
            raise provider_error

    app.state.registry_factory = FailingRegistry
    response = client.post(
        "/v1/literature/search",
        json={"source": "pubmed", "query": "valid query", "limit": 1},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Connector pubmed is unavailable."}
    assert "secret" not in response.text
    assert FailingRegistry.closed == 1


def test_registry_validation_errors_remain_client_errors(client: TestClient) -> None:
    class RejectingRegistry(FakeRegistry):
        def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
            del source, query, limit
            raise ValueError("Query violates the connector contract.")

    app.state.registry_factory = RejectingRegistry
    response = client.post(
        "/v1/literature/search",
        json={"source": "pubmed", "query": "valid query", "limit": 1},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Query violates the connector contract."}
    assert RejectingRegistry.closed == 1


def test_catalog_and_health_do_not_call_external_providers(client: TestClient) -> None:
    health = client.get("/health")
    ready = client.get("/ready")
    catalog = client.get("/v1/connectors")

    assert health.json()["status"] == "healthy"
    assert ready.json()["status"] == "ready"
    assert len(catalog.json()["connectors"]) == 12
    assert FakeRegistry.calls == []


def test_all_capability_routes_forward_their_typed_source(client: TestClient) -> None:
    grant = client.post(
        "/v1/grants/search",
        json={"source": "grants_gov", "query": "open opportunity", "limit": 1},
    )
    matching = client.post(
        "/v1/matching/search",
        json={"source": "ror", "query": "imaging facility", "limit": 4},
    )

    assert grant.status_code == 200
    assert matching.status_code == 200
    assert FakeRegistry.calls == [
        ("grants_gov", "open opportunity", 1),
        ("ror", "imaging facility", 4),
    ]


def test_gateway_authentication_middleware_rejects_and_accepts_callers(
    client: TestClient,
) -> None:
    class Validator:
        def __init__(self, *, allowed: bool) -> None:
            self.allowed = allowed
            self.headers: list[str | None] = []

        def validate(self, authorization: str | None) -> None:
            self.headers.append(authorization)
            if not self.allowed:
                from research_assistant_connector_adapter.auth import (
                    GatewayAuthorizationError,
                )

                raise GatewayAuthorizationError("The caller is not authorized.")

    rejecting = Validator(allowed=False)
    app.state.gateway_validator = rejecting
    denied = client.get("/v1/connectors", headers={"X-Request-ID": "request-denied"})

    assert denied.status_code == 401
    assert denied.json() == {"detail": "The caller is not authorized."}
    assert denied.headers["X-Request-ID"] == "request-denied"
    assert denied.headers["Cache-Control"] == "no-store"

    accepting = Validator(allowed=True)
    app.state.gateway_validator = accepting
    allowed = client.get(
        "/v1/connectors",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert allowed.status_code == 200
    assert accepting.headers == ["Bearer signed-token"]


def test_product_api_cannot_import_provider_clients_directly() -> None:
    api_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(
            (
                ROOT
                / "services"
                / "api"
                / "src"
                / "research_assistant_api"
            ).glob("*.py")
        )
    )
    api_project = (
        ROOT / "services" / "api" / "pyproject.toml"
    ).read_text(encoding="utf-8")

    assert "research_assistant_connectors" not in api_source
    assert '"research-assistant-connectors"' not in api_project
