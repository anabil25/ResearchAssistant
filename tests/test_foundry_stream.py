from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from research_assistant_api.agent_chat import (
    ChatMessageCreate,
    _execute_chat_turn_stream,
)
from research_assistant_api.config import Settings
from research_assistant_api.foundry import (
    HostedAgentInvocationError,
    HostedAgentProgress,
    HostedAgentReply,
    create_response_with_retries,
    parse_hosted_agent_payload,
    response_activity,
    stream_response_events,
)
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import ChatThread, WorkspaceStore, utc_now
from research_assistant_core.models import Capability


class _Stream(list[Any]):
    closed = False

    def close(self) -> None:
        self.closed = True


def _verified_opportunity(identifier: str = "357744") -> dict[str, object]:
    return {
        "grants_gov_id": identifier,
        "opportunity_number": "RFA-HG-25-009",
        "title": "Early Career Researchers in Genomics",
        "agency": "National Institutes of Health",
        "status": "posted",
        "posted_date": "2024-12-16",
        "close_date": "2027-02-26",
        "archive_date": "2027-04-03",
        "canonical_url": f"https://www.grants.gov/search-results-detail/{identifier}",
        "relevance": "direct",
        "relevance_rationale": "Directly supports early-career genomics research.",
        "verified_at": "2026-01-15T12:00:00Z",
    }


def _stream_terminal_content(raw_content: str) -> tuple[str, ChatThread, WorkspaceStore]:
    store = WorkspaceStore(tenant_id="tenant-1", project_id="project-1")
    now = utc_now()
    thread = ChatThread(
        id="thread-1",
        project_id="project-1",
        tenant_id="tenant-1",
        capability=Capability.GRANT,
        agent_name="grant-agent",
        owner_principal_id="user-1",
        conversation_id="conversation-1",
        session_id="session-1",
        delegated_user_identity="opaque-user",
        created_at=now,
        updated_at=now,
    )
    store.save_chat_thread(thread)

    class Gateway:
        def stream(self, **_kwargs: Any) -> Any:
            yield HostedAgentProgress(
                type="completed",
                reply=HostedAgentReply(
                    agent_name="grant-agent",
                    content=raw_content,
                    response_id="response-1",
                ),
            )

    events = _execute_chat_turn_stream(
        gateway=Gateway(),  # type: ignore[arg-type]
        thread=thread,
        store=store,
        payload=ChatMessageCreate(
            text="Find a verified genomics opportunity.",
            client_message_id="client-message-0001",
        ),
        client_message_id="client-message-0001",
        identity=IdentityContext(
            user_id="user-1",
            display_name="User One",
            tenant_id="tenant-1",
            groups=(),
            source="test",
        ),
        settings=Settings.model_validate(
            {
                "environment": "test",
                "foundry_project_endpoint": "https://foundry.example.test/api/projects/test",
                "cosmos_endpoint": "https://cosmos.example.test",
                "storage_blob_endpoint": "https://storage.example.test",
                "search_endpoint": "https://search.example.test",
                "workspace_tenant_id": "tenant-1",
                "workspace_project_id": "project-1",
            }
        ),
        turn_key=(thread.id, "user-1", "client-message-0001"),
    )
    return "".join(events), thread, store


