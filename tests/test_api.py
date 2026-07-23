from __future__ import annotations

from fastapi.testclient import TestClient
from research_assistant_api.app import app


def test_health_and_security_headers() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"].startswith("req-")


def test_capabilities_and_research_endpoint() -> None:
    with TestClient(app) as client:
        capabilities = client.get("/api/capabilities")
        result = client.post(
            "/api/research/literature",
            json={"query": "Compare auditable research synthesis"},
        )

    assert capabilities.status_code == 200
    assert len(capabilities.json()) == 6
    assert result.status_code == 200
    assert result.json()["run"]["capability"] == "literature"
    assert result.json()["citations"]


def test_mock_assistant_uses_bounded_capability() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/assistant",
            json={
                "message": "When must AI be disclosed to the IRB?",
                "capability": "institutional_qa",
            },
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "bounded"
    assert response.json()["agent_name"] == "institutional_qa-deterministic"
