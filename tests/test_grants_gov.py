from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import httpx
from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    FunctionInvocationContext,
    Message,
    ResponseStream,
)
from grant.main import (
    _GRANTS_GOV_LOOKUPS,
    _OUTSTANDING,
    _REQUEST,
    _REQUEST_MODE,
    EnvelopeMiddleware,
    EvidenceRef,
    GrantClaim,
    GrantOpportunity,
    GrantReport,
    GrantRequest,
    GrantsGovReceipt,
    GrantsGovRecord,
    GrantToolBoundary,
    OpportunityRelevance,
    OpportunitySelection,
    RequestMode,
    SupportStatus,
    _record_grants_gov_lookup,
    _safe_model_claims,
    _verified_opportunities,
    _verified_opportunity_claims,
    coverage_gate,
    outstanding_work,
)
from research_assistant_connectors import ResearchConnectorRegistry
from research_assistant_core.connector_catalog import connector_definition
from shared.source_tools import SourceToolBoundary, bind_source_tools

from scripts.build_connector_apim_spec import (
    connector_apim_openapi,
    connector_operation_policies,
)


def _lookup_payload() -> dict[str, object]:
    return {
        "errorcode": 0,
        "msg": "Webservice Succeeds",
        "data": {
            "id": 357744,
            "opportunityNumber": "RFA-HG-25-009",
            "opportunityTitle": (
                "Supporting Talented Early Career Researchers in Genomics "
                "(R01 Clinical Trial Optional)"
            ),
            "ost": "POSTED",
            "agencyDetails": {
                "agencyName": "National Institutes of Health",
            },
            "synopsis": {
                "postingDateStr": "2024-12-16-00-00-00",
                "responseDateStr": "2027-02-26-00-00-00",
                "archiveDateStr": "2027-04-03-00-00-00",
            },
            "errorMessages": [],
        },
    }


def test_grants_gov_receipt_is_recorded_from_the_verified_lookup_result() -> None:
    token = _GRANTS_GOV_LOOKUPS.set({})
    try:
        _record_grants_gov_lookup(
            json.dumps(
                {
                    "source": "grants_gov",
                    "query": 357744,
                    "records": [
                        {
                            "grants_gov_id": "357744",
                            "opportunity_number": "RFA-HG-25-009",
                            "title": "Supporting Talented Early Career Researchers in Genomics",
                            "agency": "National Institutes of Health",
                            "status": "posted",
                            "canonical_url": (
                                "https://www.grants.gov/search-results-detail/357744"
                            ),
                            "evidence_id": "connector:grants_gov:lookup-receipt",
                        }
                    ],
                    "warnings": [],
                }
            )
        )
        receipts = _GRANTS_GOV_LOOKUPS.get()
    finally:
        _GRANTS_GOV_LOOKUPS.reset(token)

    assert receipts is not None
    assert receipts["357744"].record.opportunity_number == "RFA-HG-25-009"


def test_streamed_grant_final_response_is_reconciled_from_lookup_receipts() -> None:
    request = GrantRequest(
        query="Look up Grants.gov opportunity 357744.",
        tenant_id="tenant-1",
        project_id="project-1",
        principal_id="user-1",
        session_id="session-1",
        sensitivity="internal",
        authorized_connector_ids=("grants_gov",),
        opportunity_id="357744",
    )
    context = cast(
        Any,
        SimpleNamespace(
            messages=[Message(role="user", contents=[request.model_dump_json()])],
            stream=True,
            result=None,
        ),
    )

    async def run() -> AgentResponse[GrantReport]:
        async def updates() -> AsyncIterator[AgentResponseUpdate]:
            if False:
                yield AgentResponseUpdate()

        model_report = GrantReport(
            summary="One opportunity was selected.",
            selected_opportunities=(
                OpportunitySelection(
                    grants_gov_id="357744",
                    relevance=OpportunityRelevance.DIRECT,
                    relevance_rationale="The exact requested opportunity.",
                ),
            ),
            opportunities=(
                GrantOpportunity(
                    grants_gov_id="357744",
                    opportunity_number="MODEL-AUTHORED",
                    title="Model-authored title",
                    agency="Model-authored agency",
                    status="unknown",
                    canonical_url="https://www.grants.gov/search-results-detail/357744",
                    relevance=OpportunityRelevance.DIRECT,
                    relevance_rationale="The exact requested opportunity.",
                ),
            ),
        )
        response = AgentResponse[GrantReport](
            messages=[Message(role="assistant", contents=[model_report.model_dump_json()])],
            value=model_report,
            response_format=GrantReport,
        )

        def finalize(_updates: object) -> AgentResponse[GrantReport]:
            _REQUEST.set(None)
            _GRANTS_GOV_LOOKUPS.set(None)
            return response

        async def call_next() -> None:
            _record_grants_gov_lookup(
                json.dumps(
                    {
                        "source": "grants_gov",
                        "query": "357744",
                        "records": [
                            {
                                "grants_gov_id": "357744",
                                "opportunity_number": "RFA-HG-25-009",
                                "title": "Supporting Talented Early Career Researchers in Genomics",
                                "agency": "National Institutes of Health",
                                "status": "posted",
                                "canonical_url": "https://www.grants.gov/search-results-detail/357744",
                            }
                        ],
                        "warnings": [],
                    }
                )
            )
            context.result = ResponseStream[
                AgentResponseUpdate,
                AgentResponse[GrantReport],
            ](updates(), finalizer=finalize)

        await EnvelopeMiddleware().process(context, call_next)
        assert isinstance(context.result, ResponseStream)
        async for _update in context.result:
            pass
        return cast(AgentResponse[GrantReport], await context.result.get_final_response())

    final = asyncio.run(run())

    assert isinstance(final.value, GrantReport)
    assert len(final.value.opportunities) == 1
    opportunity = final.value.opportunities[0]
    assert opportunity.opportunity_number == "RFA-HG-25-009"
    assert opportunity.title == "Supporting Talented Early Career Researchers in Genomics"
    assert opportunity.agency == "National Institutes of Health"
    assert opportunity.status == "posted"


