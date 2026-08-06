"""Advisory-free playground/test-run execution port.

Actually invoking a draft/version with a single typed input requires
running that agent's runtime -- the harness-owned invocation path
(``services/api/foundry.py``, explicitly out of scope for this platform
session; see ``AGENTS.md``/session coordination notes). Rather than
fabricate a response or trace, this backend always advertises playground
*execution* as explicitly unavailable in production until a harness-owned
adapter is wired in above this port -- the create-run endpoint fails
honestly with 503 and never persists a fabricated run record in that case
(see ``router.create_test_run``).

The boundary uses a typed protocol, a typed error, and an explicit
unavailable default. In-memory invokers exist only for tests.
"""

from __future__ import annotations

from typing import NamedTuple, Protocol

from research_assistant_api.agent_studio.models import PlaygroundToolCall, PlaygroundTraceEvent
from research_assistant_api.config import Settings


class PlaygroundInvocationError(RuntimeError):
    pass


class PlaygroundInvocationResult(NamedTuple):
    output: str
    trace: tuple[PlaygroundTraceEvent, ...]
    tool_calls: tuple[PlaygroundToolCall, ...]


class PlaygroundInvoker(Protocol):
    def invoke(self, *, instructions: str, input_text: str) -> PlaygroundInvocationResult: ...


class UnavailablePlaygroundInvoker:
    """Explicit cloud-unavailable path: no playground execution adapter is wired."""

    def invoke(self, *, instructions: str, input_text: str) -> PlaygroundInvocationResult:
        raise PlaygroundInvocationError(
            "No playground execution adapter is configured; test/playground runs are unavailable."
        )


def build_playground_invoker(settings: Settings) -> PlaygroundInvoker:
    """Always returns the explicit unavailable path.

    There is no first-party playground execution adapter owned by this
    platform session; a harness-owned adapter is wired in above this port
    once available, never fabricated here.
    """
    del settings  # no configuration currently changes this outcome
    return UnavailablePlaygroundInvoker()
