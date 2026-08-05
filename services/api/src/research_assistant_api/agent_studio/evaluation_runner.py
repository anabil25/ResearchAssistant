"""Advisory evaluation execution port.

Running an ``EvaluationSuite``'s test cases against a draft/version requires
actually invoking that agent's runtime -- the harness-owned invocation path
(``services/api/foundry.py``, explicitly out of scope for this platform
session; see ``AGENTS.md``/session coordination notes). This backend advertises
evaluation execution as explicitly unavailable until a harness-owned adapter
is wired above this port, while persisting suites and recording ``UNAVAILABLE``
run attempts through the same create-run code path.

This mirrors ``model_discovery.py``'s ``ModelDiscoveryError`` /
``UnavailableModelDiscovery`` pattern: a typed protocol, a typed error, and
an explicit unavailable default.
"""

from __future__ import annotations

from typing import Protocol

from research_assistant_api.agent_studio.models import EvaluationSuite, EvaluationTestResult
from research_assistant_api.config import Settings


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


def build_evaluation_runner(settings: Settings) -> EvaluationRunner:
    """Always returns the explicit unavailable path.

    There is no first-party evaluation execution adapter owned by this
    platform session; a harness-owned adapter is wired in above this port
    once available, never fabricated here.
    """
    del settings  # no configuration currently changes this outcome
    return UnavailableEvaluationRunner()
