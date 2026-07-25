from __future__ import annotations

import pytest
from pydantic import HttpUrl
from research_assistant_api.connector_gateway import (
    ConnectorGatewayError,
    DisabledConnectorGateway,
)
from research_assistant_api.public_research import (
    ConnectorAuthorizationError,
    resolve_authorized_sources,
    retrieve_public_metadata,
)
from research_assistant_api.workspace import WorkspaceStore
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability


@pytest.mark.asyncio
async def test_public_research_honors_enabled_agent_assignments() -> None:
    calls: list[tuple[Capability, str, str, int]] = []

    class FakeGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            calls.append((capability, source, query, limit))
            return ConnectorSearchResponse(
                source=PublicConnectorSource(source),
                query=query,
                records=[{"id": f"{source}-1", "title": "Public record"}],
                terms_url=HttpUrl(f"https://terms.example/{source}"),
                retrieved_from=HttpUrl(f"https://api.example/{source}"),
            )

        async def close(self) -> None:
            return None

    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        connector.enabled = connector.id == "pubmed"
        connector.assigned_agents = ["literature"]

    results = await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        connectors,
        gateway=FakeGateway(),
        requested_sources=["PubMed"],
    )

    assert calls == [
        (
            Capability.LITERATURE,
            "pubmed",
            "public reproducibility guidance",
            3,
        ),
    ]
    assert results[0]["source"] == "pubmed"
    assert results[0]["status"] == "ready"
    assert results[0]["records"][0]["id"] == "pubmed-1"


@pytest.mark.asyncio
async def test_public_research_honors_nondefault_requested_source() -> None:
    calls: list[str] = []

    class FakeGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            del capability, limit
            calls.append(source)
            return ConnectorSearchResponse(
                source=PublicConnectorSource(source),
                query=query,
                records=[],
                terms_url=HttpUrl("https://terms.example"),
                retrieved_from=HttpUrl("https://api.example"),
            )

        async def close(self) -> None:
            return None

    connector = next(
        item
        for item in WorkspaceStore().connectors()
        if item.id == "europe_pmc"
    )

    await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        [connector],
        gateway=FakeGateway(),
        requested_sources=["Europe PMC"],
    )

    assert calls == ["europe_pmc"]


