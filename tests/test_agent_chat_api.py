"""Live contract checks for the conversational Hosted Agent surface."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from research_assistant_api.agent_chat import (
    AgentChatGateway,
    _contract_envelope,
    _render_agent_reply,
    _require_ready_agent_connectors,
    _resolved_nothing,
    _safe_upload_path,
    _verified_grant_opportunities,
    build_agent_chat_gateway,
    delegated_user_identity_for,
)
from research_assistant_api.config import Settings
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    ChatAttachment,
    ChatThread,
    ConnectorSetting,
    ConnectorUpdate,
    WorkspaceStore,
    reconcile_required_connectors,
    utc_now,
)
from research_assistant_core.models import Capability

LIVE_API_URL = os.environ.get(
    "RESEARCH_LIVE_API_URL",
    "http://localhost:3000/api/backend",
).rstrip("/")


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "foundry_project_endpoint": "https://foundry.example.test/api/projects/test",
        "cosmos_endpoint": "https://cosmos.example.test",
        "storage_blob_endpoint": "https://storage.example.test",
        "search_endpoint": "https://search.example.test",
        "workspace_tenant_id": "tenant-1",
        "workspace_project_id": "project-1",
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture(scope="module")
def live_client() -> Iterator[httpx.Client]:
    if os.environ.get("RESEARCH_RUN_LIVE_TESTS") != "1":
        pytest.skip("Set RESEARCH_RUN_LIVE_TESTS=1 to run deployed chat checks.")
    with httpx.Client(base_url=LIVE_API_URL, timeout=240.0) as client:
        response = client.get("/ready")
        assert response.status_code == 200, (
            f"Live Research Assistant API is required at {LIVE_API_URL}: "
            f"{response.status_code} {response.text}"
        )
        yield client


@pytest.fixture(scope="module")
def project_headers(live_client: httpx.Client) -> dict[str, str]:
    response = live_client.get("/api/projects")
    assert response.status_code == 200, response.text
    projects = response.json()
    assert projects, "The live deployment must expose at least one accessible project."
    active = next((project for project in projects if project["is_active"]), projects[0])
    return {"X-Research-Project-ID": active["id"]}


@pytest.fixture(scope="module")
def literature_thread(
    live_client: httpx.Client,
    project_headers: dict[str, str],
) -> dict[str, object]:
    response = live_client.post(
        "/api/agent-chat/threads",
        headers=project_headers,
        json={"capability": "literature", "agent_name": "literature-agent"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_live_deployment_has_no_mock_mode(live_client: httpx.Client) -> None:
    response = live_client.get("/health")
    assert response.status_code == 200, response.text
    assert "mock" not in response.text.lower()


@pytest.mark.parametrize(
    ("capability", "agent_name"),
    [
        ("literature", "literature-agent"),
        ("grant", "grant-agent"),
        ("matching", "matching-agent"),
        ("dataset", "dataset-agent"),
        ("screening", "screening-agent"),
    ],
)
def test_live_catalog_exposes_the_deployed_agent(
    live_client: httpx.Client,
    capability: str,
    agent_name: str,
) -> None:
    response = live_client.get(
        "/api/agent-chat/agents",
        params={"capability": capability},
    )
    assert response.status_code == 200, response.text
    assert [agent["name"] for agent in response.json()] == [agent_name]


def test_live_thread_never_leaks_platform_identifiers(
    literature_thread: dict[str, object],
) -> None:
    assert set(literature_thread) == {
        "id",
        "capability",
        "agent_name",
        "created_at",
        "updated_at",
        "messages",
        "attachments",
    }
    assert "conversation" not in str(literature_thread).lower()
    assert "session" not in str(literature_thread).lower()


def test_live_agent_answers_without_fabricated_runtime_output(
    live_client: httpx.Client,
    project_headers: dict[str, str],
    literature_thread: dict[str, object],
) -> None:
    response = live_client.post(
        f"/api/agent-chat/threads/{literature_thread['id']}/messages",
        headers=project_headers,
        json={
            "text": (
                "State whether this project's indexed evidence supports a claim "
                "about current population trends. Cite indexed evidence or abstain."
            ),
            "client_message_id": "live-literature-turn-0001",
        },
    )
    assert response.status_code == 200, response.text
    content = str(response.json()["content"])
    assert content.strip()
    assert "mock" not in content.lower()
    assert "would have received this turn" not in content.lower()


def test_live_surface_rejects_an_agent_outside_the_capability(
    live_client: httpx.Client,
    project_headers: dict[str, str],
) -> None:
    response = live_client.post(
        "/api/agent-chat/threads",
        headers=project_headers,
        json={"capability": "literature", "agent_name": "dataset-agent"},
    )
    assert response.status_code == 422


def test_unsupported_claims_render_as_not_established() -> None:
    rendered = _render_agent_reply(
        json.dumps(
            {
                "summary": "Evidence-bounded result.",
                "claims": [
                    {
                        "text": "The supplied table has 12 rows.",
                        "support": "supported",
                        "evidence_ids": ["dataset-1"],
                    },
                    {
                        "text": "The intervention caused the observed change.",
                        "support": "unsupported",
                        "evidence_ids": [],
                    },
                ],
            }
        )
    )

    findings, not_established = rendered.split("**Not established**")
    assert "The supplied table has 12 rows." in findings
    assert "caused the observed change" not in findings
    assert "Unsupported: The intervention caused the observed change." in not_established


def test_verified_grant_opportunities_remain_structured_exact_links() -> None:
    canonical_url = "https://www.grants.gov/search-results-detail/357744"
    raw = json.dumps(
        {
            "summary": "RFA-HG-25-009 is directly relevant.",
            "claims": [
                {
                    "text": "Grants.gov lists RFA-HG-25-009 as posted.",
                    "support": "supported",
                    "evidence_ids": ["connector:grants_gov:357744"],
                },
                {
                    "text": "The supplied project summary targets genomics.",
                    "support": "supported",
                    "evidence_ids": ["file:project-summary.md"],
                },
                {
                    "text": "A second Grants.gov result was also retrieved.",
                    "support": "supported",
                    "evidence_ids": ["connector:grants_gov:358176"],
                },
            ],
            "evidence": [
                {
                    "evidence_id": "connector:grants_gov:357744",
                    "title": "RFA-HG-25-009",
                    "source_uri": canonical_url,
                },
                {
                    "evidence_id": "file:project-summary.md",
                    "title": "Project summary",
                    "source_uri": None,
                },
                {
                    "evidence_id": "connector:grants_gov:358176",
                    "title": "Another Grants.gov opportunity",
                    "source_uri": "https://www.grants.gov/search-results-detail/358176",
                },
            ],
            "opportunities": [
                {
                    "grants_gov_id": "357744",
                    "opportunity_number": "RFA-HG-25-009",
                    "title": (
                        "Supporting Talented Early Career Researchers in Genomics "
                        "(R01 Clinical Trial Optional)"
                    ),
                    "agency": "National Institutes of Health",
                    "status": "posted",
                    "posted_date": "2024-12-16",
                    "close_date": "2027-02-26",
                    "archive_date": "2027-04-03",
                    "canonical_url": canonical_url,
                    "relevance": "direct",
                    "relevance_rationale": "The opportunity explicitly supports genomics research.",
                    "verified_at": "2026-08-27T12:00:00Z",
                },
                {
                    "grants_gov_id": "357744",
                    "opportunity_number": "duplicate",
                    "title": "Duplicate",
                    "agency": "Duplicate",
                    "status": "posted",
                    "canonical_url": canonical_url,
                    "relevance": "adjacent",
                    "relevance_rationale": "Duplicate record.",
                    "verified_at": "2026-08-27T12:00:00Z",
                },
                {
                    "grants_gov_id": "123",
                    "opportunity_number": "MALFORMED",
                    "title": "Malformed URL",
                    "agency": "Unknown",
                    "status": "posted",
                    "canonical_url": "https://www.grants.gov/search-results-detail/999",
                    "relevance": "direct",
                    "relevance_rationale": "Must be rejected.",
                    "verified_at": "2026-08-27T12:00:00Z",
                },
            ],
        }
    )

    opportunities = _verified_grant_opportunities(raw)
    assert len(opportunities) == 1
    assert opportunities[0].grants_gov_id == "357744"
    assert opportunities[0].canonical_url == canonical_url
    assert opportunities[0].opportunity_number == "RFA-HG-25-009"
    rendered = _render_agent_reply(raw)
    assert (
        "RFA-HG-25-009 is the strongest match for this request: "
        "The opportunity explicitly supports genomics research."
    ) in rendered
    assert "RFA-HG-25-009 is directly relevant." not in rendered
    assert "Grants.gov lists RFA-HG-25-009 as posted." not in rendered
    assert "A second Grants.gov result was also retrieved." not in rendered
    assert "Another Grants.gov opportunity" not in rendered
    assert canonical_url not in rendered
    assert "The supplied project summary targets genomics." in rendered
    assert "Project summary" in rendered
    assert _resolved_nothing(raw) is False


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\config", "config"),
        ("/absolute/path/report.csv", "report.csv"),
        ("...", "attachment"),
        (".hidden", "hidden"),
        ("weird;name|<>.csv", "weirdname.csv"),
        (None, "attachment"),
        ("", "attachment"),
    ],
)
def test_upload_paths_are_sandbox_relative(
    filename: str | None,
    expected: str,
) -> None:
    assert _safe_upload_path(filename) == expected


def test_gateway_composition_requires_foundry() -> None:
    with pytest.raises(
        ValidationError,
        match="foundry_project_endpoint",
    ):
        build_agent_chat_gateway(_settings(foundry_project_endpoint=None))

    gateway = build_agent_chat_gateway(_settings())
    assert isinstance(gateway, AgentChatGateway)


def test_delegated_user_identity_is_stable_and_opaque() -> None:
    identity = IdentityContext(
        user_id="user-1",
        display_name="Ada",
        tenant_id="tenant-1",
        groups=("researchers",),
        source="gateway",
    )
    store = WorkspaceStore(project_id="project-1", tenant_id="tenant-1")
    first = delegated_user_identity_for(identity, store)
    second = delegated_user_identity_for(identity, store)
    assert first == second
    assert first.startswith("ra:")
    assert len(first) == 67
    assert all(character in "0123456789abcdef" for character in first.removeprefix("ra:"))


def test_chat_envelope_selects_only_ready_assigned_connectors() -> None:
    identity = IdentityContext(
        user_id="user-1",
        display_name="Ada",
        tenant_id="tenant-1",
        groups=("researchers",),
        source="gateway",
    )
    store = WorkspaceStore(project_id="project-1", tenant_id="tenant-1")
    grants = store.update_connector(
        "grants_gov",
        update=ConnectorUpdate(enabled=True, assigned_agents=["grant"]),
    )
    assert grants is not None
    store.record_connector_test("grants_gov", "ready")
    nih = store.update_connector(
        "nih_reporter",
        update=ConnectorUpdate(enabled=True, assigned_agents=["matching"]),
    )
    assert nih is not None
    store.record_connector_test("nih_reporter", "ready")
    now = utc_now()
    thread = ChatThread(
        id="chat-1",
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

    envelope = json.loads(
        _contract_envelope(
            thread,
            store=store,
            identity=identity,
            settings=_settings(
                workspace_tenant_id="tenant-1",
                workspace_project_id="project-1",
            ),
            text="Find genomics grants.",
            attachments=[
                ChatAttachment(
                    path="project-facts.md",
                    size_bytes=128,
                    content_type="text/markdown",
                    uploaded_at=now,
                )
            ],
        )
    )

    assert envelope["authorized_connector_ids"] == ["grants_gov"]
    assert envelope["query"] == "Find genomics grants."
    assert envelope["session_files"] == [
        {
            "evidence_id": "file:project-facts.md",
            "path": "project-facts.md",
            "content_type": "text/markdown",
            "size_bytes": 128,
        }
    ]


def test_required_connectors_start_enabled_assigned_and_unverified() -> None:
    store = WorkspaceStore(project_id="project-1", tenant_id="tenant-1")
    connectors = {item.id: item for item in store.connectors()}

    assert connectors["pubmed"].required is True
    assert connectors["pubmed"].enabled is True
    assert connectors["pubmed"].assigned_agents == ["literature", "screening"]
    assert connectors["pubmed"].test_status == "not_configured"

    assert connectors["grants_gov"].required is True
    assert connectors["grants_gov"].enabled is True
    assert connectors["grants_gov"].assigned_agents == ["grant"]
    assert connectors["grants_gov"].test_status == "not_configured"

    assert connectors["crossref"].required is False
    assert connectors["crossref"].enabled is False
    assert connectors["crossref"].assigned_agents == []


def test_required_connector_configuration_cannot_remove_policy_assignments() -> None:
    store = WorkspaceStore(project_id="project-1", tenant_id="tenant-1")

    with pytest.raises(ValueError, match="Required project connector"):
        store.update_connector(
            "grants_gov",
            ConnectorUpdate(enabled=False, assigned_agents=["grant"]),
        )
    with pytest.raises(ValueError, match="Required project connector"):
        store.update_connector(
            "grants_gov",
            ConnectorUpdate(enabled=True, assigned_agents=[]),
        )


def test_legacy_required_connector_is_reconciled_without_hiding_failure() -> None:
    current = next(
        item
        for item in WorkspaceStore(
            project_id="project-1",
            tenant_id="tenant-1",
        ).connectors()
        if item.id == "grants_gov"
    )
    legacy_payload = current.model_dump(mode="json")
    legacy_payload.pop("required")
    legacy_payload.update(
        {
            "enabled": False,
            "assigned_agents": [],
            "test_status": "unavailable",
        }
    )

    reconciled, changed = reconcile_required_connectors(
        [ConnectorSetting.model_validate(legacy_payload)]
    )

    assert [item.id for item in changed] == ["grants_gov", "pubmed"]
    assert reconciled[0].required is True
    assert reconciled[0].enabled is True
    assert reconciled[0].assigned_agents == ["grant"]
    assert reconciled[0].test_status == "unavailable"


def test_partial_legacy_catalog_restores_a_missing_required_connector() -> None:
    connectors = [
        item
        for item in WorkspaceStore(
            project_id="project-1",
            tenant_id="tenant-1",
        ).connectors()
        if item.id != "grants_gov"
    ]

    reconciled, changed = reconcile_required_connectors(connectors)

    restored = next(item for item in reconciled if item.id == "grants_gov")
    assert restored.required is True
    assert restored.enabled is True
    assert restored.assigned_agents == ["grant"]
    assert restored.test_status == "not_configured"
    assert [item.id for item in changed] == ["grants_gov"]


def test_grant_turn_requires_a_live_ready_grants_gov_connector() -> None:
    store = WorkspaceStore(project_id="project-1", tenant_id="tenant-1")
    now = utc_now()
    thread = ChatThread(
        id="chat-required-connector",
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

    with pytest.raises(HTTPException) as exc_info:
        _require_ready_agent_connectors(thread, store)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "Required connector Grants.gov is not ready for grant-agent. "
        "Test it in Project Settings, then retry."
    )

    store.record_connector_test("grants_gov", "ready")
    _require_ready_agent_connectors(thread, store)
