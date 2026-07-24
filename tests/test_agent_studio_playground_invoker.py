from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.playground_invoker import (
    InMemoryPlaygroundInvoker,
    PlaygroundInvocationError,
    PlaygroundInvocationResult,
    UnavailablePlaygroundInvoker,
    build_playground_invoker,
)
from research_assistant_api.config import Settings


def test_unavailable_playground_invoker_raises() -> None:
    with pytest.raises(PlaygroundInvocationError, match="unavailable"):
        UnavailablePlaygroundInvoker().invoke(instructions="be helpful", input_text="hello")


def test_in_memory_playground_invoker_uses_supplied_respond_fn() -> None:
    invoker = InMemoryPlaygroundInvoker(
        lambda input_text, instructions: PlaygroundInvocationResult(
            output=f"echo: {input_text}/{instructions}", trace=(), tool_calls=()
        )
    )
    result = invoker.invoke(instructions="be helpful", input_text="hello")
    assert result.output == "echo: hello/be helpful"
    assert result.trace == ()
    assert result.tool_calls == ()


def test_in_memory_playground_invoker_defaults_to_echoing_input() -> None:
    invoker = InMemoryPlaygroundInvoker()
    result = invoker.invoke(instructions="be helpful", input_text="hello")
    assert result.output == "hello"
    assert result.trace == ()
    assert result.tool_calls == ()


def test_build_playground_invoker_always_returns_unavailable() -> None:
    """No first-party playground execution adapter is owned by this
    platform session; the factory has no configuration-driven real branch,
    unlike ``build_model_discovery``."""
    assert isinstance(build_playground_invoker(Settings()), UnavailablePlaygroundInvoker)
    assert isinstance(
        build_playground_invoker(Settings(foundry_project_endpoint="https://project.example.test")),
        UnavailablePlaygroundInvoker,
    )
