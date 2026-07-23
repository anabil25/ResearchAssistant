from __future__ import annotations

import asyncio
from typing import Any

from research_assistant_core.models import Capability

from research_assistant_api.connector_gateway import ConnectorGateway, ConnectorGatewayError
from research_assistant_api.workspace import ConnectorSetting

_DEFAULT_SOURCES: dict[Capability, tuple[str, ...]] = {
    Capability.LITERATURE: ("pubmed", "crossref", "openalex"),
    Capability.GRANT: ("grants_gov", "nih_reporter"),
    Capability.MATCHING: ("openalex", "ror", "orcid"),
}
_CAPABILITY_SOURCES: dict[Capability, tuple[str, ...]] = {
    Capability.LITERATURE: (
        "pubmed",
        "europe_pmc",
        "crossref",
        "openalex",
        "arxiv",
        "clinical_trials",
        "datacite",
        "semantic_scholar",
    ),
    Capability.GRANT: (
        "grants_gov",
        "nih_reporter",
        "crossref",
        "openalex",
    ),
    Capability.MATCHING: (
        "openalex",
        "orcid",
        "ror",
        "nih_reporter",
    ),
}

_SOURCE_ALIASES = {
    "pubmed": "pubmed",
    "europe pmc": "europe_pmc",
    "crossref": "crossref",
    "openalex": "openalex",
    "arxiv": "arxiv",
    "clinicaltrials.gov": "clinical_trials",
    "grants.gov": "grants_gov",
    "nih reporter": "nih_reporter",
    "datacite": "datacite",
    "orcid": "orcid",
    "ror": "ror",
    "semantic scholar": "semantic_scholar",
}


async def _retrieve_one(
    capability: Capability,
    source: str,
    query: str,
    gateway: ConnectorGateway,
) -> dict[str, Any]:
    try:
        result = await gateway.search(capability, source, query, limit=3)
    except ConnectorGatewayError as exc:
        return {
            "source": source,
            "status": "unavailable",
            "error": str(exc),
            "records": [],
        }
    return {
        "source": result.source.value,
        "status": "ready",
        "terms_url": result.terms_url,
        "retrieved_from": result.retrieved_from,
        "warnings": result.warnings,
        "records": result.records,
    }


async def retrieve_public_metadata(
    capability: Capability,
    query: str,
    connectors: list[ConnectorSetting],
    *,
    gateway: ConnectorGateway,
    requested_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    logical_agent = capability.value
    enabled = {
        connector.id for connector in connectors if connector.enabled and logical_agent in connector.assigned_agents
    }
    requested_ids = (
        {_SOURCE_ALIASES.get(source.casefold(), source.casefold()) for source in requested_sources}
        if requested_sources is not None
        else None
    )
    candidates = (
        requested_ids
        if requested_ids is not None
        else set(_DEFAULT_SOURCES.get(capability, ()))
    )
    selected = [
        source
        for source in _CAPABILITY_SOURCES.get(capability, ())
        if source in enabled and source in candidates
    ]
    return list(
        await asyncio.gather(
            *(
                _retrieve_one(
                    capability,
                    source,
                    query,
                    gateway,
                )
                for source in selected
            )
        )
    )
