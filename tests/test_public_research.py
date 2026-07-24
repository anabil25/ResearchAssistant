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
