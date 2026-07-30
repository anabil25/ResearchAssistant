from __future__ import annotations

import json

import httpx
import pytest
from research_assistant_connectors import (
    ResearchConnectorRegistry,
    connector_catalog,
)
from research_assistant_core.connector_catalog import connector_definitions


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


def test_connector_catalog_defines_unique_source_qualified_read_tools() -> None:
    definitions = connector_definitions()

    assert len({connector.id for connector in definitions}) == len(definitions)
    operations = [operation for connector in definitions for operation in connector.operations]
    assert len({operation.id for operation in operations}) == len(operations)
    assert all(operation.operation_class == "read" for operation in operations)
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("pubmed")
    } == {("pubmedSearch", "search"), ("pubmedLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("crossref")
    } == {("crossrefSearch", "search"), ("crossrefLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("europePmc")
    } == {("europePmcSearch", "search"), ("europePmcLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("clinicalTrials")
    } == {("clinicalTrialsSearch", "search"), ("clinicalTrialsLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("arxiv")
    } == {("arxivSearch", "search"), ("arxivLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("datacite")
    } == {("dataciteSearch", "search"), ("dataciteLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("openalex")
    } == {("openalexSearch", "search"), ("openalexLookup", "lookup")}
    assert {
        (operation.id, operation.mcp_tool_name)
        for operation in operations
        if operation.id.startswith("ror")
    } == {("rorSearch", "search"), ("rorLookup", "lookup")}
    assert all(operation.operation_class == "read" for operation in operations)


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


def test_pubmed_lookup_returns_only_the_requested_numeric_identifier() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
    result = registry.lookup("pubmed", "123")

    assert result.query == "123"
    assert result.records[0]["pmid"] == "123"
    assert requests[0].url.params["id"] == "123"
    with pytest.raises(ValueError, match="numeric PMID"):
        registry.lookup("pubmed", "pmid-123")
    with pytest.raises(ValueError, match="does not support identifier lookup"):
        registry.lookup("grants_gov", "123")

    missing = ResearchConnectorRegistry(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"result": {}})))
    ).lookup("pubmed", "456")
    assert missing.records == []


def test_crossref_lookup_requires_a_bounded_doi_and_returns_normalized_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "DOI": "10.1/example",
                    "title": ["Grounded research"],
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                    "container-title": ["Journal"],
                    "type": "article",
                    "URL": "https://doi.org/10.1/example",
                }
            },
        )

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("crossref", "10.1/example")

    assert result.records == [
        {
            "doi": "10.1/example",
            "title": "Grounded research",
            "authors": ["Ada Lovelace"],
            "venue": "Journal",
            "type": "article",
            "updates": [],
            "url": "https://doi.org/10.1/example",
        }
    ]
    with pytest.raises(ValueError, match="must be a DOI"):
        registry.lookup("crossref", "not-a-doi")


def test_arxiv_lookup_requires_a_modern_identifier_and_returns_one_record() -> None:
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://arxiv.org/abs/2601.00001v2</id>
        <title> Grounded research </title>
        <summary> Bounded metadata </summary>
        <published>2026-01-01T00:00:00Z</published>
      </entry>
    </feed>"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=atom)

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("arxiv", "2601.00001")

    assert result.records == [
        {
            "id": "2601.00001v2",
            "title": "Grounded research",
            "summary": "Bounded metadata",
            "published": "2026-01-01T00:00:00Z",
            "url": "https://arxiv.org/abs/2601.00001v2",
        }
    ]
    assert requests[0].url.params["id_list"] == "2601.00001"
    assert requests[0].url.params["max_results"] == "1"
    with pytest.raises(ValueError, match="modern YYYY.NNNNN"):
        registry.lookup("arxiv", "hep-ex/0307015")


def test_datacite_lookup_requires_a_bounded_doi_and_returns_normalized_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "10.1/data",
                    "attributes": {
                        "titles": [{"title": "Grounded dataset"}],
                        "publisher": "Repository",
                        "published": "2026",
                        "types": {"resourceTypeGeneral": "Dataset"},
                        "url": "https://example.test/data",
                    },
                }
            },
        )

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("datacite", "10.1/data")

    assert result.records == [
        {
            "doi": "10.1/data",
            "title": "Grounded dataset",
            "publisher": "Repository",
            "published": "2026",
            "types": {"resourceTypeGeneral": "Dataset"},
            "url": "https://example.test/data",
        }
    ]
    with pytest.raises(ValueError, match="must be a DOI"):
        registry.lookup("datacite", "not-a-doi")


def test_openalex_lookup_requires_a_canonical_work_id_and_uses_an_optional_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "https://openalex.org/W123",
                "display_name": "Grounded work",
                "doi": "https://doi.org/10.1/example",
                "publication_year": 2026,
                "type": "article",
                "cited_by_count": 4,
                "is_retracted": False,
                "open_access": {"is_oa": True},
            },
        )

    monkeypatch.setenv("OPENALEX_API_KEY", "approved-key")
    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("openalex", "w123")

    assert result.records[0]["openalex_id"] == "https://openalex.org/W123"
    assert requests[0].url.path.endswith("/works/W123")
    assert requests[0].url.params["api_key"] == "approved-key"
    with pytest.raises(ValueError, match="canonical W ID"):
        registry.lookup("openalex", "https://openalex.org/W123")


def test_ror_lookup_requires_a_canonical_identifier_and_returns_normalized_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "https://ror.org/00tjv0s33",
                "names": [
                    {"value": "KMU", "types": ["acronym"]},
                    {"value": "Keimyung University", "types": ["ror_display", "label"]},
                ],
                "types": ["education"],
                "locations": [],
            },
        )

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("ror", "00TJV0S33")

    assert result.records == [
        {
            "ror_id": "https://ror.org/00tjv0s33",
            "name": "Keimyung University",
            "types": ["education"],
            "locations": [],
            "url": "https://ror.org/00tjv0s33",
        }
    ]
    with pytest.raises(ValueError, match="canonical 9-character ROR ID"):
        registry.lookup("ror", "https://ror.org/00tjv0s33")


def test_europe_pmc_lookup_requires_source_and_identifier_match() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {
                            "source": "MED",
                            "id": "123",
                            "title": "Grounded research",
                            "authorString": "Ada Lovelace",
                            "journalTitle": "Journal",
                            "pubYear": "2026",
                            "doi": "10.1/example",
                            "isOpenAccess": "Y",
                        }
                    ]
                }
            },
        )

    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(handler)))
    result = registry.lookup("europe_pmc", "MED:123")

    assert result.records == [
        {
            "id": "123",
            "title": "Grounded research",
            "authors": "Ada Lovelace",
            "journal": "Journal",
            "year": "2026",
            "doi": "10.1/example",
            "open_access": True,
            "url": "https://europepmc.org/article/MED/123",
        }
    ]
    assert requests[0].url.path.endswith("/article/MED/123")
    assert requests[0].url.params["resultType"] == "core"
    with pytest.raises(ValueError, match="SOURCE:ARTICLE_ID"):
        registry.lookup("europe_pmc", "MED/123")

    unmatched = ResearchConnectorRegistry(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"id": "123", "source": "PMC", "title": "Different source"},
                )
            )
        )
    ).lookup("europe_pmc", "MED:123")
    assert unmatched.records == []


def test_unknown_connector_and_unbounded_query_are_rejected() -> None:
    registry = ResearchConnectorRegistry(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))))

    with pytest.raises(ValueError, match="Unsupported connector"):
        registry.search("random_scraper", "query")
    with pytest.raises(ValueError, match="between 2 and 500"):
        registry.search("crossref", "x")
