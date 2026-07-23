from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import research_assistant_api.foundry as foundry
from azure.core.credentials import AccessToken, TokenCredential
from openai import APIStatusError
from research_assistant_api.app import _agent_message
from research_assistant_api.config import Settings
from research_assistant_api.foundry import HostedAgentInvocationError
from research_assistant_core.models import Capability, ResearchRequest
from research_assistant_core.service import ResearchService
from research_assistant_core.studio_models import StudioRunRequest


class FakeCredential(TokenCredential):
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("fake", 4_102_444_800)


def test_gateway_forwards_request_tool_preference(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_text="Bounded analysis", id="response-1")

    class FakeProject:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["allow_preview"] is True

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "literature-agent"
            return SimpleNamespace(responses=FakeResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", FakeProject)
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    offline = gateway.invoke(
        "Analyze supplied evidence only.",
        agent_name="literature-agent",
        allow_tools=False,
    )
    online = gateway.invoke(
        "Research current public guidance.",
        agent_name="literature-agent",
        allow_tools=True,
    )

    assert offline.content == "Bounded analysis"
    assert online.response_id == "response-1"
    assert calls == [
        {
            "input": "Analyze supplied evidence only.",
        },
        {
            "input": "Research current public guidance.",
        },
    ]


def test_public_online_agent_message_excludes_internal_objective_and_evidence() -> None:
    generic = ResearchService().run(
        Capability.LITERATURE,
        ResearchRequest(query="internal auditable synthesis objective"),
    )
    payload = StudioRunRequest(
        objective="Confidential project objective must remain internal.",
        online_research=True,
        inputs={
            "public_search_query": "current public reproducibility guidance",
            "public_research_acknowledged": True,
        },
    )

    message = _agent_message(Capability.LITERATURE, payload, generic)

    assert "current public reproducibility guidance" in message
    assert "Confidential project objective" not in message
    assert generic.citations[0].quote not in message


def test_gateway_retries_documented_session_not_ready_sequence(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class RetryResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise APIStatusError(
                    "session not ready",
                    response=httpx.Response(
                        424,
                        request=httpx.Request(
                            "POST",
                            "https://foundry.example.test/responses",
                        ),
                    ),
                    body={"error": {"code": "session_not_ready"}},
                )
            return SimpleNamespace(output_text="ready", id="response-ready")

    class RetryProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "dataset-agent"
            return SimpleNamespace(responses=RetryResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", RetryProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    response = gateway.invoke(
        "profile supplied data",
        agent_name="dataset-agent",
    )

    assert response.content == "ready"
    assert attempts == 3
    assert sleeps == [15, 30]


def test_gateway_rejects_empty_success_shaped_agent_response(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            return SimpleNamespace(output_text="  ", id="empty-response")

    class EmptyProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            return SimpleNamespace(responses=EmptyResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", EmptyProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    with pytest.raises(HostedAgentInvocationError, match="returned no output"):
        gateway.invoke("public query", agent_name="literature-online-agent")

    assert attempts == 3
    assert sleeps == [2, 5]


def test_gateway_recovers_from_transient_empty_agent_output(
    monkeypatch: Any,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class TransientEmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return SimpleNamespace(output_text=" ", id="empty-response")
            return SimpleNamespace(output_text="Recovered analysis", id="response-ready")

    class TransientEmptyProject:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "literature-agent"
            return SimpleNamespace(responses=TransientEmptyResponses())

    monkeypatch.setattr(foundry, "AIProjectClient", TransientEmptyProject)
    monkeypatch.setattr(
        "research_assistant_api.foundry.time.sleep",
        sleeps.append,
    )
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test/api/projects/test",
    )
    gateway = foundry.HostedAgentGateway(
        Settings(),
        credential=FakeCredential(),
    )

    response = gateway.invoke("analyze evidence", agent_name="literature-agent")

    assert response.content == "Recovered analysis"
    assert response.response_id == "response-ready"
    assert attempts == 2
    assert sleeps == [2]