def test_grants_gov_lookup_returns_one_normalized_verified_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.grants.gov/v1/api/fetchOpportunity"
        assert json.loads(request.content) == {"opportunityId": 357744}
        return httpx.Response(200, json=_lookup_payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    registry = ResearchConnectorRegistry(client=client)

    result = registry.lookup("grants_gov", "357744")

    assert result.records == [
        {
            "grants_gov_id": "357744",
            "opportunity_number": "RFA-HG-25-009",
            "title": (
                "Supporting Talented Early Career Researchers in Genomics "
                "(R01 Clinical Trial Optional)"
            ),
            "agency": "National Institutes of Health",
            "status": "posted",
            "posted_date": "2024-12-16",
            "close_date": "2027-02-26",
            "archive_date": "2027-04-03",
            "canonical_url": "https://www.grants.gov/search-results-detail/357744",
        }
    ]


def test_generated_grants_gov_contract_exposes_fetch_lookup() -> None:
    connector = connector_definition("grants_gov")
    assert [operation.mcp_tool_name for operation in connector.operations] == [
        "search",
        "lookup",
    ]

    specification = connector_apim_openapi()
    search = specification["paths"]["/v1/connectors/grants_gov/search"]["get"]
    assert [parameter["name"] for parameter in search["parameters"]] == [
        "query",
        "limit",
    ]
    assert search["parameters"][1]["required"] is True
    assert search["parameters"][1]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 25,
        "default": 5,
    }
    operation = specification["paths"]["/v1/connectors/grants_gov/lookup"]["get"]
    assert operation["operationId"] == "grantsGovLookup"
    assert operation["parameters"][0]["name"] == "identifier"

    policies = {
        item["operationId"]: item["value"]
        for item in connector_operation_policies()
    }
    lookup_policy = policies["grantsGovLookup"]
    assert "/v1/api/fetchOpportunity" in lookup_policy
    assert "opportunityId" in lookup_policy
    assert "search-results-detail" in lookup_policy


