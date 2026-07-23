from __future__ import annotations

import pytest
from pydantic import HttpUrl
from research_assistant_api.connector_gateway import ConnectorGatewayError
from research_assistant_api.public_research import retrieve_public_metadata
from research_assistant_api.workspace import WorkspaceStore
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability


@pytest.mark.asyncio
async def test_public_research_honors_enabled_agent_assignments() -> None:
    calls: list[tuple[Capability, str, str, int]] = []

    class FakeGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            calls.append((capability, source, query, limit))
            return ConnectorSearchResponse(
                source=PublicConnectorSource(source),
                query=query,
                records=[{"id": f"{source}-1", "title": "Public record"}],
                terms_url=HttpUrl(f"https://terms.example/{source}"),
                retrieved_from=HttpUrl(f"https://api.example/{source}"),
            )

        async def close(self) -> None:
            return None

    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        connector.enabled = connector.id == "pubmed"
        connector.assigned_agents = ["literature"]

    results = await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        connectors,
        gateway=FakeGateway(),
        requested_sources=["PubMed"],
    )

    assert calls == [
        (
            Capability.LITERATURE,
            "pubmed",
            "public reproducibility guidance",
            3,
        ),
    ]
    assert results[0]["source"] == "pubmed"
    assert results[0]["status"] == "ready"
    assert results[0]["records"][0]["id"] == "pubmed-1"


@pytest.mark.asyncio
async def test_public_research_honors_nondefault_requested_source() -> None:
    calls: list[str] = []

    class FakeGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            del capability, limit
            calls.append(source)
            return ConnectorSearchResponse(
                source=PublicConnectorSource(source),
                query=query,
                records=[],
                terms_url=HttpUrl("https://terms.example"),
                retrieved_from=HttpUrl("https://api.example"),
            )

        async def close(self) -> None:
            return None

    connector = next(
        item
        for item in WorkspaceStore().connectors()
        if item.id == "europe_pmc"
    )

    await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        [connector],
        gateway=FakeGateway(),
        requested_sources=["Europe PMC"],
    )

    assert calls == ["europe_pmc"]


@pytest.mark.asyncio
async def test_public_research_surfaces_connector_failure() -> None:
    class FailingGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            del capability, source, query, limit
            raise ConnectorGatewayError("Provider response was invalid")

        async def close(self) -> None:
            return None

    connector = next(item for item in WorkspaceStore().connectors() if item.id == "grants_gov")

    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        [connector],
        gateway=FailingGateway(),
    )

    assert results == [
        {
            "source": "grants_gov",
            "status": "unavailable",
            "error": "Provider response was invalid",
            "records": [],
        }
    ]
