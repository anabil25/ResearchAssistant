from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from research_assistant_connectors import ResearchConnectorRegistry
from research_assistant_connectors import registry as registry_module


def registry_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ResearchConnectorRegistry:
    return ResearchConnectorRegistry(
        httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_registry_closes_only_clients_it_owns() -> None:
    owned = ResearchConnectorRegistry()
    owned.close()
    assert owned._client.is_closed

    external_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    )
    external = ResearchConnectorRegistry(external_client)
    external.close()
    assert not external_client.is_closed
    external_client.close()


@pytest.mark.parametrize("method", ["get", "post"])
def test_json_helpers_reject_non_object_payloads(method: str) -> None:
    registry = registry_with(lambda _: httpx.Response(200, json=[]))

    with pytest.raises(ValueError, match="Expected a JSON object"):
        if method == "get":
            registry._get_json("https://provider.example", params={})
        else:
            registry._post_json("https://provider.example", payload={})


def test_pubmed_supports_api_keys_and_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})

    monkeypatch.setenv("NCBI_API_KEY", "approved-key")
    result = registry_with(handler).search(" PUBMED ", " bounded query ", limit=0)

    assert result.records == []
    assert requests[0].url.params["api_key"] == "approved-key"
    assert requests[0].url.params["retmax"] == "1"