def test_grant_report_builds_provider_facts_only_from_lookup_receipts() -> None:
    record = GrantsGovRecord(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title=(
            "Supporting Talented Early Career Researchers in Genomics "
            "(R01 Clinical Trial Optional)"
        ),
        agency="National Institutes of Health",
        status="posted",
        posted_date="2024-12-16",
        close_date="2027-02-26",
        archive_date="2027-04-03",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
    )
    token = _GRANTS_GOV_LOOKUPS.set(
        {
            "357744": GrantsGovReceipt(
                record=record,
                verified_at="2026-08-27T12:00:00+00:00",
            )
        }
    )
    try:
        report = GrantReport(
            summary="One opportunity directly matches the request.",
            selected_opportunities=(
                OpportunitySelection(
                    grants_gov_id="357744",
                    relevance=OpportunityRelevance.DIRECT,
                    relevance_rationale="The notice explicitly targets genomics research.",
                ),
            ),
            opportunities=(
                GrantOpportunity(
                    grants_gov_id="357744",
                    opportunity_number="MODEL-AUTHORED",
                    title="Unverified model-authored title",
                    agency="Unverified model-authored agency",
                    status="unverified",
                    canonical_url=(
                        "https://www.grants.gov/search-results-detail/357744"
                    ),
                    relevance=OpportunityRelevance.ADJACENT,
                    relevance_rationale="Unverified model-authored rationale.",
                    verified_at="2000-01-01T00:00:00+00:00",
                ),
            ),
        )

        opportunities = _verified_opportunities(report)
    finally:
        _GRANTS_GOV_LOOKUPS.reset(token)

    assert [item.model_dump(mode="json") for item in opportunities] == [
        {
            **record.model_dump(mode="json"),
            "relevance": "direct",
            "relevance_rationale": "The notice explicitly targets genomics research.",
            "verified_at": "2026-08-27T12:00:00+00:00",
        }
    ]


def test_selected_opportunity_does_not_append_unselected_lookup_receipts() -> None:
    selected = GrantsGovRecord(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title="Supporting Talented Early Career Researchers in Genomics",
        agency="National Institutes of Health",
        status="posted",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
    )
    exploratory = GrantsGovRecord(
        grants_gov_id="358176",
        opportunity_number="EXPLORATORY",
        title="Exploratory lookup that was not selected",
        agency="Other agency",
        status="posted",
        canonical_url="https://www.grants.gov/search-results-detail/358176",
    )
    token = _GRANTS_GOV_LOOKUPS.set(
        {
            "357744": GrantsGovReceipt(record=selected, verified_at="2026-08-28T12:00:00Z"),
            "358176": GrantsGovReceipt(record=exploratory, verified_at="2026-08-28T12:00:00Z"),
        }
    )
    try:
        opportunities = _verified_opportunities(
            GrantReport(
                summary="The requested opportunity was verified.",
                selected_opportunities=(
                    OpportunitySelection(
                        grants_gov_id="357744",
                        relevance=OpportunityRelevance.DIRECT,
                        relevance_rationale="The user requested this exact record.",
                    ),
                ),
            )
        )
    finally:
        _GRANTS_GOV_LOOKUPS.reset(token)

    assert [item.grants_gov_id for item in opportunities] == ["357744"]


def test_coverage_gate_requires_exact_lookup_until_receipt_is_recorded() -> None:
    report = GrantReport(
        summary="One search result needs exact verification.",
        selected_opportunities=(
            OpportunitySelection(
                grants_gov_id="357744",
                relevance=OpportunityRelevance.DIRECT,
                relevance_rationale="The search result targets genomics research.",
            ),
        ),
    )
    result = SimpleNamespace(
        messages=[
            SimpleNamespace(role="assistant", text=report.model_dump_json())
        ]
    )
    lookup_token = _GRANTS_GOV_LOOKUPS.set({})
    mode_token = _REQUEST_MODE.set(RequestMode.WORK)
    outstanding_token = _OUTSTANDING.set(None)
    try:
        malformed_result = SimpleNamespace(
            messages=[SimpleNamespace(role="assistant", text="not-json")]
        )
        assert coverage_gate(last_result=malformed_result) == (
            True,
            "Your reply did not match the grant report contract. Re-emit it.",
        )
        assert outstanding_work(result, {}) == frozenset({"lookup:357744"})
        assert coverage_gate(last_result=result) == (
            True,
            "Selected Grants.gov opportunities still require lookup: 357744. "
            "Call `grants_gov___lookup` for each ID, then re-emit the report.",
        )

        _record_grants_gov_lookup(
            json.dumps(
                {
                    "source": "grants_gov",
                    "query": "357744",
                    "records": [
                        {
                            "grants_gov_id": "357744",
                            "opportunity_number": "RFA-HG-25-009",
                            "title": "Supporting Talented Early Career Researchers in Genomics",
                            "agency": "National Institutes of Health",
                            "status": "posted",
                            "canonical_url": (
                                "https://www.grants.gov/search-results-detail/357744"
                            ),
                        }
                    ],
                    "warnings": [],
                }
            )
        )

        assert outstanding_work(result, {}) == frozenset()
        assert coverage_gate(last_result=result) == (False, None)
    finally:
        _OUTSTANDING.reset(outstanding_token)
        _REQUEST_MODE.reset(mode_token)
        _GRANTS_GOV_LOOKUPS.reset(lookup_token)