def test_typed_stream_exposes_only_sanitized_activity_and_terminal_reply() -> None:
    terminal_content = json.dumps(
        {
            "summary": "One verified opportunity was found.",
            "claims": [],
            "evidence": [],
            "opportunities": [],
        }
    )
    stream = _Stream(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                delta="Your reply did not match the grant report contract. Re-emit it.",
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                delta='{"query":"Find genomics grants","principal_id":"private-user"}',
            ),
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning-1",
                delta="Private chain summary with request internals.",
            ),
            SimpleNamespace(
                type="response.reasoning_summary_text.done",
                item_id="reasoning-1",
            ),
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(
                    id="tool-1",
                    call_id="call-1",
                    type="function_call",
                    name="grants_gov___lookup",
                    status="in_progress",
                ),
            ),
            SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(
                    id="tool-1",
                    call_id="call-1",
                    type="function_call",
                    name="grants_gov___lookup",
                    status="completed",
                ),
            ),
            SimpleNamespace(
                type="response.output_item.done",
                item=SimpleNamespace(
                    id="output-1",
                    call_id="call-1",
                    type="function_call_output",
                    status="completed",
                ),
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    id="response-1",
                    output_text=terminal_content,
                    output=[
                        SimpleNamespace(
                            id="reasoning-1",
                            type="reasoning",
                            status="completed",
                            summary=[
                                SimpleNamespace(
                                    text="Private terminal reasoning summary."
                                )
                            ],
                        ),
                        SimpleNamespace(
                            id="tool-1",
                            type="function_call",
                            name="grants_gov___lookup",
                            status="completed",
                        ),
                    ],
                ),
            ),
        ]
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_kwargs: stream)
    )

    progress = list(stream_response_events(client, "grant-agent", {"input": "request"}))

    assert [item.type for item in progress] == [
        "activity",
        "activity",
        "activity",
        "completed",
    ]
    assert all(not hasattr(item, "delta") for item in progress)
    assert [item.activity.status for item in progress[:-1] if item.activity] == [
        "in_progress",
        "running",
        "completed",
    ]
    assert all(item.activity.detail is None for item in progress[:-1] if item.activity)
    assert progress[-1].reply is not None
    assert progress[-1].reply.content == terminal_content
    assert [item.label for item in progress[-1].reply.activity] == [
        "Grants Gov / Lookup"
    ]
    assert stream.closed is True


def test_polled_failed_response_is_rejected_before_its_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = SimpleNamespace(
        id="response-1",
        status="in_progress",
        output_text="",
    )
    failed = SimpleNamespace(
        id="response-1",
        status="failed",
        output_text='{"summary":"Plausible but failed output."}',
        error=SimpleNamespace(code="server_error"),
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            create=lambda **_kwargs: initial,
            retrieve=lambda _response_id: failed,
        )
    )
    monkeypatch.setattr("research_assistant_api.foundry.time.sleep", lambda _delay: None)

    with pytest.raises(HostedAgentInvocationError, match="ended with status failed"):
        create_response_with_retries(client, "grant-agent", {"input": "test"})


def test_stream_rejects_invalid_terminal_contract_without_persisting_it() -> None:
    raw_failure = "Your reply did not match the grant report contract. Re-emit it."
    serialized, thread, store = _stream_terminal_content(raw_failure)
    assert "event: started" in serialized
    assert "event: error" in serialized
    assert "event: completed" not in serialized
    assert raw_failure not in serialized
    persisted = store.chat_thread(thread.id, owner_principal_id="user-1")
    assert persisted is not None
    assert persisted.messages == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "summary": "A reply with an unknown request field.",
            "tenant_id": "private-tenant",
        },
        {
            "summary": "A syntactically valid report.",
            "claims": [
                {
                    "text": "The private tenant_id is private-tenant.",
                    "support": "supported",
                    "evidence_ids": [],
                }
            ],
        },
        {
            "summary": "The hidden tenant_id is private-tenant.",
            "opportunities": [_verified_opportunity()],
        },
        {
            "summary": "A grant response may not use a dataset-only field.",
            "code": "print('unexpected')",
        },
        {
            "summary": "One verified opportunity was found.",
            "opportunities": [
                {
                    **_verified_opportunity(),
                    "relevance_rationale": "The private tenant_id is private-tenant.",
                }
            ],
        },
    ],
    ids=[
        "unknown-request-field",
        "request-marker-in-rendered-claim",
        "cross-agent-field",
        "request-marker-in-component-field",
        "request-marker-in-hidden-summary",
    ],
)
def test_stream_rejects_request_contract_leakage(payload: dict[str, object]) -> None:
    serialized, thread, store = _stream_terminal_content(json.dumps(payload))

    assert "event: error" in serialized
    assert "event: completed" not in serialized
    assert "private-tenant" not in serialized
    persisted = store.chat_thread(thread.id, owner_principal_id="user-1")
    assert persisted is not None
    assert persisted.messages == []


