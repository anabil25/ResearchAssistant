from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from research_assistant_api.app import app
from research_assistant_api.approval_context import (
    ApprovalContextRequest,
    ResolvedApprovalContext,
)
from research_assistant_api.config import Settings
from research_assistant_api.connector_gateway import DisabledConnectorGateway
from research_assistant_api.studios import validate_agent_insight
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability
from research_assistant_core.studio_models import EvidenceState


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _principal(tenant_id: str, groups: list[str]) -> str:
    payload = {
        "userId": "user-1",
        "userDetails": "User One",
        "claims": [
            {"typ": "tid", "val": tenant_id},
            *({"typ": "groups", "val": group} for group in groups),
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


class TrustedDatasetApprovalResolver:
    is_durable = True

    async def resolve(
        self,
        request: ApprovalContextRequest,
    ) -> ResolvedApprovalContext:
        return ResolvedApprovalContext(
            request_digest=request.digest,
            approval_decision_id="approval-inline-1",
            invocation_id="invocation-inline-1",
        )


def test_demo_identity_uses_the_configured_workspace_tenant() -> None:
    from research_assistant_api.identity import resolve_identity

    request = type("Request", (), {"headers": {}})()
    identity = resolve_identity(
        request,
        Settings(
            workspace_tenant_id="accelerator-tenant",
            allow_demo_identity=True,
        ),
    )

    assert identity.tenant_id == "accelerator-tenant"


def test_workspace_operational_surfaces_are_populated(client: TestClient) -> None:
    workspace = client.get("/api/workspace")
    library = client.get("/api/library")
    runs = client.get("/api/runs")
    approvals = client.get("/api/approvals")
    connectors = client.get("/api/connectors")
    settings = client.get("/api/settings")
    agents = client.get("/api/agents")

    assert workspace.status_code == 200
    assert workspace.json()["library_items"] == len(library.json())
    assert len(library.json()) >= 8
    assert len(runs.json()) >= 5
    assert approvals.json()
    assert len(connectors.json()) == 12
    assert settings.json()["online_research_default"] is False
    assert len(agents.json()) == 9


def test_connector_test_uses_the_connector_implementation(client: TestClient) -> None:
    calls: list[tuple[str, str, int]] = []

    class FakeGateway:
        async def search(
            self,
            capability: Capability,
            source: str,
            query: str,
            *,
            limit: int,
        ) -> ConnectorSearchResponse:
            assert capability == Capability.GRANT
            calls.append((source, query, limit))
            return ConnectorSearchResponse(
                source=PublicConnectorSource(source),
                query=query,
                records=[{"id": "record-1"}],
                terms_url=HttpUrl("https://terms.example"),
                retrieved_from=HttpUrl("https://api.example"),
            )

        async def close(self) -> None:
            return None

    app.state.connector_gateway = FakeGateway()

    response = client.post("/api/connectors/grants_gov/test")

    assert response.status_code == 200
    assert response.json()["test_status"] == "ready"
    assert calls == [("grants_gov", "research reproducibility", 1)]


def test_connector_test_distinguishes_missing_gateway_configuration(
    client: TestClient,
) -> None:
    app.state.connector_gateway = DisabledConnectorGateway()

    response = client.post("/api/connectors/pubmed/test")

    assert response.status_code == 200
    assert response.json()["test_status"] == "configuration_required"


def test_runtime_upload_records_blob_checksum_and_ingestion_run(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/library/upload",
        data={
            "title": "Runtime protocol",
            "kind": "Policy",
            "access": "internal",
            "license": "Project supplied",
            "description": "A runtime source for governed extraction.",
        },
        files={
            "file": (
                "protocol.txt",
                b"Protocol version 1.0\n\nMethods and limitations are required.",
                "text/plain",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["blob_uri"].startswith("memory://")
    assert payload["item"]["checksum"].startswith("sha256:")
    assert payload["item"]["size_bytes"] > 0
    assert payload["item"]["status"] == "processing"
    assert payload["run"]["durable_instance_id"].startswith("research-run-ingest-")


def test_runtime_upload_rejects_unsupported_content_type(client: TestClient) -> None:
    response = client.post(
        "/api/library/upload",
        data={
            "title": "Executable source",
            "kind": "Policy",
            "access": "internal",
            "license": "Project supplied",
            "description": "Unsupported upload type.",
        },
        files={"file": ("source.exe", b"MZ", "application/octet-stream")},
    )

    assert response.status_code == 415


def test_metadata_ingestion_rejects_caller_supplied_storage_reference(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/library/ingest",
        json={
            "title": "Untrusted reference",
            "kind": "Policy",
            "source": "External",
            "access": "internal",
            "license": "Project supplied",
            "description": "Attempt to inject a storage URI.",
            "blob_uri": "https://attacker.example/source.pdf",
        },
    )

    assert response.status_code == 422


def test_studio_contract_rejects_caller_supplied_tenant(client: TestClient) -> None:
    response = client.post(
        "/api/studios/literature/run",
        json={
            "objective": "Compare reproducible evidence workflows",
            "tenant_id": "another-tenant",
        },
    )

    assert response.status_code == 422


def test_untrusted_principal_header_cannot_override_demo_identity(
    client: TestClient,
) -> None:
    forged = (
        "eyJ1c2VySWQiOiJhdHRhY2tlciIsInVzZXJEZXRhaWxzIjoiQXR0YWNrZXIi"
        "LCJjbGFpbXMiOlt7InR5cCI6InRpZCIsInZhbCI6Im90aGVyLXRlbmFudCJ9XX0="
    )
    response = client.post(
        "/api/research/literature",
        headers={"X-MS-CLIENT-PRINCIPAL": forged},
        json={
            "query": "Try to cross the tenant boundary",
            "tenant_id": "other-tenant",
        },
    )

    assert response.status_code == 403


def test_non_onboarded_tenant_cannot_read_operational_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS", "true")
    monkeypatch.setenv("RESEARCH_ALLOW_DEMO_IDENTITY", "false")
    with TestClient(app) as client:
        app.state.settings = Settings()
        response = client.get(
            "/api/workspace",
            headers={
                "X-MS-CLIENT-PRINCIPAL": _principal(
                    "other-tenant",
                    ["researchers"],
                )
            },
        )

    assert response.status_code == 403


def test_settings_and_approvals_require_authorized_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS", "true")
    monkeypatch.setenv("RESEARCH_ALLOW_DEMO_IDENTITY", "false")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["researchers"])}
    with TestClient(app) as client:
        app.state.settings = Settings()
        settings = client.get("/api/settings", headers=headers).json()
        settings_response = client.put(
            "/api/settings",
            headers=headers,
            json=settings,
        )
        approval_id = client.get("/api/approvals", headers=headers).json()[0]["id"]
        approval_response = client.post(
            f"/api/approvals/{approval_id}/decision",
            headers=headers,
            json={"decision": "approved", "rationale": "Unauthorized attempt"},
        )

    assert settings_response.status_code == 403
    assert approval_response.status_code == 403


@pytest.mark.parametrize(
    ("capability", "extra", "expected_status"),
    [
        ("literature", {}, 422),
        (
            "institutional_qa",
            {
                "inputs": {
                    "public_search_query": "public policy query",
                    "public_research_acknowledged": True,
                }
            },
            422,
        ),
        (
            "dataset",
            {
                "inputs": {
                    "public_search_query": "public dataset query",
                    "public_research_acknowledged": True,
                }
            },
            422,
        ),
        (
            "grant",
            {
                "inputs": {
                    "public_search_query": "public opportunity query",
                    "public_research_acknowledged": False,
                }
            },
            422,
        ),
    ],
)
def test_online_research_policy_is_enforced(
    client: TestClient,
    capability: str,
    extra: dict[str, object],
    expected_status: int,
) -> None:
    response = client.post(
        f"/api/studios/{capability}/run",
        json={
            "objective": "Research a current public question",
            "online_research": True,
            **extra,
        },
    )

    assert response.status_code == expected_status


def test_online_research_accepts_separate_acknowledged_public_query(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/studios/literature/run",
        json={
            "objective": "Internal synthesis objective remains in the product.",
            "online_research": True,
            "inputs": {
                "public_search_query": "current public reproducibility guidance",
                "public_research_acknowledged": True,
            },
        },
    )

    assert response.status_code == 200


def test_literature_protocol_filters_retrieval_and_real_candidate_count(
    client: TestClient,
) -> None:
    no_match = client.post(
        "/api/studios/literature/run",
        json={
            "objective": "Compare auditable evidence workflows.",
            "inputs": {
                "date_from": 2099,
                "date_to": 2100,
                "sources": ["PubMed"],
            },
        },
    )
    empty_sources = client.post(
        "/api/studios/literature/run",
        json={
            "objective": "Compare auditable evidence workflows.",
            "inputs": {
                "date_from": 2020,
                "date_to": 2026,
                "sources": [],
            },
        },
    )
    filtered = client.post(
        "/api/studios/literature/run",
        json={
            "objective": "Compare auditable evidence workflows.",
            "inputs": {
                "date_from": 2024,
                "date_to": 2025,
                "sources": ["PubMed"],
            },
        },
    )

    assert no_match.status_code == 200
    assert no_match.json()["candidate_count"] == 0
    assert no_match.json()["citations"] == []
    assert empty_sources.json()["candidate_count"] == 0
    assert filtered.json()["candidate_count"] == 2
    assert len(filtered.json()["screening"]) == 2


@pytest.mark.parametrize(
    ("capability", "expected_key"),
    [
        ("literature", "extraction_matrix"),
        ("grant", "requirements"),
        ("matching", "matches"),
        ("dataset", "analysis_plan"),
        ("institutional_qa", "versions"),
        ("orchestration", "steps"),
    ],
)
def test_each_studio_returns_its_own_typed_contract(
    client: TestClient,
    capability: str,
    expected_key: str,
) -> None:
    response = client.post(
        f"/api/studios/{capability}/run",
        json={"objective": "Build an evidence-governed research artifact"},
    )

    assert response.status_code == 200
    assert expected_key in response.json()
    assert response.json()["run"]["durable_instance_id"].startswith("research-run-")


def test_large_unresolved_dataset_returns_estimate_without_fixture_profile(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": "Estimate profiling for the governed archive.",
            "inputs": {
                "filename": "clinical-events-archive.parquet",
                "estimated_bytes": 1_200_000_000_000,
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["profile_status"] == "estimate_only"
    assert result["row_count"] == 0
    assert result["column_count"] == 0
    assert result["fields"] == []
    assert result["citations"] == []
    assert result["compute_proposal"]["approval_required"] is True


def test_inline_computed_dataset_does_not_default_to_scale_out(
    client: TestClient,
) -> None:
    app.state.approval_context_resolver = TrustedDatasetApprovalResolver()
    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": "Profile the supplied two-row dataset.",
            "inputs": {
                "filename": "inline.csv",
                "csv_text": "group,score\ncontrol,10\nintervention,12\n",
                "approval_reference": "approval-request-inline-1",
                "idempotency_key": "dataset-inline-1",
            },
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["profile_status"] == "computed"
    assert result["row_count"] == 2
    assert result["run"]["status"] == "completed"
    assert result["compute_proposal"]["estimated_bytes"] == 0
    assert result["compute_proposal"]["approval_required"] is False


def test_inline_dataset_analysis_is_unavailable_without_trusted_resolver(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": "Analyze the supplied dataset.",
            "inputs": {
                "filename": "inline.csv",
                "csv_text": "group,score\ncontrol,10\n",
            },
        },
    )

    assert response.status_code == 503
    assert "approval" in response.json()["detail"].lower()


def test_automation_graph_is_hashed_and_invalid_cycles_are_blocked(
    client: TestClient,
) -> None:
    valid = client.post(
        "/api/studios/orchestration/run",
        json={
            "objective": "Validate a minimal approval workflow.",
            "inputs": {
                "template_id": "minimal-v2",
                "trigger": "Manual",
                "steps": [
                    {
                        "id": "prepare",
                        "label": "Prepare artifact",
                        "kind": "activity",
                        "depends_on": [],
                        "retry_limit": 2,
                        "approval_required": False,
                    },
                    {
                        "id": "release",
                        "label": "Release artifact",
                        "kind": "external_action",
                        "depends_on": ["prepare"],
                        "retry_limit": 1,
                        "approval_required": True,
                    },
                ],
            },
        },
    )
    cyclic = client.post(
        "/api/studios/orchestration/run",
        json={
            "objective": "Reject a cyclic workflow.",
            "inputs": {
                "steps": [
                    {
                        "id": "first",
                        "label": "First",
                        "kind": "activity",
                        "depends_on": ["second"],
                        "retry_limit": 1,
                        "approval_required": False,
                    },
                    {
                        "id": "second",
                        "label": "Second",
                        "kind": "activity",
                        "depends_on": ["first"],
                        "retry_limit": 1,
                        "approval_required": False,
                    },
                ]
            },
        },
    )

    assert valid.status_code == 200
    assert len(valid.json()["graph_hash"]) == 64
    assert valid.json()["graph_version"] == "2.0"
    assert valid.json()["dry_run_status"] == "passed"
    assert cyclic.status_code == 200
    assert cyclic.json()["run"]["status"] == "blocked"
    assert cyclic.json()["dry_run_status"] == "blocked"
    assert any("cycle" in error.lower() for error in cyclic.json()["validation_errors"])


def test_matching_score_is_sum_of_evidence_components(client: TestClient) -> None:
    response = client.post(
        "/api/studios/matching/run",
        json={"objective": "Find genomics and reproducibility collaborators"},
    )

    assert response.status_code == 200
    matches = response.json()["matches"]
    assert matches
    assert [item["score"] for item in matches] == sorted(
        [item["score"] for item in matches],
        reverse=True,
    )
    for item in matches:
        expected = round(
            sum(component["contribution"] for component in item["components"]),
            1,
        )
        assert item["score"] == expected
        assert all(component["evidence_id"] for component in item["components"])


def test_approval_decision_records_actor_rationale_and_action(client: TestClient) -> None:
    pending = client.get("/api/approvals").json()[0]
    response = client.post(
        f"/api/approvals/{pending['id']}/decision",
        json={
            "decision": "approved",
            "rationale": "The exact destination and evidence boundary were reviewed.",
        },
    )

    assert response.status_code == 200
    decision = response.json()
    assert decision["approver_id"] == "demo-researcher"
    assert decision["approver_name"] == "Dr. Maya Chen"
    assert decision["decided_at"]
    assert decision["gated_action"]
    assert decision["idempotency_key"]
    assert decision["rationale"].startswith("The exact destination")


def test_settings_cannot_enable_global_online_research(client: TestClient) -> None:
    payload = client.get("/api/settings").json()
    payload["online_research_default"] = True

    response = client.put("/api/settings", json=payload)

    assert response.status_code == 422
    assert "opt-in per run" in response.json()["detail"]


def test_hosted_agent_text_is_never_promoted_to_verified_evidence() -> None:
    insight = validate_agent_insight(
        agent_name="literature-agent",
        content=("The evidence supports a bounded conclusion. source_id: paper-rag; source_id: invented-source"),
        allowed_source_ids={"paper-rag"},
        online_research_used=False,
    )

    assert insight.evidence_state == EvidenceState.MODEL_ANALYSIS
    assert insight.referenced_source_ids == ["paper-rag"]
    assert insight.unresolved_source_ids == ["invented-source"]
