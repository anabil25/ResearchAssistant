from __future__ import annotations

import json
from typing import Any

import azure.functions as func
import httpx
import pytest
from research_assistant_connectors import ConnectorResult

from services.connector_functions import function_app


def _request(
    *,
    source: str,
    params: dict[str, str],
) -> func.HttpRequest:
    return func.HttpRequest(
        method="GET",
        url=f"https://functions.example/api/v1/connectors/{source}",
        headers={},
        params=params,
        route_params={"source": source},
        body=b"",
    )


class FakeRegistry:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, int | None]] = []
        self.closed = False

    def search(self, source: str, query: str, *, limit: int) -> ConnectorResult:
        self.calls.append((source, query, limit))
        if self.error:
            raise self.error
        return ConnectorResult(
            source=source,
            query=query,
            records=[{"id": "record-1"}],
            terms_url="https://provider.example/terms",
            retrieved_from="https://provider.example/api",
        )

    def lookup(self, source: str, identifier: str) -> ConnectorResult:
        self.calls.append((source, identifier, None))
        if self.error:
            raise self.error
        return ConnectorResult(
            source=source,
            query=identifier,
            records=[{"id": identifier}],
            terms_url="https://provider.example/terms",
            retrieved_from="https://provider.example/api",
        )

    def close(self) -> None:
        self.closed = True


def _body(response: func.HttpResponse) -> dict[str, Any]:
    payload = json.loads(response.get_body())
    assert isinstance(payload, dict)
    return payload


def test_search_returns_normalized_connector_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry()
    monkeypatch.setattr(function_app, "registry_factory", lambda: registry)

    response = function_app.connector_search(
        _request(source="pubmed", params={"query": "gene therapy", "limit": "3"})
    )

    assert response.status_code == 200
    assert registry.calls == [("pubmed", "gene therapy", 3)]
    assert registry.closed is True
    assert _body(response) == {
        "source": "pubmed",
        "query": "gene therapy",
        "records": [{"id": "record-1"}],
        "terms_url": "https://provider.example/terms",
        "retrieved_from": "https://provider.example/api",
        "warnings": [],
        "notice": (
            "Metadata only. Verify source rights, current status, and full text "
            "before using a record as research evidence."
        ),
    }


def test_lookup_returns_normalized_connector_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry()
    monkeypatch.setattr(function_app, "registry_factory", lambda: registry)

    response = function_app.connector_lookup(
        _request(source="crossref", params={"identifier": "10.1000/test"})
    )

    assert response.status_code == 200
    assert registry.calls == [("crossref", "10.1000/test", None)]
    assert registry.closed is True
    assert _body(response)["records"] == [{"id": "10.1000/test"}]


def test_search_rejects_non_integer_limit() -> None:
    response = function_app.connector_search(
        _request(source="pubmed", params={"query": "gene therapy", "limit": "many"})
    )

    assert response.status_code == 422
    assert _body(response) == {"detail": "Connector limit must be an integer"}


def test_function_maps_provider_failure_without_leaking_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = FakeRegistry(error=httpx.ConnectError("private upstream detail"))
    monkeypatch.setattr(function_app, "registry_factory", lambda: registry)

    response = function_app.connector_search(
        _request(source="pubmed", params={"query": "gene therapy"})
    )

    assert response.status_code == 502
    assert registry.closed is True
    assert _body(response) == {"detail": "Connector provider is unavailable."}