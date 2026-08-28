"""Release checks against the running, deployed Research Assistant stack."""

from __future__ import annotations

import os

import httpx
import pytest

from scripts.verify_release import DEFAULT_GRANT_ID, verify_api_release

LIVE_API_URL = os.environ.get(
    "RESEARCH_LIVE_API_URL",
    "http://localhost:3000/api/backend",
).rstrip("/")

pytestmark = pytest.mark.skipif(
    os.environ.get("RESEARCH_RUN_LIVE_TESTS") != "1",
    reason="Set RESEARCH_RUN_LIVE_TESTS=1 to run deployed release checks.",
)


def _active_project(client: httpx.Client) -> dict[str, object]:
    response = client.get("/api/projects")
    assert response.status_code == 200, response.text
    projects = response.json()
    if not projects:
        created = client.post(
            "/api/projects",
            json={
                "name": "Deployment verification workspace",
                "description": "Private workspace created by the fresh-deployment release gate.",
            },
        )
        assert created.status_code == 201, created.text
        projects = [created.json()]
    project = next((item for item in projects if item["is_active"]), projects[0])
    assert str(project["id"]).strip()
    return dict(project)


def test_live_workspace_and_service_contracts() -> None:
    with httpx.Client(base_url=LIVE_API_URL, timeout=60.0) as client:
        for path in ("/health", "/ready"):
            response = client.get(path)
            assert response.status_code == 200, response.text

        project = _active_project(client)
        headers = {"X-Research-Project-ID": str(project["id"])}
        for path in (
            "/api/workspace",
            "/api/library",
            "/api/runs",
            "/api/approvals",
            "/api/connectors",
            "/api/settings",
            "/api/agents",
            "/api/workflows",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, f"{path}: {response.text}"


def test_live_hosted_agent_session_and_turn() -> None:
    with httpx.Client(base_url=LIVE_API_URL, timeout=240.0) as client:
        project = _active_project(client)
        headers = {"X-Research-Project-ID": str(project["id"])}

        catalog = client.get(
            "/api/agent-chat/agents",
            params={"capability": "literature"},
            headers=headers,
        )
        assert catalog.status_code == 200, catalog.text
        assert [agent["name"] for agent in catalog.json()] == ["literature-agent"]

        opened = client.post(
            "/api/agent-chat/threads",
            headers=headers,
            json={"capability": "literature", "agent_name": "literature-agent"},
        )
        assert opened.status_code == 201, opened.text
        thread = opened.json()
        assert "conversation" not in opened.text.lower()
        assert "session" not in opened.text.lower()

        answered = client.post(
            f"/api/agent-chat/threads/{thread['id']}/messages",
            headers=headers,
            json={
                "text": "Return one sentence confirming readiness. Do not invent evidence.",
                "client_message_id": "live-release-turn-0001",
            },
        )
        assert answered.status_code == 200, answered.text
        content = str(answered.json()["content"])
        assert content.strip()


def test_live_exact_grant_sse_matches_independent_provider_oracle() -> None:
    oracle = verify_api_release(
        LIVE_API_URL,
        os.environ.get("RESEARCH_RELEASE_GRANT_ID", DEFAULT_GRANT_ID),
    )

    assert oracle.canonical_url == (
        f"https://www.grants.gov/search-results-detail/{oracle.grants_gov_id}"
    )