@pytest.mark.parametrize(
    ("source", "payload", "expected"),
    [
        (
            "europe_pmc",
            {
                "resultList": {
                    "result": [
                        {
                            "id": "PMC1",
                            "source": "MED",
                            "title": "Evidence",
                            "authorString": "A. Author",
                            "journalTitle": "Journal",
                            "pubYear": "2026",
                            "doi": "10.1/example",
                            "isOpenAccess": "Y",
                        }
                    ]
                }
            },
            {"id": "PMC1", "open_access": True},
        ),
        (
            "crossref",
            {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1/example",
                            "title": ["Evidence"],
                            "author": [{"given": "Ada", "family": "Lovelace"}],
                            "container-title": ["Journal"],
                            "type": "article",
                            "update-to": [],
                            "URL": "https://doi.org/10.1/example",
                        }
                    ]
                }
            },
            {"doi": "10.1/example", "authors": ["Ada Lovelace"]},
        ),
        (
            "openalex",
            {
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Evidence",
                        "doi": "https://doi.org/10.1/example",
                        "publication_year": 2026,
                        "type": "article",
                        "cited_by_count": 4,
                        "is_retracted": False,
                        "open_access": {"is_oa": True},
                    }
                ]
            },
            {
                "openalex_id": "https://openalex.org/W1",
                "is_retracted": False,
            },
        ),
        (
            "grants_gov",
            {
                "data": {
                    "oppHits": [
                        {
                            "id": "grant-1",
                            "number": "ABC-1",
                            "title": "Opportunity",
                            "agency": "Agency",
                            "openDate": "2026-01-01",
                            "closeDate": "2026-02-01",
                        }
                    ]
                }
            },
            {"id": "grant-1", "number": "ABC-1"},
        ),
        (
            "nih_reporter",
            {
                "results": [
                    {
                        "project_num": "P1",
                        "project_title": "Project",
                        "organization": {"org_name": "University"},
                        "principal_investigators": [{"full_name": "A. PI"}],
                        "fiscal_year": 2026,
                        "appl_id": 42,
                    }
                ]
            },
            {
                "project_num": "P1",
                "principal_investigators": ["A. PI"],
            },
        ),
        (
            "datacite",
            {
                "data": [
                    {
                        "id": "10.1/data",
                        "attributes": {
                            "titles": [{"title": "Dataset"}],
                            "publisher": "Repository",
                            "published": "2026",
                            "types": {"resourceTypeGeneral": "Dataset"},
                            "url": "https://example.test/data",
                        },
                    }
                ]
            },
            {"doi": "10.1/data", "title": "Dataset"},
        ),
        (
            "orcid",
            {
                "expanded-result": [
                    {
                        "orcid-id": "0000-0001",
                        "given-names": "Ada",
                        "family-names": "Lovelace",
                        "institution-name": "University",
                        "email": None,
                    }
                ]
            },
            {"orcid": "0000-0001", "name": "Ada Lovelace"},
        ),
    ],
)
def test_json_connectors_parse_bounded_metadata(
    source: str,
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    registry = registry_with(lambda _: httpx.Response(200, json=payload))

    result = registry.search(source, "evidence", limit=20)

    assert len(result.records) == 1
    assert result.records[0].items() >= expected.items()


def test_arxiv_parses_atom_and_propagates_timeouts() -> None:
    atom = b"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://arxiv.org/abs/2601.00001</id>
        <title>  Auditable   research </title>
        <summary>  Stored   metadata </summary>
        <published>2026-01-01T00:00:00Z</published>
      </entry>
    </feed>"""
    parsed = registry_with(
        lambda _: httpx.Response(200, content=atom)
    ).search("arxiv", "evidence")
    assert parsed.records == [
        {
            "id": "2601.00001",
            "title": "Auditable research",
            "summary": "Stored metadata",
            "published": "2026-01-01T00:00:00Z",
            "url": "https://arxiv.org/abs/2601.00001",
        }
    ]

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("provider timed out", request=request)

    with pytest.raises(httpx.ReadTimeout):
        registry_with(timeout).search("arxiv", "evidence")


class FakeUrlResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeUrlResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        assert limit == 2_000_001
        return self.body


def test_clinical_trials_urllib_parser_enforces_shape_and_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": "NCT1",
                        "briefTitle": "Study",
                    },
                    "statusModule": {
                        "overallStatus": "RECRUITING",
                        "studyFirstPostDateStruct": {"date": "2026-01-01"},
                    },
                }
            }
        ]
    }
    monkeypatch.setattr(
        registry_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeUrlResponse(json.dumps(payload).encode()),
    )
    registry = ResearchConnectorRegistry(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
    )
    result = registry.search("clinical_trials", "heart study")
    assert result.records[0]["nct_id"] == "NCT1"

    monkeypatch.setattr(
        registry_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeUrlResponse(b"x" * 2_000_001),
    )
    with pytest.raises(ValueError, match="2 MB safety limit"):
        registry.search("clinical_trials", "heart study")

    monkeypatch.setattr(
        registry_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeUrlResponse(b"[]"),
    )
    with pytest.raises(ValueError, match="Expected a JSON object"):
        registry.search("clinical_trials", "heart study")


def test_clinical_trials_lookup_requires_a_canonical_nct_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Request] = []
    payload = {
        "protocolSection": {
            "identificationModule": {
                "nctId": "NCT00000001",
                "briefTitle": "Study",
            },
            "statusModule": {
                "overallStatus": "RECRUITING",
                "studyFirstPostDateStruct": {"date": "2026-01-01"},
            },
        }
    }

    def fake_urlopen(request: Request, **_kwargs: object) -> FakeUrlResponse:
        requests.append(request)
        return FakeUrlResponse(json.dumps(payload).encode())

    monkeypatch.setattr(registry_module, "urlopen", fake_urlopen)
    registry = ResearchConnectorRegistry(
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        )
    )
    result = registry.lookup("clinical_trials", "nct00000001")

    assert result.query == "NCT00000001"
    assert result.records[0]["nct_id"] == "NCT00000001"
    assert requests[0].full_url.endswith("/studies/NCT00000001")
    with pytest.raises(ValueError, match="NCT ID"):
        registry.lookup("clinical_trials", "NCT1")


def test_ror_selects_display_names_and_respects_limit() -> None:
    payload = {
        "items": [
            {
                "id": "https://ror.org/1",
                "names": [
                    {"value": "Alias", "types": ["alias"]},
                    {"value": "University", "types": ["ror_display"]},
                ],
                "types": ["Education"],
                "locations": [],
            },
            {
                "id": "https://ror.org/2",
                "names": [{"value": "Alias only", "types": ["alias"]}],
                "types": ["Facility"],
                "locations": [],
            },
        ]
    }
    result = registry_with(lambda _: httpx.Response(200, json=payload)).search(
        "ror", "university", limit=2
    )
    assert [record["name"] for record in result.records] == [
        "University",
        None,
    ]


def test_semantic_scholar_handles_api_key_and_anonymous_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)

    def quota(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    anonymous = registry_with(quota).search(
        "semantic_scholar", "evidence"
    )
    assert anonymous.records == []
    assert "quota is exhausted" in anonymous.warnings[0]

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with pytest.raises(httpx.HTTPStatusError):
        registry_with(unavailable).search("semantic_scholar", "evidence")

    requests: list[httpx.Request] = []

    def success(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"paperId": "paper-1", "title": "Evidence"}]},
        )

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "approved-key")
    result = registry_with(success).search(
        "semantic_scholar", "evidence"
    )
    assert result.records[0]["paperId"] == "paper-1"
    assert requests[0].headers["x-api-key"] == "approved-key"

    with pytest.raises(httpx.HTTPStatusError):
        registry_with(quota).search("semantic_scholar", "evidence")
