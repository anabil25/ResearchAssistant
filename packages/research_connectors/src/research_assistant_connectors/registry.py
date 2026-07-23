"""Bounded clients for allowlisted public research metadata providers."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

import httpx
from defusedxml import ElementTree


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    source: str
    query: str
    records: list[dict[str, Any]]
    terms_url: str
    retrieved_from: str
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "source": self.source,
                "query": self.query,
                "records": self.records,
                "terms_url": self.terms_url,
                "retrieved_from": self.retrieved_from,
                "warnings": self.warnings,
                "notice": (
                    "Metadata only. Verify source rights, current status, and full text "
                    "before using a record as research evidence."
                ),
            }
        )


class ResearchConnectorRegistry:
    def __init__(self, client: httpx.Client | None = None) -> None:
        contact = os.getenv("RESEARCH_CONNECTOR_CONTACT", "research-assistant@example.edu")
        homepage = os.getenv(
            "RESEARCH_CONNECTOR_HOME",
            "https://example.edu/research-assistant",
        )
        self._user_agent = f"Mozilla/5.0 (compatible; ResearchAssistant/0.1; +{homepage})"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            },
        )
        self._contact = contact

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def sources(self) -> tuple[str, ...]:
        return (
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
        )

    def search(self, source: str, query: str, *, limit: int = 5) -> ConnectorResult:
        normalized = source.strip().lower()
        if normalized not in self.sources:
            raise ValueError(f"Unsupported connector '{source}'. Allowed: {', '.join(self.sources)}")
        clean_query = query.strip()
        if not 2 <= len(clean_query) <= 500:
            raise ValueError("Connector query must be between 2 and 500 characters")
        bounded_limit = min(max(limit, 1), 10)
        handlers: dict[str, Callable[[str, int], ConnectorResult]] = {
            "pubmed": self._search_pubmed,
            "europe_pmc": self._search_europe_pmc,
            "crossref": self._search_crossref,
            "openalex": self._search_openalex,
            "arxiv": self._search_arxiv,
            "clinical_trials": self._search_clinical_trials,
            "grants_gov": self._search_grants_gov,
            "nih_reporter": self._search_nih_reporter,
            "datacite": self._search_datacite,
            "orcid": self._search_orcid,
            "ror": self._search_ror,
            "semantic_scholar": self._search_semantic_scholar,
        }
        return handlers[normalized](clean_query, bounded_limit)

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._client.get(url, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object from {url}")
        return payload

    def _post_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"Expected a JSON object from {url}")
        return body

    def _urllib_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            },
        )
        with urlopen(request, timeout=20) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError(f"Response from {url} exceeded the 2 MB safety limit")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected a JSON object from {url}")
        return payload

    def _search_pubmed(self, query: str, limit: int) -> ConnectorResult:
        api_key = os.getenv("NCBI_API_KEY")
        common = {
            "db": "pubmed",
            "retmode": "json",
            "tool": "research-assistant",
            "email": self._contact,
        }
        if api_key:
            common["api_key"] = api_key
        search = self._get_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={**common, "term": query, "retmax": limit},
        )
        ids = search.get("esearchresult", {}).get("idlist", [])
        if not ids:
            records: list[dict[str, Any]] = []
        else:
            summary = self._get_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={**common, "id": ",".join(ids)},
            )
            result = summary.get("result", {})
            records = [
                {
                    "pmid": pmid,
                    "title": result.get(pmid, {}).get("title"),
                    "authors": [author.get("name") for author in result.get(pmid, {}).get("authors", [])],
                    "published": result.get(pmid, {}).get("pubdate"),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                }
                for pmid in ids
            ]
        return ConnectorResult(
            source="pubmed",
            query=query,
            records=records,
            terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
            retrieved_from="https://eutils.ncbi.nlm.nih.gov/",
        )

    def _search_europe_pmc(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": query, "format": "json", "pageSize": limit},
        )
        records = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "authors": item.get("authorString"),
                "journal": item.get("journalTitle"),
                "year": item.get("pubYear"),
                "doi": item.get("doi"),
                "open_access": item.get("isOpenAccess") == "Y",
                "url": f"https://europepmc.org/article/{item.get('source')}/{item.get('id')}",
            }
            for item in payload.get("resultList", {}).get("result", [])
        ]
        return ConnectorResult(
            source="europe_pmc",
            query=query,
            records=records,
            terms_url="https://europepmc.org/terms",
            retrieved_from="https://www.ebi.ac.uk/europepmc/webservices/rest/",
        )

    def _search_crossref(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://api.crossref.org/works",
            params={
                "query.bibliographic": query,
                "rows": limit,
                "mailto": self._contact,
                "select": "DOI,title,author,published,container-title,URL,type,update-to",
            },
        )
        records = [
            {
                "doi": item.get("DOI"),
                "title": (item.get("title") or [None])[0],
                "authors": [
                    " ".join(part for part in (author.get("given"), author.get("family")) if part)
                    for author in item.get("author", [])
                ],
                "venue": (item.get("container-title") or [None])[0],
                "type": item.get("type"),
                "updates": item.get("update-to", []),
                "url": item.get("URL"),
            }
            for item in payload.get("message", {}).get("items", [])
        ]
        return ConnectorResult(
            source="crossref",
            query=query,
            records=records,
            terms_url="https://www.crossref.org/terms/",
            retrieved_from="https://api.crossref.org/works",
        )

    def _search_openalex(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": limit, "mailto": self._contact},
        )
        records = [
            {
                "openalex_id": item.get("id"),
                "title": item.get("display_name"),
                "doi": item.get("doi"),
                "year": item.get("publication_year"),
                "type": item.get("type"),
                "cited_by_count": item.get("cited_by_count"),
                "is_retracted": item.get("is_retracted"),
                "open_access": item.get("open_access"),
                "url": item.get("id"),
            }
            for item in payload.get("results", [])
        ]
        return ConnectorResult(
            source="openalex",
            query=query,
            records=records,
            terms_url="https://openalex.org/terms",
            retrieved_from="https://api.openalex.org/works",
        )

    def _search_arxiv(self, query: str, limit: int) -> ConnectorResult:
        response = self._client.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": limit},
            headers={"Accept": "application/atom+xml"},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        records = []
        for entry in root.findall("atom:entry", namespace):
            identifier = entry.findtext("atom:id", default="", namespaces=namespace)
            records.append(
                {
                    "id": identifier.rsplit("/", 1)[-1],
                    "title": " ".join(entry.findtext("atom:title", default="", namespaces=namespace).split()),
                    "summary": " ".join(entry.findtext("atom:summary", default="", namespaces=namespace).split())[:600],
                    "published": entry.findtext("atom:published", default="", namespaces=namespace),
                    "url": identifier,
                }
            )
        return ConnectorResult(
            source="arxiv",
            query=query,
            records=records,
            terms_url="https://info.arxiv.org/help/api/tou.html",
            retrieved_from="https://export.arxiv.org/api/query",
        )

    def _search_clinical_trials(self, query: str, limit: int) -> ConnectorResult:
        payload = self._urllib_json(
            f"https://clinicaltrials.gov/api/v2/studies?query.term={quote_plus(query)}&pageSize={limit}&format=json"
        )
        records = []
        for item in payload.get("studies", []):
            protocol = item.get("protocolSection", {})
            identification = protocol.get("identificationModule", {})
            status = protocol.get("statusModule", {})
            nct_id = identification.get("nctId")
            records.append(
                {
                    "nct_id": nct_id,
                    "title": identification.get("briefTitle"),
                    "status": status.get("overallStatus"),
                    "last_update": status.get("studyFirstPostDateStruct"),
                    "url": f"https://clinicaltrials.gov/study/{nct_id}",
                }
            )
        return ConnectorResult(
            source="clinical_trials",
            query=query,
            records=records,
            terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
            retrieved_from="https://clinicaltrials.gov/api/v2/studies",
        )

    def _search_grants_gov(self, query: str, limit: int) -> ConnectorResult:
        payload = self._post_json(
            "https://api.grants.gov/v1/api/search2",
            payload={"keyword": query, "rows": limit, "oppStatuses": "forecasted|posted"},
        )
        opportunities = payload.get("data", {}).get("oppHits", [])
        records = [
            {
                "id": item.get("id"),
                "number": item.get("number"),
                "title": item.get("title"),
                "agency": item.get("agency"),
                "open_date": item.get("openDate"),
                "close_date": item.get("closeDate"),
                "url": f"https://www.grants.gov/search-results-detail/{item.get('id')}",
            }
            for item in opportunities
        ]
        return ConnectorResult(
            source="grants_gov",
            query=query,
            records=records,
            terms_url="https://www.grants.gov/web/grants/legal.html",
            retrieved_from="https://api.grants.gov/v1/api/search2",
        )

    def _search_nih_reporter(self, query: str, limit: int) -> ConnectorResult:
        payload = self._post_json(
            "https://api.reporter.nih.gov/v2/projects/search",
            payload={
                "criteria": {"advanced_text_search": {"operator": "and", "search_field": "all", "search_text": query}},
                "offset": 0,
                "limit": limit,
            },
        )
        records = [
            {
                "project_num": item.get("project_num"),
                "title": item.get("project_title"),
                "organization": item.get("organization", {}).get("org_name"),
                "principal_investigators": [
                    investigator.get("full_name") for investigator in item.get("principal_investigators", [])
                ],
                "fiscal_year": item.get("fiscal_year"),
                "url": f"https://reporter.nih.gov/project-details/{item.get('appl_id')}",
            }
            for item in payload.get("results", [])
        ]
        return ConnectorResult(
            source="nih_reporter",
            query=query,
            records=records,
            terms_url="https://reporter.nih.gov/about",
            retrieved_from="https://api.reporter.nih.gov/v2/projects/search",
        )

    def _search_datacite(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://api.datacite.org/dois",
            params={"query": query, "page[size]": limit},
        )
        records = [
            {
                "doi": item.get("id"),
                "title": (item.get("attributes", {}).get("titles") or [{}])[0].get("title"),
                "publisher": item.get("attributes", {}).get("publisher"),
                "published": item.get("attributes", {}).get("published"),
                "types": item.get("attributes", {}).get("types"),
                "url": item.get("attributes", {}).get("url"),
            }
            for item in payload.get("data", [])
        ]
        return ConnectorResult(
            source="datacite",
            query=query,
            records=records,
            terms_url="https://datacite.org/terms.html",
            retrieved_from="https://api.datacite.org/dois",
        )

    def _search_orcid(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://pub.orcid.org/v3.0/expanded-search/",
            params={"q": query, "rows": limit},
            headers={"Accept": "application/json"},
        )
        records = [
            {
                "orcid": item.get("orcid-id"),
                "name": " ".join(part for part in (item.get("given-names"), item.get("family-names")) if part),
                "institution": item.get("institution-name"),
                "email": item.get("email"),
                "url": f"https://orcid.org/{item.get('orcid-id')}",
            }
            for item in payload.get("expanded-result", [])
        ]
        return ConnectorResult(
            source="orcid",
            query=query,
            records=records,
            terms_url="https://info.orcid.org/terms-of-use/",
            retrieved_from="https://pub.orcid.org/v3.0/expanded-search/",
        )

    def _search_ror(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://api.ror.org/v2/organizations",
            params={"query": query, "page": 1},
        )
        records = [
            {
                "ror_id": item.get("id"),
                "name": next(
                    (name.get("value") for name in item.get("names", []) if "ror_display" in name.get("types", [])),
                    None,
                ),
                "types": item.get("types"),
                "locations": item.get("locations"),
                "url": item.get("id"),
            }
            for item in payload.get("items", [])[:limit]
        ]
        return ConnectorResult(
            source="ror",
            query=query,
            records=records,
            terms_url="https://ror.org/terms/",
            retrieved_from="https://api.ror.org/v2/organizations",
        )

    def _search_semantic_scholar(self, query: str, limit: int) -> ConnectorResult:
        headers = {}
        if api_key := os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
            headers["x-api-key"] = api_key
        try:
            payload = self._get_json(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={
                    "query": query,
                    "limit": limit,
                    "fields": "title,authors,year,externalIds,citationCount,url,isOpenAccess",
                },
                headers=headers,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or os.getenv("SEMANTIC_SCHOLAR_API_KEY"):
                raise
            return ConnectorResult(
                source="semantic_scholar",
                query=query,
                records=[],
                terms_url="https://www.semanticscholar.org/product/api/license",
                retrieved_from="https://api.semanticscholar.org/graph/v1/paper/search",
                warnings=[
                    "Anonymous Semantic Scholar quota is exhausted. Configure "
                    "SEMANTIC_SCHOLAR_API_KEY in an approved secret connection."
                ],
            )
        records = payload.get("data", [])
        return ConnectorResult(
            source="semantic_scholar",
            query=query,
            records=records,
            terms_url="https://www.semanticscholar.org/product/api/license",
            retrieved_from="https://api.semanticscholar.org/graph/v1/paper/search",
        )


def connector_catalog() -> dict[str, str]:
    return {
        "pubmed": "Biomedical citations and metadata from NCBI PubMed.",
        "europe_pmc": "Life-science literature, grants, citations, and open-access status.",
        "crossref": "Publisher-deposited DOI metadata and update/correction relationships.",
        "openalex": "Works, authors, institutions, citation counts, OA, and retraction flags.",
        "arxiv": "Preprint metadata and abstracts from the official arXiv API.",
        "clinical_trials": "ClinicalTrials.gov v2 public study records.",
        "grants_gov": "US federal funding opportunities from Grants.gov.",
        "nih_reporter": "NIH-funded project and investigator metadata.",
        "datacite": "Dataset/software/other DOI metadata from DataCite.",
        "orcid": "Public researcher identifiers and affiliations from ORCID.",
        "ror": "Research organization identifiers and metadata from ROR.",
        "semantic_scholar": "Paper graph metadata; optional API key increases reliability.",
    }