def test_lookup_receipts_require_an_explicit_model_selection() -> None:
    record = GrantsGovRecord(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title="Supporting Talented Early Career Researchers in Genomics",
        agency="National Institutes of Health",
        status="posted",
        close_date="2027-02-26",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
    )
    token = _GRANTS_GOV_LOOKUPS.set(
        {
            "357744": GrantsGovReceipt(
                record=record,
                verified_at="2026-08-27T12:00:00+00:00",
            )
        }
    )
    try:
        opportunities = _verified_opportunities(
            GrantReport(summary="One verified opportunity was retrieved.")
        )
    finally:
        _GRANTS_GOV_LOOKUPS.reset(token)

    assert opportunities == ()


def test_exact_request_materializes_its_receipt_without_model_selection() -> None:
    record = GrantsGovRecord(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title="Supporting Talented Early Career Researchers in Genomics",
        agency="National Institutes of Health",
        status="posted",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
    )
    lookup_token = _GRANTS_GOV_LOOKUPS.set(
        {
            "357744": GrantsGovReceipt(
                record=record,
                verified_at="2026-08-28T12:00:00+00:00",
            )
        }
    )
    try:
        opportunities = _verified_opportunities(
            GrantReport(summary="The exact opportunity was looked up."),
            exact_id="357744",
        )
    finally:
        _GRANTS_GOV_LOOKUPS.reset(lookup_token)

    assert [item.grants_gov_id for item in opportunities] == ["357744"]
    assert opportunities[0].opportunity_number == "RFA-HG-25-009"


def test_coverage_gate_repairs_an_omitted_selection_after_lookup() -> None:
    report = GrantReport(summary="One opportunity was looked up.")
    result = SimpleNamespace(
        messages=[SimpleNamespace(role="assistant", text=report.model_dump_json())]
    )
    lookup_token = _GRANTS_GOV_LOOKUPS.set(
        {
            "357744": GrantsGovReceipt(
                record=GrantsGovRecord(
                    grants_gov_id="357744",
                    opportunity_number="RFA-HG-25-009",
                    title="Supporting Talented Early Career Researchers in Genomics",
                    agency="National Institutes of Health",
                    status="posted",
                    canonical_url="https://www.grants.gov/search-results-detail/357744",
                ),
                verified_at="2026-08-27T12:00:00+00:00",
            )
        }
    )
    mode_token = _REQUEST_MODE.set(RequestMode.WORK)
    outstanding_token = _OUTSTANDING.set(frozenset({"lookup:357744"}))
    try:
        assert coverage_gate(last_result=result) == (
            True,
            "Verified Grants.gov lookup receipts still require an explicit selection: "
            "357744. Add each recommended ID to `selected_opportunities` with a "
            "relevance assessment, then re-emit the report.",
        )
    finally:
        _OUTSTANDING.reset(outstanding_token)
        _REQUEST_MODE.reset(mode_token)
        _GRANTS_GOV_LOOKUPS.reset(lookup_token)


def test_shared_lookup_ledger_builds_verified_opportunities() -> None:
    bind_source_tools(("grants_gov",), ())
    request_token = _REQUEST.set(
        GrantRequest(
            query="Look up 357744",
            tenant_id="tenant-1",
            project_id="project-1",
            principal_id="user-1",
            session_id="session-1",
            sensitivity="internal",
            authorized_connector_ids=("grants_gov",),
        )
    )
    lookup_token = _GRANTS_GOV_LOOKUPS.set({})
    context = cast(
        FunctionInvocationContext,
        SimpleNamespace(
            function=SimpleNamespace(name="grants_gov___grants_gov_lookup"),
            arguments={"identifier": 357744},
            result=None,
        ),
    )

    async def call_next() -> None:
        context.result = json.dumps(
            {
                "source": "grants_gov",
                "query": "357744",
                "retrieved_from": "https://api.grants.gov/v1/api/fetchOpportunity",
                "records": [
                    {
                        "grants_gov_id": "357744",
                        "opportunity_number": "RFA-HG-25-009",
                        "title": "Supporting Talented Early Career Researchers in Genomics",
                        "agency": "National Institutes of Health",
                        "status": "posted",
                        "canonical_url": (
                            "https://www.grants.gov/search-results-detail/357744"
                        ),
                    }
                ],
                "warnings": [],
            }
        )

    async def invoke_grant_boundary() -> None:
        await GrantToolBoundary().process(context, call_next)

    try:
        asyncio.run(SourceToolBoundary().process(context, invoke_grant_boundary))
        opportunities = _verified_opportunities(
                GrantReport(
                    summary="One verified opportunity was retrieved.",
                    selected_opportunities=(
                        OpportunitySelection(
                            grants_gov_id="357744",
                            relevance=OpportunityRelevance.DIRECT,
                            relevance_rationale="The user requested this exact record.",
                        ),
                    ),
                )
        )
    finally:
        _GRANTS_GOV_LOOKUPS.reset(lookup_token)
        _REQUEST.reset(request_token)

    assert len(opportunities) == 1
    assert opportunities[0].opportunity_number == "RFA-HG-25-009"


