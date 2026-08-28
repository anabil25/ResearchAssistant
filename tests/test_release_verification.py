from __future__ import annotations

import subprocess
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.verify_release import (
    BROWSER_INSTALL_TIMEOUT_SECONDS,
    BROWSER_NPM_CI_TIMEOUT_SECONDS,
    BROWSER_TEST_TIMEOUT_SECONDS,
    GrantOracle,
    ReleaseVerificationError,
    ServerSentEvent,
    expected_agent_versions,
    grant_turn_prompts,
    parse_sse_events,
    run_browser_release_gate,
    validate_agent_inventory,
    validate_connection_inventory,
    validate_deployment_environment,
    validate_grant_completion,
)


def _oracle() -> GrantOracle:
    return GrantOracle(
        grants_gov_id="357744",
        opportunity_number="RFA-HG-25-009",
        title="Supporting Talented Early Career Researchers in Genomics",
        agency="National Institutes of Health",
        status="posted",
        posted_date="2024-12-16",
        close_date="2027-02-26",
        archive_date="2027-04-03",
        canonical_url="https://www.grants.gov/search-results-detail/357744",
    )


def _message() -> dict[str, Any]:
    oracle = _oracle()
    return {
        "id": "reply-release-message-0001",
        "role": "assistant",
        "content": "The exact Grants.gov record was verified.",
        "opportunities": [
            {
                "grants_gov_id": oracle.grants_gov_id,
                "opportunity_number": oracle.opportunity_number,
                "title": oracle.title,
                "agency": oracle.agency,
                "status": oracle.status,
                "posted_date": "2024-12-16",
                "close_date": "2027-02-26",
                "archive_date": "2027-04-03",
                "canonical_url": oracle.canonical_url,
                "relevance": "direct",
                "relevance_rationale": "The user requested this exact record.",
                "verified_at": "2026-08-27T12:00:00Z",
            }
        ],
    }


def _started() -> ServerSentEvent:
    return ServerSentEvent(
        event="started",
        data={
            "type": "started",
            "message_id": "reply-1",
            "agent_name": "grant-agent",
            "created_at": "2026-08-27T12:00:00Z",
        },
    )


def test_parse_sse_events_supports_chunked_lines_and_multiline_data() -> None:
    lines = [
        b": keepalive\n",
        b"event: started\n",
        b'data: {"type":"started"}\n',
        b"\n",
        b"event: completed\n",
        b'data: {"type":"completed",\n',
        b'data: "message":{"id":"reply-1"}}\n',
        b"\n",
    ]

    events = parse_sse_events(lines)

    assert [event.event for event in events] == ["started", "completed"]
    assert events[0].data == {"type": "started"}
    assert events[1].data == {
        "type": "completed",
        "message": {"id": "reply-1"},
    }


def test_validate_grant_completion_accepts_one_exact_terminal_opportunity() -> None:
    events = [
        _started(),
        ServerSentEvent(
            event="completed",
            data={"type": "completed", "message": _message()},
        ),
    ]

    message = validate_grant_completion(events, _oracle())

    assert message["id"] == "reply-release-message-0001"


def test_release_grant_prompts_require_context_on_the_second_turn() -> None:
    first, second = grant_turn_prompts("357744")

    assert "357744" in first
    assert "357744" not in second
    assert "same conversation" in second
    assert "preceding request" in second


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda message: message.update(
                {"content": '{"authorized_connector_ids":["grants_gov"]}'}
            ),
            "internal request data",
        ),
        (
            lambda message: message.update(
                {"content": "The request tenant_id must remain private."}
            ),
            "internal request data",
        ),
        (
            lambda message: message["opportunities"][0].update(
                {"relevance_rationale": "The tenant_id must remain private."}
            ),
            "internal request data",
        ),
        (
            lambda message: message.update(
                {"content": '{"summary":"This is still a raw contract."}'}
            ),
            "raw JSON",
        ),
        (
            lambda message: message["opportunities"].append(
                dict(message["opportunities"][0])
            ),
            "exactly one",
        ),
        (
            lambda message: message["opportunities"][0].update(
                {"canonical_url": "https://example.test/not-grants-gov"}
            ),
            "canonical_url",
        ),
        (
            lambda message: message["opportunities"][0].update(
                {"close_date": "2099-01-01"}
            ),
            "close_date",
        ),
    ],
    ids=[
        "private-envelope",
        "private-request-marker",
        "private-component-marker",
        "raw-json-content",
        "duplicate-opportunity",
        "wrong-link",
        "wrong-date",
    ],
)
def test_validate_grant_completion_rejects_non_release_output(
    mutate: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    message = _message()
    mutate(message)
    events = [
        _started(),
        ServerSentEvent(
            event="completed",
            data={"type": "completed", "message": message},
        ),
    ]

    with pytest.raises(ReleaseVerificationError, match=expected):
        validate_grant_completion(events, _oracle())


def test_validate_grant_completion_rejects_delta_and_error_events() -> None:
    events = parse_sse_events(
        [
            "event: text_delta\n",
            'data: {"type":"text_delta","delta":"private"}\n',
            "\n",
            "event: error\n",
            'data: {"type":"error","detail":"failed"}\n',
            "\n",
        ]
    )

    with pytest.raises(ReleaseVerificationError, match="forbidden text delta"):
        validate_grant_completion(events, _oracle())


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (
            [_started(), ServerSentEvent(event="mystery", data={"type": "mystery"})],
            "unsupported event",
        ),
        (
            [
                _started(),
                ServerSentEvent(
                    event="activity",
                    data={
                        "type": "activity",
                        "activity_id": "activity-1",
                        "activity": {
                            "kind": "tool",
                            "label": "Research connector",
                            "status": "completed",
                            "detail": "private payload",
                        },
                    },
                ),
            ],
            "invalid activity",
        ),
        (
            [
                ServerSentEvent(
                    event="started",
                    data={**_started().data, "type": "activity"},
                )
            ],
            "do not match",
        ),
        (
            [
                _started(),
                ServerSentEvent(
                    event="completed",
                    data={"type": "completed", "message": _message()},
                ),
                ServerSentEvent(
                    event="activity",
                    data={
                        "type": "activity",
                        "activity_id": "activity-1",
                        "activity": {
                            "kind": "tool",
                            "label": "Research connector",
                            "status": "completed",
                            "detail": None,
                        },
                    },
                ),
            ],
            "final completed",
        ),
    ],
    ids=["unknown", "activity-detail", "type-mismatch", "after-completed"],
)
def test_validate_grant_completion_rejects_invalid_protocol_grammar(
    events: list[ServerSentEvent],
    expected: str,
) -> None:
    with pytest.raises(ReleaseVerificationError, match=expected):
        validate_grant_completion(events, _oracle())