@pytest.mark.asyncio
async def test_public_research_surfaces_connector_failure() -> None:
    class FailingGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            del capability, source, query, limit
            raise ConnectorGatewayError("Provider response was invalid")

        async def close(self) -> None:
            return None

    connector = next(item for item in WorkspaceStore().connectors() if item.id == "grants_gov")

    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        [connector],
        gateway=FailingGateway(),
    )

    assert results == [
        {
            "source": "grants_gov",
            "status": "unavailable",
            "error": "Provider response was invalid",
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_distinguishes_gateway_setup_from_provider_outage() -> None:
    connector = next(
        item for item in WorkspaceStore().connectors() if item.id == "pubmed"
    )

    results = await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        [connector],
        gateway=DisabledConnectorGateway(),
    )

    assert results == [
        {
            "source": "pubmed",
            "status": "configuration_required",
            "error": "The connector gateway is not configured.",
            "records": [],
        }
    ]


def test_resolve_authorized_sources_passes_through_none_unchanged() -> None:
    """No explicit client selection means the server-chosen defaults apply;
    there is nothing client-supplied to validate."""
    connectors = WorkspaceStore().connectors()

    assert resolve_authorized_sources(Capability.LITERATURE, None, connectors) is None


def test_resolve_authorized_sources_accepts_known_ready_connectors() -> None:
    connectors = WorkspaceStore().connectors()

    resolved = resolve_authorized_sources(
        Capability.LITERATURE,
        ["PubMed", "Europe PMC"],
        connectors,
    )

    assert resolved == ["pubmed", "europe_pmc"]


def test_resolve_authorized_sources_rejects_unknown_connector() -> None:
    connectors = WorkspaceStore().connectors()

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(Capability.LITERATURE, ["not-a-real-connector"], connectors)

    violation = excinfo.value.violations[0]
    assert violation.requested == "not-a-real-connector"
    assert violation.canonical_id is None
    assert violation.reason == "unknown"
    assert excinfo.value.is_authorization_failure is False


def test_resolve_authorized_sources_rejects_duplicate_after_canonicalization() -> None:
    connectors = WorkspaceStore().connectors()

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(Capability.LITERATURE, ["pubmed", "PubMed"], connectors)

    violation = excinfo.value.violations[0]
    assert violation.requested == "PubMed"
    assert violation.canonical_id == "pubmed"
    assert violation.reason == "duplicate"
    assert excinfo.value.is_authorization_failure is False


def test_resolve_authorized_sources_rejects_disabled_connector_as_authorization_failure() -> None:
    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.enabled = False

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(Capability.LITERATURE, ["pubmed"], connectors)

    violation = excinfo.value.violations[0]
    assert violation.canonical_id == "pubmed"
    assert violation.reason == "disabled"
    assert excinfo.value.is_authorization_failure is True


def test_resolve_authorized_sources_rejects_connector_not_assigned_to_capability() -> None:
    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.assigned_agents = ["dataset"]

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(Capability.LITERATURE, ["pubmed"], connectors)

    violation = excinfo.value.violations[0]
    assert violation.canonical_id == "pubmed"
    assert violation.reason == "not_assigned"
    assert excinfo.value.is_authorization_failure is True


def test_resolve_authorized_sources_honors_explicit_logical_agent_override() -> None:
    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.assigned_agents = ["literature_online"]

    resolved = resolve_authorized_sources(
        Capability.LITERATURE,
        ["pubmed"],
        connectors,
        logical_agent="literature_online",
    )

    assert resolved == ["pubmed"]


@pytest.mark.parametrize("status", ["configuration_required", "unavailable"])
def test_resolve_authorized_sources_rejects_connector_that_is_not_ready(status: str) -> None:
    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.test_status = status

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(Capability.LITERATURE, ["pubmed"], connectors)

    violation = excinfo.value.violations[0]
    assert violation.canonical_id == "pubmed"
    assert violation.reason == status
    # Not-ready is a readiness problem, not a permissions problem.
    assert excinfo.value.is_authorization_failure is False


def test_resolve_authorized_sources_collects_every_violation_not_just_the_first() -> None:
    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.enabled = False

    with pytest.raises(ConnectorAuthorizationError) as excinfo:
        resolve_authorized_sources(
            Capability.LITERATURE,
            ["pubmed", "not-a-real-connector", "pubmed"],
            connectors,
        )

    reasons = {violation.reason for violation in excinfo.value.violations}
    assert reasons == {"disabled", "unknown", "duplicate"}
    assert len(excinfo.value.violations) == 3
class _UnreachableGateway:
    """A gateway that fails the test if it is ever called -- used to prove
    that a non-ready connector is rejected without attempting a live call."""

    async def search(
        self,
        capability: Capability,
        source: str,
        query: str,
        *,
        limit: int,
    ) -> ConnectorSearchResponse:
        raise AssertionError(
            f"gateway.search() must not be called for a non-ready connector "
            f"(capability={capability}, source={source}, query={query}, limit={limit})"
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_public_research_excludes_enabled_but_not_ready_connector_from_default_sources() -> None:
    # An enabled connector whose last test came back configuration_required
    # must not be silently treated as ready just because `enabled` is true --
    # it must be excluded from the *default*, non-explicit source set with no
    # rejection entry (nothing was explicitly asked for, so there is nothing
    # to report back).
    connector = next(
        item for item in WorkspaceStore().connectors() if item.id == "grants_gov"
    )
    connector.test_status = "configuration_required"

    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        [connector],
        gateway=_UnreachableGateway(),
    )

    assert results == []


@pytest.mark.asyncio
async def test_public_research_rejects_explicitly_requested_not_ready_connector_with_real_status() -> None:
    # The server is the authoritative enforcement boundary even against a UI
    # bug or a client that bypasses the readiness gate: an explicitly
    # requested source whose connector is enabled and assigned but not ready
    # must be reported back with its own real stored test_status (not a
    # generic rejection code) and a matching, specific error -- and the
    # gateway must never be called for it.
    connector = next(
        item for item in WorkspaceStore().connectors() if item.id == "grants_gov"
    )
    connector.test_status = "configuration_required"

    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        [connector],
        gateway=_UnreachableGateway(),
        requested_sources=["Grants.gov"],
    )

    assert results == [
        {
            "source": "grants_gov",
            "status": "configuration_required",
            "error": (
                "The 'grants_gov' connector's last test status is "
                "'configuration_required', which is not ready."
            ),
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_rejects_explicitly_requested_disabled_connector() -> None:
    # A disabled connector must be rejected with its own reason distinct
    # from a not-ready test_status, even though its test_status itself may
    # still read "ready" from before it was disabled.
    connector = next(
        item for item in WorkspaceStore().connectors() if item.id == "pubmed"
    )
    connector.enabled = False

    results = await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        [connector],
        gateway=_UnreachableGateway(),
        requested_sources=["PubMed"],
    )

    assert results == [
        {
            "source": "pubmed",
            "status": "ready",
            "error": "The 'pubmed' connector is disabled for this workspace.",
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_rejects_explicitly_requested_unassigned_connector() -> None:
    # A connector that is enabled and genuinely ready, but simply not
    # assigned to this capability's logical agent, must be rejected with a
    # reason naming that specific cause.
    connector = next(
        item for item in WorkspaceStore().connectors() if item.id == "pubmed"
    )
    connector.assigned_agents = ["grant"]

    results = await retrieve_public_metadata(
        Capability.LITERATURE,
        "public reproducibility guidance",
        [connector],
        gateway=_UnreachableGateway(),
        requested_sources=["PubMed"],
    )

    assert results == [
        {
            "source": "pubmed",
            "status": "ready",
            "error": "The 'pubmed' connector is not assigned to this capability.",
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_rejects_explicitly_requested_source_with_no_connector_entry() -> None:
    # A source ID that *is* part of the capability's own vocabulary
    # (`_CAPABILITY_SOURCES`) but has no matching entry at all in the
    # workspace's own connector list -- e.g. a workspace whose connector
    # catalog was pared down, or a stale client referencing a source the
    # workspace never provisioned -- must be rejected with its own
    # "unavailable"/not-configured reason, not silently dropped or treated
    # as `enabled` by a `None` fallback.
    grants_gov = next(
        item for item in WorkspaceStore().connectors() if item.id == "grants_gov"
    )

    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        [grants_gov],
        gateway=_UnreachableGateway(),
        requested_sources=["NIH RePORTER"],
    )

    assert results == [
        {
            "source": "nih_reporter",
            "status": "unavailable",
            "error": "The 'nih_reporter' connector is not configured for this workspace.",
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_rejects_entirely_unknown_requested_source() -> None:
    # A requested source ID with no matching connector alias/definition
    # anywhere is not part of `_CAPABILITY_SOURCES` for any capability, so it
    # can never appear in the connector-status rejection loop (which only
    # walks that capability's own vocabulary). Previously this silently
    # vanished from the response entirely -- no rejection entry, no gateway
    # call, no signal to the caller that anything was wrong with the
    # request. It must instead be explicitly reported as unsupported.
    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        WorkspaceStore().connectors(),
        gateway=_UnreachableGateway(),
        requested_sources=["totally-unknown-source"],
    )

    assert results == [
        {
            "source": "totally-unknown-source",
            "status": "unsupported",
            "error": (
                "'totally-unknown-source' is not a supported source for "
                "the 'grant' capability."
            ),
            "records": [],
        }
    ]


@pytest.mark.asyncio
async def test_public_research_deselecting_all_sources_makes_zero_gateway_calls() -> None:
    # An explicit empty list (as opposed to `None`, which falls back to the
    # capability's own default source set) represents a caller who has
    # deselected every connector in the UI. This must resolve to an
    # entirely empty candidate set -- no default sources reinstated, no
    # "unsupported" rejection entries (nothing was actually named), and,
    # critically, no live call to the gateway for any connector, even ones
    # that are otherwise enabled/ready/assigned.
    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        WorkspaceStore().connectors(),
        gateway=_UnreachableGateway(),
        requested_sources=[],
    )

    assert results == []


@pytest.mark.asyncio
async def test_public_research_rejects_source_valid_for_a_different_capability() -> None:
    # "pubmed" is a real, known connector -- just not part of the GRANT
    # capability's vocabulary (`_CAPABILITY_SOURCES[Capability.GRANT]`).
    # Requesting it under Capability.GRANT must not silently resolve to an
    # empty result merely because the ID happens to be recognized elsewhere;
    # it is unsupported for *this* capability and must say so explicitly.
    results = await retrieve_public_metadata(
        Capability.GRANT,
        "public opportunity",
        WorkspaceStore().connectors(),
        gateway=_UnreachableGateway(),
        requested_sources=["PubMed", "totally-unknown"],
    )

    assert results == [
        {
            "source": "pubmed",
            "status": "unsupported",
            "error": "'pubmed' is not a supported source for the 'grant' capability.",
            "records": [],
        },
        {
            "source": "totally-unknown",
            "status": "unsupported",
            "error": "'totally-unknown' is not a supported source for the 'grant' capability.",
            "records": [],
        },
    ]
