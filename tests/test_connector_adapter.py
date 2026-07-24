from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from research_assistant_connector_adapter.app import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    REQUEST_BODY_LIMIT_ENV,
    RequestBodyLimitMiddleware,
    app,
    request_body_limit_from_environment,
)
from research_assistant_connectors import ConnectorResult
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


def test_catalog_and_health_do_not_call_external_providers(client: TestClient) -> None:
    health = client.get("/health")
    catalog = client.get("/v1/connectors")

    assert health.json()["status"] == "healthy"
    assert len(catalog.json()["connectors"]) == 12
    assert FakeRegistry.calls == []


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


def test_request_body_limit_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(REQUEST_BODY_LIMIT_ENV, raising=False)
    assert request_body_limit_from_environment() == DEFAULT_MAX_REQUEST_BODY_BYTES
    monkeypatch.setenv(REQUEST_BODY_LIMIT_ENV, "1024")
    assert request_body_limit_from_environment() == 1024
    for invalid in ("invalid", "0", "-1"):
        monkeypatch.setenv(REQUEST_BODY_LIMIT_ENV, invalid)
        with pytest.raises(ValueError, match=REQUEST_BODY_LIMIT_ENV):
            request_body_limit_from_environment()
    with pytest.raises(ValueError, match="positive"):
        RequestBodyLimitMiddleware(cast(ASGIApp, object()), max_body_bytes=0)

    passed_through = False

    async def downstream(scope: Scope, _receive: Receive, _send: Send) -> None:
        nonlocal passed_through
        assert scope["type"] == "lifespan"
        passed_through = True

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(_message: Message) -> None:
        raise AssertionError("Non-HTTP pass-through should not send directly")

    asyncio.run(
        RequestBodyLimitMiddleware(downstream, max_body_bytes=1)(
            cast(Scope, {"type": "lifespan", "asgi": {"version": "3.0"}}),
            receive,
            send,
        )
    )
    assert passed_through


def test_request_body_limiter_rejects_declared_chunked_and_concurrent_oversize() -> None:
    completed_paths: set[str] = set()

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        completed_paths.add(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limiter = RequestBodyLimitMiddleware(downstream, max_body_bytes=5)

    async def invoke(
        path: str,
        chunks: tuple[bytes, ...] | None,
        declared_length: bytes | None,
    ) -> tuple[int, int]:
        messages = (
            [{"type": "http.disconnect"}]
            if chunks is None
            else [
                {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": index < len(chunks) - 1,
                }
                for index, chunk in enumerate(chunks)
            ]
        )
        received = 0
        sent: list[Message] = []

        async def receive() -> Message:
            nonlocal received
            message = messages[received]
            received += 1
            return cast(Message, message)

        async def send(message: Message) -> None:
            sent.append(message)

        headers = [] if declared_length is None else [(b"content-length", declared_length)]
        scope = cast(
            Scope,
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": headers,
                "client": ("127.0.0.1", 1234),
                "server": ("test", 443),
            },
        )
        await limiter(scope, receive, send)
        response_start = next(message for message in sent if message["type"] == "http.response.start")
        return cast(int, response_start["status"]), received

    async def exercise() -> tuple[tuple[int, int], ...]:
        return tuple(
            await asyncio.gather(
                invoke("/declared", (b"ignored",), b"7"),
                invoke("/chunked", (b"abc", b"def"), None),
                invoke("/lying", (b"abc", b"def"), b"4"),
                invoke("/malformed-length", (b"abc",), b"invalid"),
                invoke("/disconnect", None, None),
                invoke("/allowed", (b"abc", b"de"), None),
            )
        )

    assert asyncio.run(exercise()) == (
        (413, 0),
        (413, 2),
        (413, 2),
        (204, 1),
        (204, 1),
        (204, 2),
    )
    assert completed_paths == {"/allowed", "/disconnect", "/malformed-length"}
