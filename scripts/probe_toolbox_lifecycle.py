from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from agent_framework import Agent, ChatContext
from agent_framework_foundry_hosting import FoundryToolbox  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).parents[1] / "agents"))

from shared.errors import ConfigurationError
from shared.middleware import ConnectorToolExposureMiddleware
from shared.profiles import get_profile
from shared.tools import request_tool_names_for_profile


class RecordingToolbox(FoundryToolbox):  # type: ignore[misc]
    def __init__(self, events: list[str]) -> None:
        super().__init__(
            cast(Any, object()),
            url="https://toolbox.example/mcp?api-version=v1",
        )
        self._events = events

    async def connect(self, *, reset: bool = False) -> None:
        assert not reset
        self._events.append("connect")
        self.is_connected = True
        await self.load_tools()

    async def load_tools(self) -> None:
        self._events.append("load")

    async def close(self) -> None:
        self._events.append("close")
        self.is_connected = False
        http_client = getattr(self, "_httpx_client", None)
        if http_client is not None:
            await http_client.aclose()


async def _probe_owner_lifecycle() -> None:
    events: list[str] = []
    agent = Agent(
        client=cast(Any, object()),
        tools=RecordingToolbox(events),
    )
    async with agent:
        assert events == ["connect", "load"]
    assert events == ["connect", "load", "close"]


async def _probe_concurrent_isolation() -> None:
    profile = get_profile("literature")
    middleware = ConnectorToolExposureMiddleware(profile)
    shared_tools = [
        SimpleNamespace(name="pubmed___search"),
        SimpleNamespace(name="pubmed___lookup"),
        SimpleNamespace(name="crossref___search"),
        SimpleNamespace(name="crossref___lookup"),
        SimpleNamespace(name="web_search"),
    ]
    shared_options = {"tools": shared_tools}
    observed: dict[str, frozenset[str]] = {}

    async def invoke(label: str, connector_ids: tuple[str, ...]) -> None:
        context = ChatContext(
            client=cast(Any, object()),
            messages=[],
            options=shared_options,
            function_invocation_kwargs={"authorized_connector_ids": connector_ids},
        )

        async def capture() -> None:
            await asyncio.sleep(0)
            assert context.options is not None
            observed[label] = frozenset(tool.name for tool in context.options["tools"])

        await middleware.process(context, capture)

    await asyncio.gather(
        invoke("pubmed", ("pubmed",)),
        invoke("crossref", ("crossref",)),
    )
    assert observed == {
        "pubmed": frozenset({"pubmed___search", "pubmed___lookup", "web_search"}),
        "crossref": frozenset({"crossref___search", "crossref___lookup", "web_search"}),
    }
    assert shared_options["tools"] is shared_tools

    missing_context = ChatContext(
        client=cast(Any, object()),
        messages=[],
        options={"tools": [SimpleNamespace(name="web_search")]},
        function_invocation_kwargs={"authorized_connector_ids": ("pubmed",)},
    )
    try:
        await middleware.process(missing_context, _unexpected_call)
    except ConfigurationError as exc:
        assert "missing authorized tools" in str(exc)
    else:
        raise AssertionError("missing authorized Toolbox tools were accepted")


async def _unexpected_call() -> None:
    raise AssertionError("middleware continued after a failed availability check")


def _probe_unauthorized_rejection() -> None:
    try:
        request_tool_names_for_profile(
            get_profile("literature"),
            ("grants_gov",),
        )
    except ConfigurationError as exc:
        assert "outside the profile Toolbox surface" in str(exc)
    else:
        raise AssertionError("connector outside the profile policy was accepted")


async def _run() -> None:
    await _probe_owner_lifecycle()
    _probe_unauthorized_rejection()
    await _probe_concurrent_isolation()


if __name__ == "__main__":
    asyncio.run(_run())
    print("Toolbox lifecycle probe passed.")