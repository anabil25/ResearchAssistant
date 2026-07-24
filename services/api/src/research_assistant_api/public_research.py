from __future__ import annotations

import asyncio
from typing import Any

from research_assistant_core.models import Capability

from research_assistant_api.connector_gateway import (
    ConnectorGateway,
    ConnectorGatewayError,
    ConnectorGatewayNotConfiguredError,
)
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

# The only two `ConnectorSetting.test_status` values that represent a
# genuinely usable connector. Mirrors the identical allowlist already used
# by `WorkspaceStore` (see workspace.py's readiness check) so both layers
# agree: `configuration_required`/`unavailable`/any other value are never
# treated as ready here, even if `connector.enabled` is true -- an enabled
# connector whose last test came back configuration_required/unavailable
# must not be offered to callers as if it could actually serve a request.
_READY_TEST_STATUSES = {"ready", "ready_with_key"}


def _rejection_reason(connector: ConnectorSetting, logical_agent: str) -> str:
    """Explain, in a caller-facing string, why an explicitly requested
    source was rejected rather than silently dropped. Distinguishes the
    three independent reasons a connector can fail the readiness gate so
    the message accurately reflects which one applies."""
    if not connector.enabled:
        return f"The '{connector.id}' connector is disabled for this workspace."
    if logical_agent not in connector.assigned_agents:
        return f"The '{connector.id}' connector is not assigned to this capability."
    return (
        f"The '{connector.id}' connector's last test status is "
        f"'{connector.test_status}', which is not ready."
    )


async def _retrieve_one(
    capability: Capability,
    source: str,
    query: str,
    gateway: ConnectorGateway,
) -> dict[str, Any]:
    try:
        result = await gateway.search(capability, source, query, limit=3)
    except ConnectorGatewayNotConfiguredError as exc:
        return {
            "source": source,
            "status": "configuration_required",
            "error": str(exc),
            "records": [],
        }
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
        "terms_url": str(result.terms_url),
        "retrieved_from": str(result.retrieved_from),
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
    connectors_by_id = {connector.id: connector for connector in connectors}
    # Readiness-gated on purpose, not just `enabled`: an enabled connector
    # whose stored `test_status` is `configuration_required`/`unavailable`/
    # anything outside `_READY_TEST_STATUSES` must not be offered here --
    # otherwise the UI's ready/ready_with_key vs. configuration_required/
    # unavailable vocabulary could be bypassed simply by an operator
    # flipping `enabled` back on without the connector actually having
    # retested as ready.
    enabled = {
        connector.id
        for connector in connectors
        if connector.enabled
        and logical_agent in connector.assigned_agents
        and connector.test_status in _READY_TEST_STATUSES
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
    capability_sources = _CAPABILITY_SOURCES.get(capability, ())
    selected = [source for source in capability_sources if source in enabled and source in candidates]

    # An explicitly requested source (as opposed to one silently drawn from
    # the capability's own default set) that names a connector the server
    # does not consider ready must be reported back with the connector's
    # real, authoritative status rather than silently omitted from the
    # response -- the server remains the enforcement boundary even against
    # a UI bug or a client that bypasses the readiness gate client-side.
    # The default (non-explicit) source set never triggers this: an
    # unready default source is simply excluded from `selected`, exactly
    # as before, since nothing was explicitly asked for.
    rejected: list[dict[str, Any]] = []
    if requested_ids is not None:
        # An explicitly requested source ID that is not part of this
        # capability's own vocabulary at all -- either genuinely unrecognized
        # (e.g. a typo) or a real source name that simply belongs to a
        # different capability (e.g. "pubmed" requested under Capability.GRANT)
        # -- must be reported back too. The loop below this one only walks
        # `capability_sources`, so without this check such an ID would
        # silently vanish from both `selected` and `rejected`, leaving the
        # caller with an empty response and no signal anything was wrong
        # with their request. Sorted for deterministic output/tests.
        for source in sorted(candidates - set(capability_sources)):
            rejected.append(
                {
                    "source": source,
                    "status": "unsupported",
                    "error": (
                        f"'{source}' is not a supported source for the "
                        f"'{logical_agent}' capability."
                    ),
                    "records": [],
                }
            )
        for source in capability_sources:
            if source not in candidates or source in enabled:
                continue
            connector = connectors_by_id.get(source)
            if connector is None:
                rejected.append(
                    {
                        "source": source,
                        "status": "unavailable",
                        "error": f"The '{source}' connector is not configured for this workspace.",
                        "records": [],
                    }
                )
                continue
            rejected.append(
                {
                    "source": source,
                    "status": connector.test_status,
                    "error": _rejection_reason(connector, logical_agent),
                    "records": [],
                }
            )

    fetched = await asyncio.gather(
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
    return [*rejected, *fetched]
