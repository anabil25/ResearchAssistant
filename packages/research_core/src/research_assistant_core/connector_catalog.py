"""Reviewed connector metadata used to compose governed tool surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OperationClass = Literal["read", "create", "update", "delete"]
CredentialKind = Literal["none", "api_key"]


@dataclass(frozen=True, slots=True)
class ConnectorCredential:
    kind: CredentialKind
    required: bool
    #: Upstream header the gateway injects; empty when no credential applies.
    header: str = ""
    #: APIM named value holding the secret, so it never reaches the app store.
    named_value: str = ""
    help_url: str = ""


NO_CREDENTIAL = ConnectorCredential(kind="none", required=False)
#: Sentinel stored in the APIM named value while no operator key is configured.
UNCONFIGURED_CREDENTIAL = "unset"


@dataclass(frozen=True, slots=True)
class ConnectorOperation:
    id: str
    mcp_tool_name: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    operation_class: OperationClass

    @property
    def apim_tool_name(self) -> str:
        """APIM requires tool resource names to be unique across the whole service."""
        return f"research{self.id[:1].upper()}{self.id[1:]}"


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    id: str
    name: str
    category: str
    description: str
    assigned_agents: tuple[str, ...]
    terms_url: str
    capabilities: tuple[str, ...]
    auth_kind: str = "None"
    secret_status: str = "Not required"
    test_status: str = "ready"
    data_boundary: str = "Public metadata only; query text is sent to the provider."
    probe_query: str = "research reproducibility"
    credential: ConnectorCredential = NO_CREDENTIAL
    operations: tuple[ConnectorOperation, ...] = ()

    @property
    def apim_mcp_api_id(self) -> str:
        return f"research-{self.id.replace('_', '-')}-mcp-v1"

    @property
    def apim_mcp_path(self) -> str:
        return f"research-{self.id.replace('_', '-')}-mcp"

    @property
    def toolbox_connection_id(self) -> str:
        return f"research-connector-{self.id.replace('_', '-')}-apim"


def _search_operation(connector_id: str) -> ConnectorOperation:
    first, *remaining = connector_id.split("_")
    return ConnectorOperation(
        id=f"{first}{''.join(part.capitalize() for part in remaining)}Search",
        mcp_tool_name="search",
        method="POST",
        path=f"/v1/connectors/{connector_id}/search",
        operation_class="read",
    )


def _pubmed_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="pubmedLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/pubmed/records/{pmid}",
        operation_class="read",
    )


def _crossref_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="crossrefLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/crossref/works/{doi:path}",
        operation_class="read",
    )


def _europe_pmc_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="europePmcLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/europe_pmc/articles/{source}/{article_id}",
        operation_class="read",
    )


def _clinical_trials_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="clinicalTrialsLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/clinical_trials/studies/{nct_id}",
        operation_class="read",
    )


def _datacite_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="dataciteLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/datacite/dois/{doi:path}",
        operation_class="read",
    )


def _openalex_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="openalexLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/openalex/works/{work_id}",
        operation_class="read",
    )


def _ror_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="rorLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/ror/organizations/{ror_id}",
        operation_class="read",
    )


def _arxiv_lookup_operation() -> ConnectorOperation:
    return ConnectorOperation(
        id="arxivLookup",
        mcp_tool_name="lookup",
        method="GET",
        path="/v1/connectors/arxiv/records/{arxiv_id}",
        operation_class="read",
    )


_CONNECTORS: tuple[ConnectorDefinition, ...] = (
    ConnectorDefinition(
        id="pubmed",
        name="PubMed",
        category="Literature",
        description="Biomedical citations and abstracts from NCBI.",
        assigned_agents=("literature",),
        terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        capabilities=("Search", "Metadata"),
        operations=(
            _search_operation("pubmed"),
            _pubmed_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="europe_pmc",
        name="Europe PMC",
        category="Literature",
        description="Life-sciences publications, grants, and links.",
        assigned_agents=("literature",),
        terms_url="https://europepmc.org/terms",
        capabilities=("Search", "Metadata"),
        operations=(
            _search_operation("europe_pmc"),
            _europe_pmc_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="crossref",
        name="Crossref",
        category="Literature",
        description="DOI metadata and scholarly work resolution.",
        assigned_agents=("literature", "grant"),
        terms_url="https://www.crossref.org/services/metadata-delivery/rest-api/",
        capabilities=("DOI resolution", "Metadata"),
        operations=(
            _search_operation("crossref"),
            _crossref_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="openalex",
        name="OpenAlex",
        category="Discovery",
        description="Open catalog of works, people, venues, and institutions.",
        assigned_agents=("literature", "matching", "dataset"),
        terms_url="https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication",
        capabilities=("Search", "Entity leads"),
        operations=(
            _search_operation("openalex"),
            _openalex_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="arxiv",
        name="arXiv",
        category="Literature",
        description="Preprint metadata for supported disciplines.",
        assigned_agents=("literature",),
        terms_url="https://info.arxiv.org/help/api/tou.html",
        capabilities=("Search", "Preprints"),
        operations=(
            _search_operation("arxiv"),
            _arxiv_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="clinical_trials",
        name="ClinicalTrials.gov",
        category="Clinical research",
        description="Clinical study records from the U.S. NLM.",
        assigned_agents=("literature",),
        terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
        capabilities=("Trials", "Metadata"),
        operations=(
            _search_operation("clinical_trials"),
            _clinical_trials_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="grants_gov",
        name="Grants.gov",
        category="Funding",
        description="Authoritative U.S. federal opportunity records.",
        assigned_agents=("grant",),
        terms_url="https://www.grants.gov/web/grants/legal-privacy.html",
        capabilities=("Opportunities", "Requirements"),
        operations=(_search_operation("grants_gov"),),
    ),
    ConnectorDefinition(
        id="nih_reporter",
        name="NIH RePORTER",
        category="Funding",
        description="NIH funded-project and investigator metadata.",
        assigned_agents=("grant", "matching"),
        terms_url="https://reporter.nih.gov/termsconditions",
        capabilities=("Awards", "Project leads"),
        operations=(_search_operation("nih_reporter"),),
    ),
    ConnectorDefinition(
        id="datacite",
        name="DataCite",
        category="Datasets",
        description="DOI metadata for datasets and research outputs.",
        assigned_agents=("literature", "dataset"),
        terms_url="https://support.datacite.org/docs/terms-and-conditions",
        capabilities=("Dataset discovery", "DOI resolution"),
        operations=(
            _search_operation("datacite"),
            _datacite_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="orcid",
        name="ORCID",
        category="Identity",
        description="Public researcher identifier records.",
        assigned_agents=("matching",),
        terms_url="https://info.orcid.org/terms-of-use/",
        capabilities=("Identity resolution",),
        operations=(_search_operation("orcid"),),
    ),
    ConnectorDefinition(
        id="ror",
        name="ROR",
        category="Identity",
        description="Open identifiers for research organizations.",
        assigned_agents=("matching",),
        terms_url="https://ror.org/terms/",
        capabilities=("Organization resolution",),
        operations=(
            _search_operation("ror"),
            _ror_lookup_operation(),
        ),
    ),
    ConnectorDefinition(
        id="semantic_scholar",
        name="Semantic Scholar",
        category="Literature",
        description="Paper and citation graph metadata.",
        assigned_agents=("literature",),
        terms_url="https://www.semanticscholar.org/product/api/license",
        capabilities=("Search", "Citation graph"),
        auth_kind="API key recommended",
        secret_status="Optional secret not configured",
        test_status="ready_with_key",
        credential=ConnectorCredential(
            kind="api_key",
            required=False,
            header="x-api-key",
            named_value="research-semantic-scholar-key",
            help_url="https://www.semanticscholar.org/product/api",
        ),
        operations=(_search_operation("semantic_scholar"),),
    ),
)


def connector_definitions() -> tuple[ConnectorDefinition, ...]:
    return _CONNECTORS


def connector_ids() -> tuple[str, ...]:
    return tuple(connector.id for connector in _CONNECTORS)


def connector_definition(connector_id: str) -> ConnectorDefinition:
    normalized = connector_id.strip().casefold()
    for connector in _CONNECTORS:
        if connector.id == normalized:
            return connector
    raise ValueError(f"Unsupported connector '{connector_id}'. Allowed: {', '.join(connector_ids())}")