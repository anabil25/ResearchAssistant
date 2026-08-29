"""Bounded clients for allowlisted public research metadata providers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, quote_plus
from urllib.request import Request, urlopen

import httpx
from defusedxml import ElementTree
from research_assistant_core.connector_catalog import connector_definitions, connector_ids


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
        return connector_ids()

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

    def lookup(self, source: str, identifier: str) -> ConnectorResult:
        normalized = source.strip().lower()
        if normalized == "pubmed":
            pmid = identifier.strip()
            if not pmid.isascii() or not pmid.isdecimal() or not 1 <= len(pmid) <= 10:
                raise ValueError("PubMed identifier must be a numeric PMID with at most 10 digits")
            common = self._pubmed_common()
            summary = self._get_json(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={**common, "id": pmid},
            )
            return ConnectorResult(
                source="pubmed",
                query=pmid,
                records=self._pubmed_records((pmid,), summary),
                terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
                retrieved_from="https://eutils.ncbi.nlm.nih.gov/",
            )
        if normalized == "crossref":
            doi = identifier.strip()
            if (
                not 7 <= len(doi) <= 255
                or not doi.casefold().startswith("10.")
                or "/" not in doi
                or any(character.isspace() for character in doi)
            ):
                raise ValueError("Crossref identifier must be a DOI")
            payload = self._get_json(
                f"https://api.crossref.org/works/{quote(doi, safe='')}",
                params={"mailto": self._contact},
            )
            message = payload.get("message")
            records = [self._crossref_record(message)] if isinstance(message, dict) else []
            return ConnectorResult(
                source="crossref",
                query=doi,
                records=records,
                terms_url="https://www.crossref.org/terms/",
                retrieved_from="https://api.crossref.org/works",
            )
        if normalized == "europe_pmc":
            source, separator, article_id = identifier.strip().partition(":")
            source = source.upper()
            if (
                separator != ":"
                or not 2 <= len(source) <= 8
                or not source.isascii()
                or not source.isalnum()
                or not 1 <= len(article_id) <= 128
                or any(
                    not character.isascii()
                    or not (character.isalnum() or character in "-._")
                    for character in article_id
                )
            ):
                raise ValueError(
                    "Europe PMC identifier must use a bounded SOURCE:ARTICLE_ID value"
                )
            payload = self._get_json(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/"
                f"article/{quote(source, safe='')}/{quote(article_id, safe='')}",
                params={"format": "json", "resultType": "core"},
            )
            matched = next(
                (
                    item
                    for item in self._europe_pmc_items(payload)
                    if str(item.get("source", "")).upper() == source
                    and str(item.get("id", "")) == article_id
                ),
                None,
            )
            return ConnectorResult(
                source="europe_pmc",
                query=f"{source}:{article_id}",
                records=[self._europe_pmc_record(matched)] if matched is not None else [],
                terms_url="https://europepmc.org/terms",
                retrieved_from="https://www.ebi.ac.uk/europepmc/webservices/rest/article",
            )
        if normalized == "grants_gov":
            opportunity_id = identifier.strip()
            if (
                not opportunity_id.isascii()
                or not opportunity_id.isdecimal()
                or not 1 <= len(opportunity_id) <= 12
            ):
                raise ValueError(
                    "Grants.gov identifier must be a numeric opportunity ID with at most 12 digits"
                )
            payload = self._post_json(
                "https://api.grants.gov/v1/api/fetchOpportunity",
                payload={"opportunityId": int(opportunity_id)},
            )
            data = payload.get("data")
            errors = data.get("errorMessages") if isinstance(data, dict) else None
            record = (
                self._grants_gov_lookup_record(data)
                if payload.get("errorcode") == 0
                and isinstance(data, dict)
                and str(data.get("id")) == opportunity_id
                and not errors
                else None
            )
            return ConnectorResult(
                source="grants_gov",
                query=opportunity_id,
                records=[record] if record is not None else [],
                terms_url="https://www.grants.gov/web/grants/legal.html",
                retrieved_from="https://api.grants.gov/v1/api/fetchOpportunity",
                warnings=[str(item) for item in errors] if isinstance(errors, list) else [],
            )
        if normalized == "clinical_trials":
            nct_id = identifier.strip().upper()
            if (
                len(nct_id) != 11
                or not nct_id.startswith("NCT")
                or not nct_id[3:].isascii()
                or not nct_id[3:].isdecimal()
            ):
                raise ValueError(
                    "ClinicalTrials.gov identifier must be an NCT ID with eight digits"
                )
            payload = self._urllib_json(
                f"https://clinicaltrials.gov/api/v2/studies/{quote(nct_id, safe='')}"
            )
            record = self._clinical_trial_record(payload)
            return ConnectorResult(
                source="clinical_trials",
                query=nct_id,
                records=[record] if record["nct_id"] == nct_id else [],
                terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
                retrieved_from="https://clinicaltrials.gov/api/v2/studies",
            )
        if normalized == "arxiv":
            arxiv_id = identifier.strip()
            if not re.fullmatch(r"\d{4}\.\d{4,5}(?:v[1-9]\d*)?", arxiv_id):
                raise ValueError(
                    "arXiv identifier must use a modern YYYY.NNNNN value with an optional version"
                )
            response = self._client.get(
                "https://export.arxiv.org/api/query",
                params={"id_list": arxiv_id, "max_results": 1},
                headers={"Accept": "application/atom+xml"},
            )
            response.raise_for_status()
            records = self._arxiv_records(response.content)
            return ConnectorResult(
                source="arxiv",
                query=arxiv_id,
                records=records[:1],
                terms_url="https://info.arxiv.org/help/api/tou.html",
                retrieved_from="https://export.arxiv.org/api/query",
            )
        if normalized == "datacite":
            doi = identifier.strip()
            if (
                not 7 <= len(doi) <= 255
                or not doi.casefold().startswith("10.")
                or "/" not in doi
                or any(character.isspace() for character in doi)
            ):
                raise ValueError("DataCite identifier must be a DOI")
            payload = self._get_json(
                f"https://api.datacite.org/dois/{quote(doi, safe='')}",
                params={},
            )
            data = payload.get("data")
            records = (
                [self._datacite_record(data)]
                if isinstance(data, dict)
                and str(data.get("id", "")).casefold() == doi.casefold()
                else []
            )
            return ConnectorResult(
                source="datacite",
                query=doi,
                records=records,
                terms_url="https://datacite.org/terms.html",
                retrieved_from="https://api.datacite.org/dois",
            )
        if normalized == "openalex":
            work_id = identifier.strip().upper()
            if (
                not 2 <= len(work_id) <= 20
                or not work_id.startswith("W")
                or not work_id[1:].isascii()
                or not work_id[1:].isdecimal()
            ):
                raise ValueError("OpenAlex identifier must be a canonical W ID")
            payload = self._get_json(
                f"https://api.openalex.org/works/{quote(work_id, safe='')}",
                params={
                    **self._openalex_params(),
                    "select": "id,display_name,doi,publication_year,type,cited_by_count,is_retracted,open_access",
                },
            )
            returned_id = str(payload.get("id", "")).rsplit("/", 1)[-1].upper()
            return ConnectorResult(
                source="openalex",
                query=work_id,
                records=[self._openalex_record(payload)] if returned_id == work_id else [],
                terms_url="https://openalex.org/terms",
                retrieved_from="https://api.openalex.org/works",
            )
        if normalized == "ror":
            ror_id = identifier.strip().lower()
            if (
                len(ror_id) != 9
                or not ror_id.startswith("0")
                or not ror_id.isascii()
                or not ror_id.isalnum()
            ):
                raise ValueError("ROR identifier must be a canonical 9-character ROR ID")
            payload = self._get_json(
                f"https://api.ror.org/v2/organizations/{quote(ror_id, safe='')}",
                params={},
            )
            returned_id = str(payload.get("id", "")).rsplit("/", 1)[-1].lower()
            return ConnectorResult(
                source="ror",
                query=ror_id,
                records=[self._ror_record(payload)] if returned_id == ror_id else [],
                terms_url="https://ror.org/terms/",
                retrieved_from="https://api.ror.org/v2/organizations",
            )
        raise ValueError(f"Connector '{source}' does not support identifier lookup")

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
        common = self._pubmed_common()
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
            records = self._pubmed_records(tuple(ids), summary)
        return ConnectorResult(
            source="pubmed",
            query=query,
            records=records,
            terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
            retrieved_from="https://eutils.ncbi.nlm.nih.gov/",
        )

    def _pubmed_common(self) -> dict[str, str]:
        common = {
            "db": "pubmed",
            "retmode": "json",
            "tool": "research-assistant",
            "email": self._contact,
        }
        if api_key := os.getenv("NCBI_API_KEY"):
            common["api_key"] = api_key
        return common

    @staticmethod
    def _pubmed_records(
        pmids: tuple[str, ...],
        summary: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = summary.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("PubMed summary did not contain a result object")
        records: list[dict[str, Any]] = []
        for pmid in pmids:
            record = result.get(pmid)
            if not isinstance(record, dict):
                continue
            records.append(
                {
                    "pmid": pmid,
                    "title": record.get("title"),
                    "authors": [author.get("name") for author in record.get("authors", [])],
                    "published": record.get("pubdate"),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                }
            )
        return records

    def _search_europe_pmc(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": query, "format": "json", "pageSize": limit},
        )
        records = [self._europe_pmc_record(item) for item in self._europe_pmc_items(payload)]
        return ConnectorResult(
            source="europe_pmc",
            query=query,
            records=records,
            terms_url="https://europepmc.org/terms",
            retrieved_from="https://www.ebi.ac.uk/europepmc/webservices/rest/",
        )

    @staticmethod
    def _europe_pmc_items(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        result_list = payload.get("resultList")
        if isinstance(result_list, dict):
            results = result_list.get("result")
            if isinstance(results, list):
                return tuple(item for item in results if isinstance(item, dict))
        if "id" in payload and "source" in payload:
            return (payload,)
        return ()

    @staticmethod
    def _europe_pmc_record(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "authors": item.get("authorString"),
            "journal": item.get("journalTitle"),
            "year": item.get("pubYear"),
            "doi": item.get("doi"),
            "open_access": item.get("isOpenAccess") == "Y",
            "url": f"https://europepmc.org/article/{item.get('source')}/{item.get('id')}",
        }

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
        message = payload.get("message", {})
        items = message.get("items", []) if isinstance(message, dict) else []
        records = [self._crossref_record(item) for item in items if isinstance(item, dict)]
        return ConnectorResult(
            source="crossref",
            query=query,
            records=records,
            terms_url="https://www.crossref.org/terms/",
            retrieved_from="https://api.crossref.org/works",
        )

    @staticmethod
    def _crossref_record(item: dict[str, Any]) -> dict[str, Any]:
        titles = item.get("title")
        venues = item.get("container-title")
        authors = item.get("author")
        return {
            "doi": item.get("DOI"),
            "title": titles[0] if isinstance(titles, list) and titles else None,
            "authors": [
                " ".join(part for part in (author.get("given"), author.get("family")) if part)
                for author in (authors if isinstance(authors, list) else [])
                if isinstance(author, dict)
            ],
            "venue": venues[0] if isinstance(venues, list) and venues else None,
            "type": item.get("type"),
            "updates": item.get("update-to", []),
            "url": item.get("URL"),
        }

    def _search_openalex(self, query: str, limit: int) -> ConnectorResult:
        payload = self._get_json(
            "https://api.openalex.org/works",
            params={"search": query, "per-page": limit, **self._openalex_params()},
        )
        records = [
            self._openalex_record(item)
            for item in payload.get("results", [])
            if isinstance(item, dict)
        ]
        return ConnectorResult(
            source="openalex",
            query=query,
            records=records,
            terms_url="https://openalex.org/terms",
            retrieved_from="https://api.openalex.org/works",
        )

    def _openalex_params(self) -> dict[str, str]:
        params = {"mailto": self._contact}
        if api_key := os.getenv("OPENALEX_API_KEY"):
            params["api_key"] = api_key
        return params

    @staticmethod
    def _openalex_record(item: dict[str, Any]) -> dict[str, Any]:
        return {
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

    def _search_arxiv(self, query: str, limit: int) -> ConnectorResult:
        response = self._client.get(
            "https://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": limit},
            headers={"Accept": "application/atom+xml"},
        )
        response.raise_for_status()
        records = self._arxiv_records(response.content)[:limit]
        return ConnectorResult(
            source="arxiv",
            query=query,
            records=records,
            terms_url="https://info.arxiv.org/help/api/tou.html",
            retrieved_from="https://export.arxiv.org/api/query",
        )

    @staticmethod
    def _arxiv_records(payload: bytes) -> list[dict[str, Any]]:
        root = ElementTree.fromstring(payload)
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
        return records

    def _search_clinical_trials(self, query: str, limit: int) -> ConnectorResult:
        payload = self._urllib_json(
            f"https://clinicaltrials.gov/api/v2/studies?query.term={quote_plus(query)}&pageSize={limit}&format=json"
        )
        records = [
            self._clinical_trial_record(item)
            for item in payload.get("studies", [])
            if isinstance(item, dict)
        ]
        return ConnectorResult(
            source="clinical_trials",
            query=query,
            records=records,
            terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
            retrieved_from="https://clinicaltrials.gov/api/v2/studies",
        )

    @staticmethod
    def _clinical_trial_record(study: dict[str, Any]) -> dict[str, Any]:
        protocol = study.get("protocolSection")
        if not isinstance(protocol, dict):
            protocol = {}
        identification = protocol.get("identificationModule")
        if not isinstance(identification, dict):
            identification = {}
        status = protocol.get("statusModule")
        if not isinstance(status, dict):
            status = {}
        nct_id = identification.get("nctId")
        return {
            "nct_id": nct_id,
            "title": identification.get("briefTitle"),
            "status": status.get("overallStatus"),
            "last_update": status.get("studyFirstPostDateStruct"),
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        }

    def _search_grants_gov(self, query: str, limit: int) -> ConnectorResult:
        payload = self._post_json(
            "https://api.grants.gov/v1/api/search2",
            payload={"keyword": query, "rows": limit, "oppStatuses": "forecasted|posted"},
        )
        opportunities = payload.get("data", {}).get("oppHits", [])
        records = [
            record
            for item in opportunities
            if isinstance(item, dict)
            and (record := self._grants_gov_search_record(item)) is not None
        ]
        return ConnectorResult(
            source="grants_gov",
            query=query,
            records=records,
            terms_url="https://www.grants.gov/web/grants/legal.html",
            retrieved_from="https://api.grants.gov/v1/api/search2",
        )

    @staticmethod
    def _grants_gov_date(value: Any) -> str | None:
        text = str(value or "").strip()
        slash_date = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", text)
        if slash_date:
            month, day, year = slash_date.groups()
            return f"{year}-{month}-{day}"
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:-|$)", text):
            return text[:10]
        return None

    @staticmethod
    def _grants_gov_amount(value: Any) -> int | None:
        # Grants.gov reports award amounts as digit strings, "none", or "0".
        text = str(value or "").strip()
        if not text.isascii() or not text.isdecimal():
            return None
        return int(text) or None

    @classmethod
    def _grants_gov_search_record(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        opportunity_id = str(item.get("id") or "").strip()
        if not opportunity_id.isascii() or not opportunity_id.isdecimal():
            return None
        return {
            "grants_gov_id": opportunity_id,
            "opportunity_number": item.get("number"),
            "title": item.get("title"),
            "agency": item.get("agency"),
            "status": str(item.get("oppStatus") or "").casefold() or None,
            "posted_date": cls._grants_gov_date(item.get("openDate")),
            "close_date": cls._grants_gov_date(item.get("closeDate")),
            "archive_date": None,
            "canonical_url": f"https://www.grants.gov/search-results-detail/{opportunity_id}",
        }

    @classmethod
    def _grants_gov_lookup_record(cls, data: dict[str, Any]) -> dict[str, Any]:
        opportunity_id = str(data["id"])
        raw_synopsis = data.get("synopsis")
        raw_agency = data.get("agencyDetails")
        synopsis: dict[str, Any] = raw_synopsis if isinstance(raw_synopsis, dict) else {}
        agency: dict[str, Any] = raw_agency if isinstance(raw_agency, dict) else {}
        return {
            "grants_gov_id": opportunity_id,
            "opportunity_number": data.get("opportunityNumber"),
            "title": data.get("opportunityTitle"),
            "agency": agency.get("agencyName") or synopsis.get("agencyName"),
            "status": str(data.get("ost") or "").casefold() or None,
            "posted_date": cls._grants_gov_date(synopsis.get("postingDateStr")),
            "close_date": cls._grants_gov_date(synopsis.get("responseDateStr")),
            "archive_date": cls._grants_gov_date(synopsis.get("archiveDateStr")),
            "award_ceiling": cls._grants_gov_amount(synopsis.get("awardCeiling")),
            "award_floor": cls._grants_gov_amount(synopsis.get("awardFloor")),
            "canonical_url": f"https://www.grants.gov/search-results-detail/{opportunity_id}",
        }

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
            self._datacite_record(item)
            for item in payload.get("data", [])
            if isinstance(item, dict)
        ]
        return ConnectorResult(
            source="datacite",
            query=query,
            records=records,
            terms_url="https://datacite.org/terms.html",
            retrieved_from="https://api.datacite.org/dois",
        )

    @staticmethod
    def _datacite_record(item: dict[str, Any]) -> dict[str, Any]:
        attributes = item.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        titles = attributes.get("titles")
        first_title = titles[0] if isinstance(titles, list) and titles else {}
        return {
            "doi": item.get("id"),
            "title": first_title.get("title") if isinstance(first_title, dict) else None,
            "publisher": attributes.get("publisher"),
            "published": attributes.get("published"),
            "types": attributes.get("types"),
            "url": attributes.get("url"),
        }

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
            self._ror_record(item)
            for item in payload.get("items", [])[:limit]
            if isinstance(item, dict)
        ]
        return ConnectorResult(
            source="ror",
            query=query,
            records=records,
            terms_url="https://ror.org/terms/",
            retrieved_from="https://api.ror.org/v2/organizations",
        )

    @staticmethod
    def _ror_record(item: dict[str, Any]) -> dict[str, Any]:
        names = item.get("names")
        if not isinstance(names, list):
            names = []
        return {
            "ror_id": item.get("id"),
            "name": next(
                (
                    name.get("value")
                    for name in names
                    if isinstance(name, dict)
                    and isinstance(name.get("types"), list)
                    and "ror_display" in name["types"]
                ),
                None,
            ),
            "types": item.get("types"),
            "locations": item.get("locations"),
            "url": item.get("id"),
        }

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
    return {connector.id: connector.description for connector in connector_definitions()}