def test_search_ledger_records_do_not_become_verified_opportunities() -> None:
    bind_source_tools(("grants_gov",), ())
    context = cast(
        FunctionInvocationContext,
        SimpleNamespace(
            function=SimpleNamespace(name="grants_gov___grants_gov_search"),
            arguments={"query": "genomics", "limit": 5},
            result=None,
        ),
    )

    async def call_next() -> None:
        context.result = json.dumps(
            {
                "source": "grants_gov",
                "query": "genomics",
                "retrieved_from": "https://api.grants.gov/v1/api/search2",
                "records": [
                    {
                        "grants_gov_id": "357744",
                        "opportunity_number": "RFA-HG-25-009",
                        "title": "Supporting Talented Early Career Researchers in Genomics",
                        "agency": "National Institutes of Health",
                        "status": "posted",
                        "canonical_url": (
                            "https://www.grants.gov/search-results-detail/357744"
                        ),
                    }
                ],
                "warnings": [],
            }
        )

    asyncio.run(SourceToolBoundary().process(context, call_next))

    assert _verified_opportunities(
        GrantReport(summary="One unverified search result was retrieved.")
    ) == ()


def test_warned_shared_lookup_record_does_not_become_a_verified_opportunity() -> None:
    bind_source_tools(("grants_gov",), ())
    context = cast(
        FunctionInvocationContext,
        SimpleNamespace(
            function=SimpleNamespace(name="grants_gov___grants_gov_lookup"),
            arguments={"identifier": 999999},
            result=None,
        ),
    )

    async def call_next() -> None:
        context.result = json.dumps(
            {
                "source": "grants_gov",
                "query": "357744",
                "retrieved_from": "https://api.grants.gov/v1/api/fetchOpportunity",
                "records": [
                    {
                        "grants_gov_id": "999999",
                        "opportunity_number": "WRONG-ID",
                        "title": "Warned mismatched record",
                        "agency": "Unknown agency",
                        "status": "posted",
                        "canonical_url": (
                            "https://www.grants.gov/search-results-detail/999999"
                        ),
                    }
                ],
                "warnings": ["Lookup returned a mismatched identifier."],
            }
        )

    asyncio.run(SourceToolBoundary().process(context, call_next))

    assert _verified_opportunities(
        GrantReport(summary="A warned lookup record was retrieved.")
    ) == ()


def test_verified_provider_facts_own_supported_claims_and_evidence() -> None:
    evidence_id = "connector:grants_gov:lookup-receipt"
    opportunity = GrantOpportunity(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title=(
            "Supporting Talented Early Career Researchers in Genomics "
            "(R01 Clinical Trial Optional)"
        ),
        agency="National Institutes of Health",
        status="posted",
        posted_date="2024-12-16",
        close_date="2027-02-26",
        archive_date="2027-04-03",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
        relevance=OpportunityRelevance.DIRECT,
        relevance_rationale="The opportunity explicitly targets genomics research.",
        verified_at="2026-08-27T12:00:00+00:00",
    )
    evidence = {
        evidence_id: EvidenceRef(
            evidence_id=evidence_id,
            source_uri=opportunity.canonical_url,
            title=opportunity.title,
        )
    }

    claims = _verified_opportunity_claims((opportunity,), evidence)

    assert len(claims) == 1
    assert claims[0].support == SupportStatus.SUPPORTED
    assert claims[0].evidence_ids == (evidence_id,)
    assert "RFA-HG-25-009" in claims[0].text
    assert "closes 2027-02-26" in claims[0].text
    assert _safe_model_claims(
        (
            GrantClaim(
                text="The sponsoring agency is the National Institutes of Health.",
                support=SupportStatus.UNSUPPORTED,
            ),
            GrantClaim(
                text=(
                    "The canonical Grants.gov link is "
                    "https://www.grants.gov/search-results-detail/357744."
                ),
                support=SupportStatus.UNSUPPORTED,
            ),
        ),
        {evidence_id},
        opportunities=(opportunity,),
    ) == ()