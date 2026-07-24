from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.evaluation_runner import (
    EvaluationRunnerError,
    InMemoryEvaluationRunner,
    UnavailableEvaluationRunner,
    build_evaluation_runner,
)
from research_assistant_api.agent_studio.models import EvaluationSuite, EvaluationTestCase
from research_assistant_api.config import Settings


def _suite() -> EvaluationSuite:
    return EvaluationSuite(
        id="suite-1",
        logical_agent_id="agent-1",
        tenant_id="demo",
        project_id="project-1",
        name="Suite",
        created_by="user-1",
        test_cases=(
            EvaluationTestCase(id="case-1", name="Case 1", input="hello"),
            EvaluationTestCase(id="case-2", name="Case 2", input="world"),
        ),
    )


def test_unavailable_evaluation_runner_raises() -> None:
    with pytest.raises(EvaluationRunnerError, match="unavailable"):
        UnavailableEvaluationRunner().run_suite(_suite(), instructions="be helpful")


def test_in_memory_evaluation_runner_uses_supplied_score_fn() -> None:
    runner = InMemoryEvaluationRunner(
        lambda case_input, instructions: (f"echo: {case_input}/{instructions}", 0.75, False)
    )
    results = runner.run_suite(_suite(), instructions="be helpful")
    assert [result.test_case_id for result in results] == ["case-1", "case-2"]
    assert results[0].output == "echo: hello/be helpful"
    assert results[0].score == 0.75
    assert results[0].passed is False


def test_in_memory_evaluation_runner_defaults_to_echoing_input_as_a_pass() -> None:
    runner = InMemoryEvaluationRunner()
    results = runner.run_suite(_suite(), instructions="be helpful")
    assert [result.output for result in results] == ["hello", "world"]
    assert all(result.score == 1.0 and result.passed for result in results)


def test_build_evaluation_runner_always_returns_unavailable() -> None:
    """No first-party evaluation execution adapter is owned by this
    platform session; the factory has no configuration-driven real branch,
    unlike ``build_model_discovery``."""
    assert isinstance(build_evaluation_runner(Settings()), UnavailableEvaluationRunner)
    assert isinstance(
        build_evaluation_runner(Settings(foundry_project_endpoint="https://project.example.test")),
        UnavailableEvaluationRunner,
    )
