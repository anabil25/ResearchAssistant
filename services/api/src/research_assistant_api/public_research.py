from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from research_assistant_core.models import Capability, PublicDiscoveryRequest

from research_assistant_api.connector_gateway import (
    ConnectorGateway,
    ConnectorGatewayError,
    ConnectorGatewayNotConfiguredError,
)
from research_assistant_api.workspace import ConnectorSetting

#: Connector ``test_status`` values that mean a connector is actually usable
#: right now. Any other value (``"configuration_required"``, ``"unavailable"``,
#: or an unrecognized future value) means the connector is not ready and must
#: not be selected for a live run, regardless of ``enabled``/assignment.
_READY_STATUSES = frozenset({"ready", "ready_with_key"})

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
# (The canonical set is ``_READY_STATUSES``, defined above; the duplicate
# ``_READY_TEST_STATUSES`` that arrived on the state lineage was removed at
# integration so the two cannot drift apart.)


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


@dataclass(frozen=True)
class ConnectorSourceViolation:
    """One requested connector identifier that failed deterministic
    authorization/readiness validation against the tenant/project connector
    registry.

    ``reason`` is one of the literal strings ``"unknown"`` (does not resolve
    to a connector registered for this capability), ``"duplicate"`` (the
    same canonical connector was requested more than once), ``"disabled"``
    (registered but not enabled for this project), ``"not_assigned"``
    (registered/enabled but not assigned to the target capability/agent),
    or the connector's own actual ``test_status`` value (e.g.
    ``"configuration_required"``/``"unavailable"``) when it resolves but is
    not currently ready.
    """

    requested: str
    canonical_id: str | None
    reason: str
    detail: str


class ConnectorAuthorizationError(Exception):
    """Raised when one or more client-requested connector sources fail
    deterministic registry validation.

    Carries every violation found (not just the first) so the caller can
    return a single structured 4xx response describing all of them at once,
    rather than round-tripping one rejection per request.
    """

    def __init__(self, violations: Sequence[ConnectorSourceViolation]) -> None:
        self.violations: tuple[ConnectorSourceViolation, ...] = tuple(violations)
        super().__init__("; ".join(f"{v.requested}: {v.reason}" for v in self.violations))

    @property
    def is_authorization_failure(self) -> bool:
        """``True`` when at least one violation reflects that the caller is
        not permitted to use the connector (disabled/not assigned), as
        opposed to a pure request-shape problem (unknown/duplicate) or a
        transient readiness problem (the connector's own persisted
        status). Callers use this to choose HTTP 403 vs 422.
        """
        return any(violation.reason in {"disabled", "not_assigned"} for violation in self.violations)


@dataclass(frozen=True, slots=True)
class PublicDiscoveryAuthorization:
    """Server-resolved authorization for one public-discovery turn."""

    connector_ids: tuple[str, ...]
    public_context: str | None