def test_browser_release_gate_bootstraps_locked_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)

    run_browser_release_gate("https://web.example.test/", "357744")

    executable = "npm.cmd" if __import__("os").name == "nt" else "npm"
    node = "node.exe" if __import__("os").name == "nt" else "node"
    assert [command for command, _ in calls] == [
        [executable, "ci", "--no-audit", "--no-fund"],
        [
            node,
            str(
                __import__("pathlib").Path(__file__).parents[1]
                / "apps"
                / "web"
                / "node_modules"
                / "playwright"
                / "cli.js"
            ),
            "install",
            "chromium",
        ],
        [executable, "run", "test:release"],
    ]
    assert [kwargs["timeout"] for _, kwargs in calls] == [
        BROWSER_NPM_CI_TIMEOUT_SECONDS,
        BROWSER_INSTALL_TIMEOUT_SECONDS,
        BROWSER_TEST_TIMEOUT_SECONDS,
    ]
    release_environment = calls[-1][1]["env"]
    assert release_environment["PLAYWRIGHT_BASE_URL"] == "https://web.example.test"
    assert "RESEARCH_RELEASE_GRANT_ID" not in release_environment


def test_browser_release_gate_converts_a_child_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ReleaseVerificationError, match="browser bootstrap or gate failed"):
        run_browser_release_gate("https://web.example.test/", "357744")


def test_release_inventory_validates_identity_agents_and_connections() -> None:
    incarnation = "111122223333"
    account = "cog-research-111122223333"
    project = "research-11112222"
    values = {
        "AZURE_ENV_NAME": "research",
        "AZURE_DEPLOYMENT_INCARNATION": incarnation,
        "AZURE_AI_ACCOUNT_NAME": account,
        "AZURE_AI_PROJECT_NAME": project,
        "FOUNDRY_ACCOUNT_NAME": account,
        "FOUNDRY_PROJECT_NAME": project,
        "FOUNDRY_PROJECT_ENDPOINT": (
            f"https://{account}.services.ai.azure.com/api/projects/{project}"
        ),
        **{
            f"AGENT_{name.replace('-', '_').upper()}_VERSION": "3"
            for name in (
                "research-coordinator",
                "literature-agent",
                "grant-agent",
                "matching-agent",
                "dataset-agent",
                "institution-agent",
                "screening-agent",
            )
        },
    }
    digest = "a" * 64
    versions = [
        SimpleNamespace(
            name=name,
            version=version,
            status="active",
            definition=SimpleNamespace(
                environment_variables={"AGENT_SOURCE_TREE_DIGEST": digest},
                protocol_versions=[
                    SimpleNamespace(protocol="responses", version="2.0.0")
                ],
            ),
        )
        for name, version in expected_agent_versions(values).items()
    ]
    expected_connections = {
        "rc-grants": ("RemoteTool", "https://gateway.example.test/grants/mcp"),
        "acr-release": ("ContainerRegistry", "registry.example.test"),
    }
    connection_payload = {
        "value": [
            {
                "name": name,
                "properties": {"category": category, "target": target},
            }
            for name, (category, target) in expected_connections.items()
        ]
    }

    validate_deployment_environment(values)
    assert set(
        validate_agent_inventory(
            versions,
            expected_versions=expected_agent_versions(values),
            source_tree_digest=digest,
        )
    ) == set(expected_agent_versions(values))
    validate_connection_inventory(connection_payload, expected=expected_connections)


def test_release_inventory_rejects_stale_agent_digest_and_extra_connection() -> None:
    version = SimpleNamespace(
        name="grant-agent",
        version="4",
        status="active",
        definition=SimpleNamespace(
            environment_variables={"AGENT_SOURCE_TREE_DIGEST": "b" * 64},
            protocol_versions=[
                SimpleNamespace(protocol="responses", version="2.0.0")
            ],
        ),
    )
    with pytest.raises(ReleaseVerificationError, match="source-tree digest"):
        validate_agent_inventory(
            [version],
            expected_versions={"grant-agent": "4"},
            source_tree_digest="a" * 64,
        )

    with pytest.raises(ReleaseVerificationError, match="unexpected"):
        validate_connection_inventory(
            {
                "value": [
                    {
                        "name": "expected",
                        "properties": {
                            "category": "RemoteTool",
                            "target": "https://expected.example.test/mcp",
                        },
                    },
                    {
                        "name": "legacy-duplicate",
                        "properties": {
                            "category": "RemoteTool",
                            "target": "https://old.example.test/mcp",
                        },
                    },
                ]
            },
            expected={
                "expected": ("RemoteTool", "https://expected.example.test/mcp")
            },
        )