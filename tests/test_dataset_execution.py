from __future__ import annotations

import pytest
from research_assistant_api.dataset_execution import (
    ALLOWED_DATA_CLASSIFICATION,
    MAX_INLINE_DATASET_CHARS,
    build_dataset_agent_message,
    validate_dataset_execution,
)
from research_assistant_core.models import Capability, ResearchRequest, ResearchResult
from research_assistant_core.service import ResearchService
from research_assistant_core.studio_models import StudioRunRequest


def dataset_request(**inputs: object) -> StudioRunRequest:
    return StudioRunRequest(
        objective="Compute the exact group means.",
        inputs={
            "filename": "approved.csv",
            "analysis_approved": True,
            "data_classification": ALLOWED_DATA_CLASSIFICATION,
            **inputs,
        },
    )


def dataset_result(request: StudioRunRequest) -> ResearchResult:
    return ResearchService().run(
        Capability.DATASET,
        ResearchRequest(
            query=request.objective,
            context=request.inputs,
        ),
    )


def test_approved_csv_builds_an_untruncated_code_interpreter_message() -> None:
    request = dataset_request(csv_text="group,value\ncontrol,10\nintervention,14\n")

    message = build_dataset_agent_message(
        request,
        dataset_result(request),
        workflow_title="Dataset output analysis",
        stage_labels=["Validate", "Compute"],
    )

    assert "Material format: Approved CSV" in message
    assert "group,value\ncontrol,10\nintervention,14" in message
    assert "public or synthetic" in message
    assert "Stages: Validate, Compute" in message


def test_profile_only_sample_is_labeled_as_json_not_csv() -> None:
    request = dataset_request(filename="pilot-outcomes.csv")

    message = build_dataset_agent_message(
        request,
        dataset_result(request),
        workflow_title="Dataset output analysis",
        stage_labels=["Profile"],
    )

    assert "Material format: Deterministic profile JSON" in message
    assert '"rows":' in message


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        (
            {
                "analysis_approved": False,
                "data_classification": ALLOWED_DATA_CLASSIFICATION,
            },
            "approval",
        ),
        (
            {"analysis_approved": True},
            "public or synthetic",
        ),
        (
            {
                "analysis_approved": True,
                "data_classification": ALLOWED_DATA_CLASSIFICATION,
                "csv_text": "",
            },
            "contain data",
        ),
        (
            {
                "analysis_approved": True,
                "data_classification": ALLOWED_DATA_CLASSIFICATION,
                "csv_text": "x" * (MAX_INLINE_DATASET_CHARS + 1),
            },
            "100,000 characters",
        ),
        (
            {
                "analysis_approved": True,
                "data_classification": ALLOWED_DATA_CLASSIFICATION,
                "filename": "approved.json",
                "csv_text": '{"value": 1}',
            },
            "CSV input only",
        ),
    ],
)
def test_dataset_execution_fails_closed(
    inputs: dict[str, object],
    message: str,
) -> None:
    request = StudioRunRequest(
        objective="Analyze data.",
        inputs=inputs,
    )

    with pytest.raises(ValueError, match=message):
        validate_dataset_execution(request)


def test_non_string_csv_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="contain data"):
        validate_dataset_execution(dataset_request(csv_text=["not", "text"]))
