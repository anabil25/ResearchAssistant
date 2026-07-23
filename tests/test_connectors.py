from __future__ import annotations

import json

import httpx
import pytest
from research_assistant_connectors import (
    ResearchConnectorRegistry,
    connector_catalog,
)


def test_connector_catalog_covers_scholarly_funding_and_identity_sources() -> None:
    catalog = connector_catalog()

    assert {
        "pubmed",
        "europe_pmc",
        "crossref",
        "openalex",
        "arxiv",
        "clinical_trials",
        "grants_gov",
        "nih_reporter",
        "datacite",
        "orcid",
        "ror",
        "semantic_scholar",
    } == set(catalog)


def test_pubmed_connector_returns_metadata_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["123"]}})
        return httpx.Response(
            200,
            json={
                "result": {
                    "123": {
                        "title": "Grounded research",
                        "authors": [{"name": "A. Researcher"}],
                        "pubdate": "2026",
                    }
                }
            },
        )

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.search("pubmed", "grounded research", limit=1)
    payload = json.loads(result.to_json())

    assert payload["records"] == [
        {
            "pmid": "123",
            "title": "Grounded research",
            "authors": ["A. Researcher"],
            "published": "2026",
            "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
        }
    ]
    assert "Metadata only" in payload["notice"]


def test_unknown_connector_and_unbounded_query_are_rejected() -> None:
    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))))

    with pytest.raises(ValueError, match="Unsupported connector"):
        registry.search("random_scraper", "query")
    with pytest.raises(ValueError, match="between 2 and 500"):
        registry.search("crossref", "x")
