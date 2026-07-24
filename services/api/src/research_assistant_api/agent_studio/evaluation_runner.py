"""Advisory evaluation execution port.

Running an ``EvaluationSuite``'s test cases against a draft/version requires
actually invoking that agent's runtime -- the harness-owned invocation path
(``services/api/foundry.py``, explicitly out of scope for this platform
session; see ``AGENTS.md``/session coordination notes). Rather than fabricate
scores or a fake "completed" run, this backend always advertises evaluation
*execution* as explicitly unavailable in production until a harness-owned
adapter is wired in above this port -- while still fully persisting suites
and honestly recording ``UNAVAILABLE`` run attempts through the same
create-run code path (see ``router.create_evaluation_run``).

This mirrors ``model_discovery.py``'s ``ModelDiscoveryError`` /
``UnavailableModelDiscovery`` pattern: a typed protocol, a typed error, and
an explicit unavailable default. In-memory runners exist only for tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from research_assistant_api.agent_studio.models import EvaluationSuite, EvaluationTestResult
from research_assistant_api.config import Settings

#: ``(case_input, instructions) -> (output, score, passed)`` -- test-only scoring hook.
ScoreFn = Callable[[str, str], tuple[str, float, bool]]


class EvaluationRunnerError(RuntimeError):
    pass


class EvaluationRunner(Protocol):
    def run_suite(self, suite: EvaluationSuite, *, instructions: str) -> tuple[EvaluationTestResult, ...]: ...


class UnavailableEvaluationRunner:
    """Explicit cloud-unavailable path: no evaluation execution adapter is wired."""

    def run_suite(self, suite: EvaluationSuite, *, instructions: str) -> tuple[EvaluationTestResult, ...]:
        raise EvaluationRunnerError(
            "No evaluation execution adapter is configured; advisory evaluation runs are unavailable."
        )


class InMemoryEvaluationRunner:
    """Explicit, test/offline-only runner backed by a caller-supplied scoring function.

    This must never be wired in a cloud/production path; it exists so unit
    tests can exercise the run-creation/persistence/history flow
    deterministically without a live runtime adapter.
    """

    def __init__(self, score_fn: ScoreFn | None = None) -> None:
        self._score_fn = score_fn

    def run_suite(self, suite: EvaluationSuite, *, instructions: str) -> tuple[EvaluationTestResult, ...]:
        results: list[EvaluationTestResult] = []
        for case in suite.test_cases:
            if self._score_fn is not None:
                output, score, passed = self._score_fn(case.input, instructions)
            else:
                output, score, passed = case.input, 1.0, True
            results.append(
                EvaluationTestResult(test_case_id=case.id, score=score, passed=passed, output=output)
            )
        return tuple(results)


def build_evaluation_runner(settings: Settings) -> EvaluationRunner:
    """Always returns the explicit unavailable path.

    There is no first-party evaluation execution adapter owned by this
    platform session; a harness-owned adapter is wired in above this port
    once available, never fabricated here.
    """
    del settings  # no configuration currently changes this outcome
    return UnavailableEvaluationRunner()
