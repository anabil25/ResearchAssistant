from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ConnectorGatewayContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicConnectorSource(StrEnum):
    PUBMED = "pubmed"
    EUROPE_PMC = "europe_pmc"
    CROSSREF = "crossref"
    OPENALEX = "openalex"
    ARXIV = "arxiv"
    CLINICAL_TRIALS = "clinical_trials"
    GRANTS_GOV = "grants_gov"
    NIH_REPORTER = "nih_reporter"
    DATACITE = "datacite"
    ORCID = "orcid"
    ROR = "ror"
    SEMANTIC_SCHOLAR = "semantic_scholar"


class LiteratureConnectorSource(StrEnum):
    PUBMED = "pubmed"
    EUROPE_PMC = "europe_pmc"
    CROSSREF = "crossref"
    OPENALEX = "openalex"
    ARXIV = "arxiv"
    CLINICAL_TRIALS = "clinical_trials"
    DATACITE = "datacite"
    SEMANTIC_SCHOLAR = "semantic_scholar"


class GrantConnectorSource(StrEnum):
    GRANTS_GOV = "grants_gov"
    NIH_REPORTER = "nih_reporter"
    CROSSREF = "crossref"
    OPENALEX = "openalex"


class MatchingConnectorSource(StrEnum):
    OPENALEX = "openalex"
    ORCID = "orcid"
    ROR = "ror"
    NIH_REPORTER = "nih_reporter"


class ConnectorSearchRequest(ConnectorGatewayContract):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=3, ge=1, le=10)


class LiteratureSearchRequest(ConnectorSearchRequest):
    source: LiteratureConnectorSource


class GrantSearchRequest(ConnectorSearchRequest):
    source: GrantConnectorSource


class MatchingSearchRequest(ConnectorSearchRequest):
    source: MatchingConnectorSource


class ConnectorSearchResponse(ConnectorGatewayContract):
    source: PublicConnectorSource
    query: str
    records: list[dict[str, Any]]
    terms_url: HttpUrl
    retrieved_from: HttpUrl
    warnings: list[str] = Field(default_factory=list)
    notice: str = (
        "Metadata only. Verify source rights, current status, and full text "
        "before using a record as research evidence."
    )


class ConnectorDescriptor(ConnectorGatewayContract):
    id: PublicConnectorSource
    description: str
    capabilities: list[str]


class ConnectorCatalogResponse(ConnectorGatewayContract):
    schema_version: str = "research-assistant.connector-gateway.v1"
    connectors: list[ConnectorDescriptor]


class ConnectorHealthResponse(ConnectorGatewayContract):
    status: str
    service: str = "research-assistant-connector-adapter"
    schema_version: str = "research-assistant.connector-gateway.v1"
