from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_core import Capability, HostedPublicAgentRequest, ResearchRequest, ResearchService


@pytest.mark.parametrize(
    ("capability", "query"),
    [
        (Capability.LITERATURE, "Compare auditable research synthesis"),
        (Capability.GRANT, "Draft aims for open research infrastructure"),
        (Capability.MATCHING, "Find genomics experts and equipment"),
        (Capability.DATASET, "Summarize the sample outcomes"),
        (Capability.INSTITUTIONAL_QA, "When must AI be disclosed to the IRB?"),
        (Capability.ORCHESTRATION, "Plan ingest compare review and export"),
    ],
)
def test_every_capability_returns_a_verified_result(
    capability: Capability,
    query: str,
) -> None:
    service = ResearchService()

    result = service.run(capability, ResearchRequest(query=query))

    assert result.run.capability is capability
    assert result.title
    assert result.provenance.run_id == result.run.id
    assert result.provenance.verification == "citation references resolved"
    known = {citation.id for citation in result.citations}
    assert all(citation_id in known for section in result.sections for citation_id in section.citation_ids)


def test_institutional_qa_abstains_when_evidence_is_missing() -> None:
    result = ResearchService().run(
        Capability.INSTITUTIONAL_QA,
        ResearchRequest(query="What is the campus parking permit fee?"),
    )

    assert result.citations == []
    assert "does not support an answer" in result.title
    assert result.provenance.caveats


def test_restricted_tenant_content_is_not_returned() -> None:
    result = ResearchService().run(
        Capability.INSTITUTIONAL_QA,
        ResearchRequest(
            query="What does the restricted tenant policy say?",
            tenant_id="demo",
        ),
    )

    assert all(citation.source_id != "policy-private" for citation in result.citations)


def test_grant_draft_does_not_invent_project_facts() -> None:
    result = ResearchService().run(
        Capability.GRANT,
        ResearchRequest(query="Draft specific aims for the sample opportunity"),
    )

    assert result.metadata["review_status"] == "needs_project_facts"
    assert any("No project-specific facts" in warning for warning in result.warnings)
    assert any(metric.label == "Unsupported facts added" and metric.value == "0" for metric in result.metrics)


def test_hosted_public_agent_request_binds_a_unique_public_connector_set() -> None:
    request = HostedPublicAgentRequest(
        query="Compare current public guidance",
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="user-a",
        session_id="run-a",
        authorized_connector_ids=("pubmed", "crossref"),
    )

    assert request.sensitivity == "public"
    assert request.authorized_connector_ids == ("pubmed", "crossref")
    with pytest.raises(ValidationError, match="unique"):
        HostedPublicAgentRequest(
            query="Compare current public guidance",
            tenant_id="tenant-a",
            project_id="project-a",
            principal_id="user-a",
            session_id="run-a",
            authorized_connector_ids=("pubmed", "pubmed"),
        )
