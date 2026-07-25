from __future__ import annotations

import pytest
from research_assistant_core.models import ArtifactSection, Capability, ResearchRequest
from research_assistant_core.service import CitationValidationError, ResearchService


def test_grant_with_supplied_facts_is_not_marked_as_missing_context() -> None:
    result = ResearchService().run(
        Capability.GRANT,
        ResearchRequest(
            query="open research infrastructure",
            context={"project_facts": ["The institution operates a governed research workspace."]},
        ),
    )

    assert result.warnings == []
    assert result.metadata["supplied_facts"]


def test_result_validation_rejects_unknown_and_missing_citations() -> None:
    service = ResearchService()
    result = service.run(
        Capability.GRANT,
        ResearchRequest(query="open research infrastructure"),
    )
    result.sections = [
        ArtifactSection(
            heading="Unknown citation",
            body="This claim points at evidence that is not in the result.",
            citation_ids=["cite-missing"],
        )
    ]
    with pytest.raises(CitationValidationError, match="unknown citations"):
        service._validate_result(result)

    result.sections = [
        ArtifactSection(
            heading="Uncited claim",
            body="This claim has no evidence reference.",
        )
    ]
    with pytest.raises(CitationValidationError, match="uncited claim"):
        service._validate_result(result)