def resolve_authorized_sources(
    capability: Capability,
    requested_sources: list[str] | None,
    connectors: list[ConnectorSetting],
    *,
    logical_agent: str | None = None,
) -> list[str] | None:
    """Deterministically resolve client-requested connector identifiers
    against the tenant/project connector registry *before* any live
    connector fetch runs.

    This must never be skipped because the UI already filters which
    connectors it shows: the request body is fully attacker-controlled, so
    server-side re-validation against the authoritative registry is the only
    trustworthy check.

    Returns ``None`` unchanged when ``requested_sources`` is ``None`` --
    that means the caller did not request an explicit subset, so the
    capability's server-chosen defaults apply and there is nothing
    client-supplied to validate. Otherwise returns the resolved, de-duplicated
    list of canonical connector IDs the caller is authorized to query.

    Raises ``ConnectorAuthorizationError`` (never partially -- every
    violation is collected before raising) when any requested identifier:

    * does not resolve to a connector registered for this capability
      (``"unknown"``);
    * is requested more than once after alias canonicalization
      (``"duplicate"``);
    * resolves to a connector that is not ``enabled`` for this project
      (``"disabled"``);
    * resolves to a connector not assigned to the target capability/agent
      (``"not_assigned"``);
    * resolves to a connector whose actual persisted ``test_status`` is not
      ``ready``/``ready_with_key`` -- the literal status is echoed back as
      the violation reason rather than collapsed into a generic one.

    Authorized *consent* to make an external call at all is intentionally
    not re-implemented here: it is already gated upstream by ``_online_policy``
    (``online_research`` + an explicit ``public_research_acknowledged`` flag),
    which callers must invoke before reaching this function.
    """
    if requested_sources is None:
        return None

    target_agent = logical_agent or capability.value
    by_id = {connector.id: connector for connector in connectors}
    known_ids = set(_CAPABILITY_SOURCES.get(capability, ()))

    violations: list[ConnectorSourceViolation] = []
    seen: set[str] = set()
    resolved: list[str] = []
    for raw in requested_sources:
        canonical = _SOURCE_ALIASES.get(raw.casefold(), raw.casefold())
        if canonical not in known_ids or canonical not in by_id:
            violations.append(
                ConnectorSourceViolation(
                    requested=raw,
                    canonical_id=None,
                    reason="unknown",
                    detail=f"'{raw}' does not resolve to a connector registered for {capability.value}.",
                )
            )
            continue
        if canonical in seen:
            violations.append(
                ConnectorSourceViolation(
                    requested=raw,
                    canonical_id=canonical,
                    reason="duplicate",
                    detail=f"'{raw}' (connector '{canonical}') was requested more than once.",
                )
            )
            continue
        seen.add(canonical)
        connector = by_id[canonical]
        if not connector.enabled:
            violations.append(
                ConnectorSourceViolation(
                    requested=raw,
                    canonical_id=canonical,
                    reason="disabled",
                    detail=f"Connector '{canonical}' is not enabled for this project.",
                )
            )
            continue
        if target_agent not in connector.assigned_agents:
            violations.append(
                ConnectorSourceViolation(
                    requested=raw,
                    canonical_id=canonical,
                    reason="not_assigned",
                    detail=f"Connector '{canonical}' is not assigned to '{target_agent}'.",
                )
            )
            continue
        if connector.test_status not in _READY_STATUSES:
            violations.append(
                ConnectorSourceViolation(
                    requested=raw,
                    canonical_id=canonical,
                    reason=connector.test_status,
                    detail=f"Connector '{canonical}' is not ready (status: {connector.test_status}).",
                )
            )
            continue
        resolved.append(canonical)

    if violations:
        raise ConnectorAuthorizationError(violations)
    return resolved


def select_authorized_sources(
    capability: Capability,
    requested_sources: list[str] | None,
    connectors: list[ConnectorSetting],
    *,
    logical_agent: str | None = None,
) -> tuple[str, ...]:
    """Resolve the full effective source set for one authorized public run.

    Explicit selection is rejected by :func:`resolve_authorized_sources` when
    it names a disabled, unassigned, unknown, duplicate, or unready connector.
    With no explicit selection, retain the existing server-selected defaults
    but quietly omit sources that are not currently ready for the capability.
    """
    resolved = resolve_authorized_sources(
        capability,
        requested_sources,
        connectors,
        logical_agent=logical_agent,
    )
    if resolved is not None:
        return tuple(resolved)

    target_agent = logical_agent or capability.value
    by_id = {connector.id: connector for connector in connectors}
    return tuple(
        source
        for source in _DEFAULT_SOURCES.get(capability, ())
        if (
            (connector := by_id.get(source)) is not None
            and connector.enabled
            and target_agent in connector.assigned_agents
            and connector.test_status in _READY_STATUSES
        )
    )


def authorize_public_discovery(
    capability: Capability,
    request: PublicDiscoveryRequest | None,
    connectors: list[ConnectorSetting],
) -> PublicDiscoveryAuthorization | None:
    """Resolve explicit per-turn public discovery against workspace policy."""
    if request is None:
        return None
    if capability not in _CAPABILITY_SOURCES:
        raise ValueError(f"{capability.value} does not support public discovery.")
    connector_ids = select_authorized_sources(
        capability,
        list(request.connector_ids) if request.connector_ids is not None else None,
        connectors,
    )
    return PublicDiscoveryAuthorization(
        connector_ids=connector_ids,
        public_context=request.public_context,
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
    # Defense-in-depth: even when a caller forgets to invoke
    # ``resolve_authorized_sources`` first, never select a connector that is
    # disabled, unassigned to this capability/agent, or not actually ready
    # (its persisted ``test_status`` is anything other than
    # ready/ready_with_key) -- this must match the checks in
    # ``resolve_authorized_sources`` exactly.
    #
    # Readiness-gated on purpose, not just ``enabled``: an enabled connector
    # whose stored ``test_status`` is ``configuration_required``/
    # ``unavailable``/anything outside the ready set must not be offered here
    # -- otherwise the UI's ready/ready_with_key vs. configuration_required/
    # unavailable vocabulary could be bypassed simply by an operator flipping
    # ``enabled`` back on without the connector actually having retested as
    # ready.
    enabled = {
        connector.id
        for connector in connectors
        if connector.enabled
        and logical_agent in connector.assigned_agents
        and connector.test_status in _READY_STATUSES
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
