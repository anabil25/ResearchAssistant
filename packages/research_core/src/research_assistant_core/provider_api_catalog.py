"""Catalog of upstream provider APIs onboarded through API Management.

Every provider is exposed as an APIM API whose backend is the upstream service,
and then as an APIM-native MCP server. Providers that publish a machine-readable
specification are fetched into ``infra/provider-specs/official``; the rest are
authored from their official documentation into
``infra/provider-specs/authored``. Delete-class operations are never declared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

HttpMethod = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class SpecParameter:
    name: str
    location: Literal["query", "path"]
    description: str
    required: bool = False
    schema_type: Literal["string", "integer", "boolean"] = "string"


@dataclass(frozen=True, slots=True)
class SpecOperation:
    operation_id: str
    method: HttpMethod
    path: str
    summary: str
    parameters: tuple[SpecParameter, ...] = ()
    accepts_json_body: bool = False


@dataclass(frozen=True, slots=True)
class SecondaryApi:
    """An additional upstream host for a provider that spans several backends."""

    suffix: str
    server_url: str
    description: str
    operations: tuple[SpecOperation, ...]


@dataclass(frozen=True, slots=True)
class ProviderApi:
    connector_id: str
    display_name: str
    documentation_url: str
    server_url: str
    description: str
    published_spec_url: str | None = None
    operations: tuple[SpecOperation, ...] = field(default_factory=tuple)
    secondary_apis: tuple[SecondaryApi, ...] = field(default_factory=tuple)

    @property
    def api_id(self) -> str:
        return f"provider-{self.connector_id.replace('_', '-')}-v1"

    @property
    def api_path(self) -> str:
        return f"providers/{self.connector_id.replace('_', '-')}"

    def secondary_api_id(self, suffix: str) -> str:
        return f"provider-{self.connector_id.replace('_', '-')}-{suffix}-v1"

    def secondary_api_path(self, suffix: str) -> str:
        return f"providers/{self.connector_id.replace('_', '-')}-{suffix}"

    @property
    def mcp_api_id(self) -> str:
        return f"provider-{self.connector_id.replace('_', '-')}-mcp-v1"

    @property
    def mcp_path(self) -> str:
        return f"provider-{self.connector_id.replace('_', '-')}-mcp"

    @property
    def server_label(self) -> str:
        return self.connector_id

    @property
    def is_published(self) -> bool:
        return self.published_spec_url is not None

    @property
    def spec_folder(self) -> str:
        return "official" if self.is_published else "authored"

    @property
    def spec_file(self) -> str:
        return f"infra/provider-specs/{self.spec_folder}/{self.connector_id}.json"

    def secondary_spec_file(self, suffix: str) -> str:
        return f"infra/provider-specs/authored/{self.connector_id}_{suffix}.json"


def _q(
    name: str,
    description: str,
    *,
    required: bool = False,
    schema_type: Literal["string", "integer", "boolean"] = "string",
) -> SpecParameter:
    return SpecParameter(name, "query", description, required, schema_type)


def _p(name: str, description: str) -> SpecParameter:
    return SpecParameter(name, "path", description, True, "string")


# Every OpenAlex list endpoint accepts the same paging, filtering, and shaping set.
_OPENALEX_LIST = (
    _q("filter", "Filter expression using field:value; comma is AND, pipe is OR."),
    _q("search", "Full-text search across titles, abstracts, and other text fields."),
    _q("sort", "Sort field; prefix with '-' for descending order."),
    _q("page", "Page number; basic paging caps at 10000 results.", schema_type="integer"),
    _q("per_page", "Results per page between 1 and 100.", schema_type="integer"),
    _q("cursor", "Deep-paging cursor; start with '*' and follow meta.next_cursor."),
    _q("sample", "Return a random sample of N results; cannot combine with sort or page.", schema_type="integer"),
    _q("select", "Comma-separated allowlist of fields to return."),
    _q("group_by", "Aggregate results by a field and return counts."),
    _q("mailto", "Contact email that routes the request to the polite pool."),
    _q("api_key", "OpenAlex API key, when one has been issued."),
)

_OPENALEX_ENTITIES: tuple[tuple[str, str, str], ...] = (
    ("works", "Work", "scholarly documents"),
    ("authors", "Author", "researcher profiles"),
    ("sources", "Source", "journals, repositories, and conferences"),
    ("institutions", "Institution", "universities and research organizations"),
    ("publishers", "Publisher", "publishing organizations"),
    ("funders", "Funder", "funding agencies"),
    ("topics", "Topic", "research topics"),
    ("subfields", "Subfield", "topic subfields"),
    ("fields", "Field", "topic fields"),
    ("domains", "Domain", "topic domains"),
    ("concepts", "Concept", "legacy research concepts"),
    ("keywords", "Keyword", "keywords"),
)


def _openalex_operations() -> tuple[SpecOperation, ...]:
    operations: list[SpecOperation] = []
    for entity, singular, plural_description in _OPENALEX_ENTITIES:
        operations.append(
            SpecOperation(
                f"openalexList{singular}s",
                "GET",
                f"/{entity}",
                f"List and filter OpenAlex {plural_description}.",
                _OPENALEX_LIST,
            )
        )
        operations.append(
            SpecOperation(
                f"openalexGet{singular}",
                "GET",
                f"/{entity}/{{id}}",
                f"Retrieve a single OpenAlex entity from {plural_description}.",
                (
                    _p("id", "OpenAlex ID or a supported external identifier."),
                    _q("select", "Comma-separated allowlist of fields to return."),
                    _q("mailto", "Contact email that routes the request to the polite pool."),
                    _q("api_key", "OpenAlex API key, when one has been issued."),
                ),
            )
        )
        operations.append(
            SpecOperation(
                f"openalexAutocomplete{singular}s",
                "GET",
                f"/autocomplete/{entity}",
                f"Autocomplete OpenAlex {plural_description} by partial name.",
                (
                    _q("q", "Partial entity name to complete.", required=True),
                    _q("mailto", "Contact email that routes the request to the polite pool."),
                    _q("api_key", "OpenAlex API key, when one has been issued."),
                ),
            )
        )
    operations.append(
        SpecOperation(
            "openalexAutocomplete",
            "GET",
            "/autocomplete",
            "Autocomplete across all OpenAlex entity types.",
            (
                _q("q", "Partial entity name to complete.", required=True),
                _q("mailto", "Contact email that routes the request to the polite pool."),
                _q("api_key", "OpenAlex API key, when one has been issued."),
            ),
        )
    )
    operations.append(
        SpecOperation(
            "openalexText",
            "GET",
            "/text",
            "Infer topics, keywords, and concepts from supplied text.",
            (
                _q("title", "Title text to analyze."),
                _q("abstract", "Abstract text to analyze."),
                _q("fulltext", "Full text to analyze."),
                _q("mailto", "Contact email that routes the request to the polite pool."),
                _q("api_key", "OpenAlex API key, when one has been issued."),
            ),
        )
    )
    return tuple(operations)


_OAI_METADATA_PREFIX = _q("metadataPrefix", "Metadata format: oai_dc, arXiv, or arXivRaw.")
_OAI_RESUMPTION = _q("resumptionToken", "Flow-control token from a previous response; exclusive of other arguments.")
_OAI_SELECTIVE = (
    _q("from", "Lower-bound UTC datestamp as YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ."),
    _q("until", "Upper-bound UTC datestamp as YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ."),
    _q("set", "setSpec for selective harvesting, for example physics:hep-th."),
)


def _arxiv_oai_operations() -> tuple[SpecOperation, ...]:
    """All six OAI-PMH verbs share GET /oai, so they are one operation keyed by `verb`."""
    return (
        SpecOperation(
            "arxivOaiRequest",
            "GET",
            "/oai",
            "Issue an OAI-PMH 2.0 request; the verb selects the harvesting operation.",
            (
                _q(
                    "verb",
                    "OAI-PMH verb: Identify, ListMetadataFormats, ListSets, "
                    "ListIdentifiers, ListRecords, or GetRecord.",
                    required=True,
                ),
                _q("identifier", "OAI identifier, required for GetRecord, for example oai:arXiv.org:0804.2273."),
                _q("metadataPrefix", "Metadata format: oai_dc, arXiv, or arXivRaw."),
                *_OAI_SELECTIVE,
                _OAI_RESUMPTION,
            ),
        ),
    )


# Required on every E-utility request by NCBI usage policy.
_NCBI_COMMON = (
    _q("tool", "Registered application name; required by NCBI usage policy."),
    _q("email", "Contact email; required by NCBI usage policy."),
    _q("api_key", "NCBI API key raising the rate limit from 3 to 10 requests per second."),
)
_NCBI_HISTORY = (
    _q("WebEnv", "Entrez History web environment string from a previous call."),
    _q("query_key", "Entrez History query key identifying the input UID set.", schema_type="integer"),
)
_NCBI_DATE = (
    _q("datetype", "Date type used to limit results, for example mdat, pdat, or edat."),
    _q("reldate", "Limit to items dated within the last n days.", schema_type="integer"),
    _q("mindate", "Range start as YYYY/MM/DD, YYYY/MM, or YYYY; requires maxdate."),
    _q("maxdate", "Range end as YYYY/MM/DD, YYYY/MM, or YYYY; requires mindate."),
)

_PROVIDERS: tuple[ProviderApi, ...] = (
    ProviderApi(
        connector_id="pubmed",
        display_name="NCBI PubMed E-utilities",
        documentation_url="https://www.ncbi.nlm.nih.gov/books/NBK25499/",
        server_url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
        description="NCBI Entrez Programming Utilities for biomedical literature.",
        operations=(
            SpecOperation(
                "pubmedEInfo",
                "GET",
                "/einfo.fcgi",
                "List valid Entrez databases, or indexing fields and statistics for one database.",
                (
                    _q("db", "Entrez database to describe; omit to list all databases."),
                    _q("version", "Set to 2.0 for extended EInfo XML."),
                    _q("retmode", "Output format: xml (default) or json."),
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedESearch",
                "GET",
                "/esearch.fcgi",
                "Search an Entrez database and return matching UIDs.",
                (
                    _q("db", "Entrez database to search.", required=True),
                    _q("term", "Entrez text query.", required=True),
                    _q("usehistory", "Set to y to post results to the Entrez History server."),
                    *_NCBI_HISTORY,
                    _q("retstart", "Index of the first UID returned.", schema_type="integer"),
                    _q("retmax", "Number of UIDs returned, maximum 10000.", schema_type="integer"),
                    _q("rettype", "Retrieval type: uilist (default) or count."),
                    _q("retmode", "Output format: xml (default) or json."),
                    _q("sort", "Sort order, for example relevance or pub_date."),
                    _q("field", "Limit the entire query to one Entrez field."),
                    _q("idtype", "Set to acc to return accession.version for sequence databases."),
                    *_NCBI_DATE,
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedEPost",
                "POST",
                "/epost.fcgi",
                "Upload UIDs to the transient Entrez History server and return a WebEnv and query key.",
                (
                    _q("db", "Entrez database containing the UIDs.", required=True),
                    _q("id", "Comma-delimited UID list, maximum 10000.", required=True),
                    _q("WebEnv", "Existing web environment to append to."),
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedESummary",
                "GET",
                "/esummary.fcgi",
                "Return document summaries for UIDs or an Entrez History set.",
                (
                    _q("db", "Entrez database to summarize.", required=True),
                    _q("id", "Comma-delimited UID list."),
                    *_NCBI_HISTORY,
                    _q("retstart", "Index of the first summary returned.", schema_type="integer"),
                    _q("retmax", "Number of summaries returned, maximum 10000.", schema_type="integer"),
                    _q("retmode", "Output format: xml (default) or json."),
                    _q("version", "Set to 2.0 for extended DocSum XML."),
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedEFetch",
                "GET",
                "/efetch.fcgi",
                "Return formatted full records for UIDs or an Entrez History set.",
                (
                    _q("db", "Entrez database to fetch from.", required=True),
                    _q("id", "Comma-delimited UID list."),
                    *_NCBI_HISTORY,
                    _q("retmode", "Record format, for example text, xml, or asn.1."),
                    _q("rettype", "Record view, for example abstract or medline."),
                    _q("retstart", "Index of the first record returned.", schema_type="integer"),
                    _q("retmax", "Number of records returned, maximum 10000.", schema_type="integer"),
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedELink",
                "GET",
                "/elink.fcgi",
                "Return UIDs linked to an input UID set within or across Entrez databases.",
                (
                    _q("linkname", "Entrez link name of the form dbfrom_db_subset."),
                    _q("db", "Destination Entrez database."),
                    _q("dbfrom", "Origin Entrez database."),
                    _q("cmd", "Link command mode, for example neighbor or prlinks."),
                    _q("id", "Comma-delimited UID list from dbfrom."),
                    *_NCBI_HISTORY,
                    _q("retmode", "Output format: xml (default), json, or ref."),
                    _q("idtype", "Set to acc to return accession.version for sequence databases."),
                    _q("term", "Entrez query applied after linking."),
                    _q("holding", "Restrict LinkOut results to one provider abbreviation."),
                    *_NCBI_DATE,
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedEGQuery",
                "GET",
                "/egquery.fcgi",
                "Return record counts for a text query across every Entrez database.",
                (_q("term", "Entrez text query.", required=True), *_NCBI_COMMON),
            ),
            SpecOperation(
                "pubmedESpell",
                "GET",
                "/espell.fcgi",
                "Return spelling suggestions for the terms in an Entrez query.",
                (
                    _q("db", "Entrez database to check against.", required=True),
                    _q("term", "Entrez text query.", required=True),
                    *_NCBI_COMMON,
                ),
            ),
            SpecOperation(
                "pubmedECitMatch",
                "GET",
                "/ecitmatch.cgi",
                "Resolve citation strings to PubMed identifiers.",
                (
                    _q("db", "Only pubmed is supported.", required=True),
                    _q("rettype", "Only xml is supported.", required=True),
                    _q("bdata", "Pipe-delimited citation strings separated by carriage returns.", required=True),
                    *_NCBI_COMMON,
                ),
            ),
        ),
    ),
    ProviderApi(
        connector_id="europe_pmc",
        display_name="Europe PMC Articles REST API",
        documentation_url="https://europepmc.org/RestfulWebService",
        server_url="https://www.ebi.ac.uk/europepmc/webservices/rest",
        description="Life-science literature metadata, citations, and data links.",
        published_spec_url="https://www.ebi.ac.uk/europepmc/webservices/api/swagger.json",
    ),
    ProviderApi(
        connector_id="crossref",
        display_name="Crossref REST API",
        documentation_url="https://api.crossref.org/swagger-docs",
        server_url="https://api.crossref.org",
        description="Publisher-deposited DOI metadata for scholarly works.",
        published_spec_url="https://api.crossref.org/swagger-docs",
    ),
    ProviderApi(
        connector_id="openalex",
        display_name="OpenAlex API",
        documentation_url="https://developers.openalex.org/api-reference/introduction",
        server_url="https://api.openalex.org",
        description="Open catalog of scholarly works, authors, sources, and institutions.",
        # The official 185 KB spec is rejected by APIM import, so the documented
        # surface is authored here instead.
        operations=_openalex_operations(),
    ),
    ProviderApi(
        connector_id="arxiv",
        display_name="arXiv API",
        documentation_url="https://info.arxiv.org/help/api/user-manual.html",
        server_url="https://export.arxiv.org",
        description="Preprint metadata returned as an Atom 1.0 XML feed.",
        operations=(
            SpecOperation(
                "arxivQuery",
                "GET",
                "/api/query",
                "Search arXiv or resolve identifiers, returning an Atom 1.0 XML feed.",
                (
                    _q("search_query", "Field-prefixed search expression, for example all:electron."),
                    _q("id_list", "Comma-delimited arXiv identifiers, optionally version-suffixed."),
                    _q("start", "Zero-based index of the first result.", schema_type="integer"),
                    _q("max_results", "Number of results to return; capped at 30000.", schema_type="integer"),
                    _q("sortBy", "Sort field: relevance, lastUpdatedDate, or submittedDate."),
                    _q("sortOrder", "Sort direction: ascending or descending."),
                ),
            ),
        ),
        secondary_apis=(
            SecondaryApi(
                suffix="oai",
                # Base URL moved from export.arxiv.org/oai2 in March 2025.
                server_url="https://oaipmh.arxiv.org",
                description="arXiv OAI-PMH 2.0 metadata harvesting interface.",
                operations=_arxiv_oai_operations(),
            ),
            SecondaryApi(
                suffix="feeds",
                server_url="https://rss.arxiv.org",
                description="arXiv daily announcement feeds.",
                operations=(
                    SpecOperation(
                        "arxivRssFeed",
                        "GET",
                        "/rss/{category}",
                        "Daily RSS 2.0 feed of new and cross-listed announcements for a category.",
                        (_p("category", "Archive, subject class, or several joined by '+'."),),
                    ),
                    SpecOperation(
                        "arxivAtomFeed",
                        "GET",
                        "/atom/{category}",
                        "Daily Atom feed of new and cross-listed announcements for a category.",
                        (_p("category", "Archive, subject class, or several joined by '+'."),),
                    ),
                ),
            ),
        ),
    ),
    ProviderApi(
        connector_id="clinical_trials",
        display_name="ClinicalTrials.gov API v2",
        documentation_url="https://clinicaltrials.gov/data-api/api",
        server_url="https://clinicaltrials.gov/api/v2",
        description="Clinical study records published by the U.S. National Library of Medicine.",
        published_spec_url="https://clinicaltrials.gov/api/oas/v2",
    ),
    ProviderApi(
        connector_id="grants_gov",
        display_name="Grants.gov Search API",
        documentation_url="https://www.grants.gov/api/api-guide",
        server_url="https://api.grants.gov/v1/api",
        description="U.S. federal funding opportunity search and retrieval.",
        operations=(
            SpecOperation(
                "grantsGovSearch2",
                "POST",
                "/search2",
                "Search federal funding opportunities by keyword, agency, status, and category.",
                (),
                True,
            ),
            SpecOperation(
                "grantsGovFetchOpportunity",
                "POST",
                "/fetchOpportunity",
                "Retrieve one funding opportunity by its identifier.",
                (),
                True,
            ),
        ),
    ),
    ProviderApi(
        connector_id="nih_reporter",
        display_name="NIH RePORTER API v2",
        documentation_url="https://api.reporter.nih.gov/",
        server_url="https://api.reporter.nih.gov",
        description="NIH-funded project and publication metadata.",
        published_spec_url="https://api.reporter.nih.gov/swagger/v2/swagger.json",
    ),
    ProviderApi(
        connector_id="datacite",
        display_name="DataCite REST API",
        documentation_url="https://support.datacite.org/docs/api",
        server_url="https://api.datacite.org",
        description="DOI metadata for datasets, software, and other research outputs.",
        published_spec_url="https://raw.githubusercontent.com/datacite/lupo/master/openapi.yaml",
    ),
    ProviderApi(
        connector_id="orcid",
        display_name="ORCID Public API v3.0",
        documentation_url="https://info.orcid.org/documentation/api-tutorials/",
        server_url="https://pub.orcid.org/v3.0",
        description="Public researcher identifier, affiliation, and work records.",
        operations=(
            SpecOperation(
                "orcidExpandedSearch",
                "GET",
                "/expanded-search/",
                "Search ORCID records and return expanded summary fields.",
                (
                    _q("q", "Solr/Lucene ORCID search expression.", required=True),
                    _q("start", "Zero-based result offset.", schema_type="integer"),
                    _q("rows", "Number of rows to return, maximum 1000.", schema_type="integer"),
                ),
            ),
            SpecOperation(
                "orcidSearch",
                "GET",
                "/search/",
                "Search ORCID records and return matching identifiers.",
                (
                    _q("q", "Solr/Lucene ORCID search expression.", required=True),
                    _q("start", "Zero-based result offset.", schema_type="integer"),
                    _q("rows", "Number of rows to return, maximum 1000.", schema_type="integer"),
                ),
            ),
            SpecOperation(
                "orcidGetRecord",
                "GET",
                "/{orcid}/record",
                "Retrieve the complete public ORCID record.",
                (_p("orcid", "ORCID iD, for example 0000-0002-1825-0097."),),
            ),
            SpecOperation(
                "orcidGetPerson",
                "GET",
                "/{orcid}/person",
                "Retrieve the person section of a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetWorks",
                "GET",
                "/{orcid}/works",
                "List the works on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetEmployments",
                "GET",
                "/{orcid}/employments",
                "List employment affiliations on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetEducations",
                "GET",
                "/{orcid}/educations",
                "List education affiliations on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetFundings",
                "GET",
                "/{orcid}/fundings",
                "List funding entries on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetPeerReviews",
                "GET",
                "/{orcid}/peer-reviews",
                "List peer-review activity on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
            SpecOperation(
                "orcidGetResearchResources",
                "GET",
                "/{orcid}/research-resources",
                "List research resources on a public ORCID record.",
                (_p("orcid", "ORCID iD."),),
            ),
        ),
    ),
    ProviderApi(
        connector_id="ror",
        display_name="ROR API v2",
        documentation_url="https://ror.readme.io/v2/docs/rest-api",
        server_url="https://api.ror.org",
        description="Open identifiers and metadata for research organizations.",
        operations=(
            SpecOperation(
                "rorListOrganizations",
                "GET",
                "/v2/organizations",
                "Search or filter research organization records.",
                (
                    _q("query", "Keyword matched against organization names and external identifiers."),
                    _q("query.advanced", "Elasticsearch query string searching all record fields."),
                    _q("affiliation", "Unstructured affiliation string to match against ROR records."),
                    _q("filter", "Comma-separated field:value filters, for example status:active."),
                    _q("page", "1-based page number; maximum 500.", schema_type="integer"),
                    _q("all_status", "Include inactive and withdrawn records.", schema_type="boolean"),
                ),
            ),
            SpecOperation(
                "rorGetOrganization",
                "GET",
                "/v2/organizations/{ror_id}",
                "Retrieve one research organization record by ROR identifier.",
                (_p("ror_id", "ROR identifier, for example 015w2mp89."),),
            ),
            SpecOperation(
                "rorHeartbeat",
                "GET",
                "/heartbeat",
                "Report ROR API availability.",
                (),
            ),
        ),
    ),
    ProviderApi(
        connector_id="semantic_scholar",
        display_name="Semantic Scholar Academic Graph API",
        documentation_url="https://api.semanticscholar.org/api-docs/graph",
        server_url="https://api.semanticscholar.org/graph/v1",
        description="Paper, author, and citation graph metadata.",
        published_spec_url="https://api.semanticscholar.org/graph/v1/swagger.json",
    ),
)


def provider_apis() -> tuple[ProviderApi, ...]:
    return _PROVIDERS


def provider_api(connector_id: str) -> ProviderApi:
    normalized = connector_id.strip().casefold()
    for provider in _PROVIDERS:
        if provider.connector_id == normalized:
            return provider
    raise ValueError(f"No provider API is registered for '{connector_id}'")