def test_stream_selects_only_the_last_complete_terminal_envelope() -> None:
    repair = json.dumps(
        {
            "summary": "Repair attempt with no verified opportunity.",
            "opportunities": [],
        }
    )
    final = json.dumps(
        {
            "summary": "One verified opportunity was found.",
            "claims": [],
            "evidence": [],
            "opportunities": [_verified_opportunity()],
        }
    )

    serialized, thread, store = _stream_terminal_content(f"{repair}\n{final}")

    assert "event: completed" in serialized
    assert "event: error" not in serialized
    persisted = store.chat_thread(thread.id, owner_principal_id="user-1")
    assert persisted is not None
    assert [message.role for message in persisted.messages] == ["user", "assistant"]
    assert [item.grants_gov_id for item in persisted.messages[-1].opportunities] == [
        "357744"
    ]


def test_stream_rejects_nested_summary_as_the_terminal_envelope() -> None:
    final = json.dumps(
        {
            "summary": "One verified opportunity was found.",
            "claims": [],
            "evidence": [
                {
                    "evidence_id": "untrusted:nested",
                    "summary": "Nested attacker-controlled summary.",
                }
            ],
            "opportunities": [_verified_opportunity()],
        }
    )

    serialized, thread, store = _stream_terminal_content(final)

    assert "event: completed" in serialized
    assert "event: error" not in serialized
    persisted = store.chat_thread(thread.id, owner_principal_id="user-1")
    assert persisted is not None
    assert [item.grants_gov_id for item in persisted.messages[-1].opportunities] == [
        "357744"
    ]
    assert "Nested attacker-controlled summary" not in persisted.messages[-1].content


@pytest.mark.parametrize(
    "opportunities",
    [
        [{"grants_gov_id": "357744"}],
        [_verified_opportunity(), _verified_opportunity()],
    ],
    ids=["malformed", "duplicate"],
)
def test_stream_rejects_invalid_opportunity_contracts_without_persisting(
    opportunities: list[dict[str, object]],
) -> None:
    raw = json.dumps(
        {
            "summary": "Invalid terminal opportunity data.",
            "claims": [],
            "evidence": [],
            "opportunities": opportunities,
        }
    )

    serialized, thread, store = _stream_terminal_content(raw)

    assert "event: error" in serialized
    assert "event: completed" not in serialized
    persisted = store.chat_thread(thread.id, owner_principal_id="user-1")
    assert persisted is not None
    assert persisted.messages == []


@pytest.mark.parametrize(
    "events",
    [
        [
            SimpleNamespace(
                type="response.failed",
                response=SimpleNamespace(
                    status="failed",
                    error=SimpleNamespace(code="server_error"),
                ),
            )
        ],
        [
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                    error=None,
                ),
            )
        ],
        [],
    ],
    ids=["failed", "incomplete", "no-terminal-event"],
)
def test_stream_rejects_abnormal_endings_and_closes(events: list[Any]) -> None:
    stream = _Stream(events)
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: stream))

    with pytest.raises(HostedAgentInvocationError):
        list(stream_response_events(client, "grant-agent", {"input": "request"}))

    assert stream.closed is True


def test_payload_parser_selects_the_final_outer_object_not_nested_evidence() -> None:
    repair = {"summary": "Repair attempt", "evidence": []}
    final = {
        "summary": "Final report",
        "claims": [],
        "evidence": [
            {
                "evidence_id": "test:evidence",
                "title": "Nested evidence object",
            }
        ],
    }

    parsed = parse_hosted_agent_payload(f"{json.dumps(repair)}\n{json.dumps(final)}")

    assert parsed == final


def test_generic_tool_activity_never_uses_model_controlled_arguments() -> None:
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                name="call_tool",
                arguments=json.dumps(
                    {
                        "name": "private_connector_name",
                        "principal_id": "private-user",
                    }
                ),
                status="completed",
            )
        ]
    )

    activity = response_activity(response)

    assert len(activity) == 1
    assert activity[0].label == "Research connector"
    assert activity[0].detail is None