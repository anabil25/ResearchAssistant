from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import HttpUrl
from research_assistant_api.app import _authorize_requested_sources, _raw_requested_sources, app
from research_assistant_api.config import Settings
from research_assistant_api.connector_gateway import DisabledConnectorGateway
from research_assistant_api.foundry import HostedAgentReply
from research_assistant_api.studios import validate_agent_insight
from research_assistant_api.workspace import WorkspaceStore
from research_assistant_api.studios import StudioService, validate_agent_insight
from research_assistant_core.connector_gateway import (
    ConnectorSearchResponse,
    PublicConnectorSource,
)
from research_assistant_core.models import Capability, ResearchRequest
from research_assistant_core.service import ResearchService
from research_assistant_core.studio_models import EvidenceState, StudioRunRequest
from research_assistant_api.app import app
from research_assistant_core.models import Capability
from research_assistant_core.studio_models import EvidenceState
from research_assistant_api.approval_context import (
    ApprovalContextRequest,
    ResolvedApprovalContext,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class _StubHostedGateway:
    """Records invocations and returns a fixed reply, so a hosted-mode studio
    run completes without contacting Foundry."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(
        self,
        message: str,
        *,
        agent_name: str | None = None,
        allow_tools: bool = True,
    ) -> HostedAgentReply:
        self.calls.append(message)
        return HostedAgentReply(
            agent_name=agent_name or "dataset-agent",
            content="Bounded analysis complete.",
            response_id="resp-stub-1",
        )


@pytest.fixture
def hosted_client() -> Iterator[TestClient]:
    """Hosted-mode client for the dataset approval-boundary tests.

    A dataset approval is consumed only on the path that actually sends to the
    hosted agent (consuming earlier would burn a reviewer-decided approval when
    nothing is sent). These tests assert the fail-closed denials that guard that
    send, so they must exercise hosted mode -- in mock mode there is no send and
    therefore nothing to authorize.
    """
    with TestClient(app) as test_client:
        app.state.hosted = _StubHostedGateway()
        app.state.settings = Settings(
            execution_mode="hosted",
            foundry_project_endpoint="https://foundry.example.test",
        )
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


def test_demo_identity_uses_the_configured_workspace_tenant() -> None:
    from research_assistant_api.identity import resolve_identity

    request = type("Request", (), {"headers": {}})()
    identity = resolve_identity(
        request,
        Settings(
            workspace_tenant_id="accelerator-tenant",
        ),
    )

    assert identity.tenant_id == "accelerator-tenant"


def test_local_developer_identity_is_confined_to_local_environments() -> None:
    """Least privilege, enforced by reachability rather than group shaping.

    The local developer identity is deliberately privileged -- it carries
    ``research-admins`` so an offline run exercises the same role-gated
    routes a real admin reaches, and ``research-admins`` is a member of
    Agent Studio's ``PLATFORM_OWNER_GROUPS``. What keeps that safe is that
    it is issued only when ``entra_auth_enforced`` is false; with a gateway
    enforced an unauthenticated caller gets nothing.
    """
    from fastapi import HTTPException
    from research_assistant_api.identity import LOCAL_DEVELOPMENT_SOURCE, resolve_identity

    request = type("Request", (), {"headers": {}})()
    assert resolve_identity(request, Settings()).source == LOCAL_DEVELOPMENT_SOURCE

    with pytest.raises(HTTPException) as unauthenticated:
        resolve_identity(request, Settings(entra_auth_enforced=True))
    assert unauthenticated.value.status_code == 401


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


def test_connector_test_uses_the_connector_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``POST /api/connectors/{id}/test`` requires ``research-admins`` (a
    pre-existing, non-Agent-Studio admin gate). The demo identity no longer
    carries that group by default (least privilege), so this must
    authenticate explicitly as an admin via the header-based identity path
    rather than relying on the ambient demo identity.
    """
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["research-admins"])}
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

    with TestClient(app) as test_client:
        app.state.settings = Settings()
        app.state.connector_gateway = FakeGateway()
        response = test_client.post("/api/connectors/grants_gov/test", headers=headers)

    assert response.status_code == 200
    assert response.json()["test_status"] == "ready"
    assert calls == [("grants_gov", "research reproducibility", 1)]


def test_connector_test_distinguishes_missing_gateway_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """See ``test_connector_test_uses_the_connector_implementation``: this
    route requires explicit ``research-admins`` authentication now that the
    demo identity no longer carries that group by default.
    """
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["research-admins"])}

    with TestClient(app) as test_client:
        app.state.settings = Settings()
        app.state.connector_gateway = DisabledConnectorGateway()
        response = test_client.post("/api/connectors/pubmed/test", headers=headers)

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
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
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
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
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
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()
    decision = client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )
    assert decision.status_code == 200
    assert decision.json()["state"] == "approved"

    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": objective,
            "inputs": {
                "filename": filename,
                "csv_text": csv_text,
                "approval_request_id": approval_request["id"],
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


def test_inline_dataset_analysis_requires_explicit_approval(
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

    assert response.status_code == 409
    assert "approval" in response.json()["detail"].lower()


def test_client_supplied_analysis_approved_flag_grants_no_authority(
    client: TestClient,
) -> None:
    """A caller that bypasses the UI (e.g. curl/Postman) and simply asserts
    its own ``analysis_approved: true`` -- with no durable, reviewer-decided
    approval request ever created -- must still be rejected. The field is
    inert: only a server-resolved, single-use consumption of a decided
    ``DatasetApprovalRequest`` can authorize dataset analysis.
    """
    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": "Analyze the supplied dataset.",
            "inputs": {
                "filename": "inline.csv",
                "csv_text": "group,score\ncontrol,10\n",
                "analysis_approved": True,
                "compute_adapter_configured": True,
            },
        },
    )

    assert response.status_code == 409
    assert "approval" in response.json()["detail"].lower()


def test_dataset_approval_request_cannot_be_replayed_for_a_different_csv(
    client: TestClient,
) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={
            "filename": filename,
            "objective": objective,
            "csv_text": "group,score\ncontrol,10\nintervention,12\n",
        },
    ).json()
    client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )

    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": objective,
            "inputs": {
                "filename": filename,
                "csv_text": "group,score\ncontrol,999\nintervention,999\n",
                "approval_request_id": approval_request["id"],
            },
        },
    )

    assert response.status_code == 409
    assert "does not match" in response.json()["detail"].lower()


def test_dataset_approval_request_is_single_use(hosted_client: TestClient) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = hosted_client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()
    hosted_client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )
    run_payload = {
        "objective": objective,
        "inputs": {
            "filename": filename,
            "csv_text": csv_text,
            "approval_request_id": approval_request["id"],
        },
    }

    first = hosted_client.post("/api/studios/dataset/run", json=run_payload)
    second = hosted_client.post("/api/studios/dataset/run", json=run_payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already been consumed" in second.json()["detail"].lower()


def test_dataset_approval_request_rejected_cannot_authorize_analysis(
    client: TestClient,
) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()
    decision = client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "rejected", "rationale": "Contains disallowed identifiers."},
    )
    assert decision.json()["state"] == "rejected"

    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": objective,
            "inputs": {
                "filename": filename,
                "csv_text": csv_text,
                "approval_request_id": approval_request["id"],
            },
        },
    )

    assert response.status_code == 409
    assert "rejected" in response.json()["detail"].lower()


def test_dataset_approval_request_forged_id_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": "Analyze the supplied dataset.",
            "inputs": {
                "filename": "inline.csv",
                "csv_text": "group,score\ncontrol,10\n",
                "approval_request_id": "dsapproval-forgedforgedforged",
            },
        },
    )

    assert response.status_code == 409
    assert "not found" in response.json()["detail"].lower()


def test_dataset_approval_request_still_pending_cannot_authorize_analysis(
    client: TestClient,
) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()

    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": objective,
            "inputs": {
                "filename": filename,
                "csv_text": csv_text,
                "approval_request_id": approval_request["id"],
            },
        },
    )

    assert response.status_code == 409
    assert "has not been decided" in response.json()["detail"].lower()


def test_dataset_approval_request_expiry_fails_closed(client: TestClient) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()
    client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )
    store = app.state.workspace
    with store._lock:
        record = next(
            item for item in store._dataset_approvals if item.id == approval_request["id"]
        )
        record.expires_at = record.requested_at

    response = client.post(
        "/api/studios/dataset/run",
        json={
            "objective": objective,
            "inputs": {
                "filename": filename,
                "csv_text": csv_text,
                "approval_request_id": approval_request["id"],
            },
        },
    )

    assert response.status_code == 409
    assert "expired" in response.json()["detail"].lower()
@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"date_from": "recent"}, "must be integers"),
        ({"date_from": 2026, "date_to": 2020}, "window is invalid"),
        ({"sources": "PubMed"}, "must be a list of provider names"),
    ],
)
def test_literature_studio_validates_year_and_source_inputs(
    inputs: dict[str, object],
    message: str,
) -> None:
    generic = ResearchService().run(
        Capability.LITERATURE,
        ResearchRequest(query="Review auditable evidence workflows"),
    )

    with pytest.raises(ValueError, match=message):
        StudioService._literature(
            generic,
            StudioRunRequest(
                objective="Review auditable evidence workflows",
                inputs=inputs,
            ),
            owner="Dr. Maya Chen",
            insight=None,
        )


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


def test_automation_graph_collects_contract_validation_errors(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/studios/orchestration/run",
        json={
            "objective": "Reject a malformed activation graph.",
            "inputs": {
                "trigger": "Webhook",
                "steps": [
                    {
                        "id": "review",
                        "label": "Review",
                        "kind": "approval",
                        "depends_on": [],
                        "retry_limit": 0,
                        "approval_required": True,
                    },
                    {
                        "id": "review",
                        "label": "Review duplicate",
                        "kind": "approval",
                        "depends_on": [],
                        "retry_limit": 0,
                        "approval_required": True,
                    },
                    {
                        "id": "ship",
                        "label": "Ship externally",
                        "kind": "external_action",
                        "depends_on": ["ship"],
                        "retry_limit": 1,
                        "approval_required": False,
                    },
                ],
            },
        },
    )

    assert response.status_code == 200
    errors = response.json()["validation_errors"]
    assert response.json()["run"]["status"] == "blocked"
    assert "Workflow step IDs must be unique" in errors
    assert "Unsupported workflow trigger: Webhook" in errors
    assert "V2 automation graphs support one exact activation gate" in errors
    assert "ship cannot depend on itself" in errors
    assert "ship external actions require an approval ancestor" in errors


def test_automation_graph_requires_at_least_one_step(client: TestClient) -> None:
    response = client.post(
        "/api/studios/orchestration/run",
        json={
            "objective": "Reject an empty graph.",
            "inputs": {"steps": []},
        },
    )

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "blocked"
    assert "Workflow graph must contain at least one step" in response.json()[
        "validation_errors"
    ]


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


def test_institutional_studio_surfaces_conflict_fixture(client: TestClient) -> None:
    response = client.post(
        "/api/studios/institutional_qa/run",
        json={
            "objective": "Summarize the institutional policy boundary.",
            "inputs": {"include_conflict_fixture": True},
        },
    )

    assert response.status_code == 200
    assert response.json()["conflicts"][0]["topic"] == "AI disclosure threshold"


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
    assert decision["approver_id"] == "local-developer"
    assert decision["approver_name"] == "Dr. Maya Chen"
    assert decision["decided_at"]
    assert decision["gated_action"]
    assert decision["idempotency_key"]
    assert decision["rationale"].startswith("The exact destination")


def test_settings_cannot_enable_global_online_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PUT /api/settings`` requires ``research-admins``; authenticate
    explicitly since the demo identity no longer carries that group.
    """
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["research-admins"])}

    with TestClient(app) as test_client:
        app.state.settings = Settings()
        payload = test_client.get("/api/settings", headers=headers).json()
        payload["online_research_default"] = True

        response = test_client.put("/api/settings", headers=headers, json=payload)

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


def test_raw_requested_sources_merges_sources_and_funding_sources_for_grant() -> None:
    """The grant studio UI sends connector IDs under ``funding_sources``;
    it must be unioned with ``sources`` (de-duplicated, order-preserving) so
    that field finally has real server-side effect."""
    merged = _raw_requested_sources(
        Capability.GRANT,
        {
            "sources": ["grants_gov", "nih_reporter"],
            "funding_sources": ["nih_reporter", "crossref"],
        },
    )

    assert merged == ["grants_gov", "nih_reporter", "crossref"]


def test_raw_requested_sources_ignores_funding_sources_for_non_grant_capabilities() -> None:
    assert _raw_requested_sources(Capability.LITERATURE, {"funding_sources": ["pubmed"]}) is None


def test_raw_requested_sources_returns_none_when_no_applicable_key_present() -> None:
    assert _raw_requested_sources(Capability.GRANT, {}) is None
    assert _raw_requested_sources(Capability.GRANT, {"sources": "not-a-list"}) is None


def test_raw_requested_sources_reads_sources_alone_when_funding_sources_absent() -> None:
    assert _raw_requested_sources(Capability.GRANT, {"sources": ["grants_gov"]}) == ["grants_gov"]


def test_authorize_requested_sources_raises_403_and_audits_disabled_connector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import HTTPException

    connectors = WorkspaceStore().connectors()
    for connector in connectors:
        if connector.id == "pubmed":
            connector.enabled = False

    with caplog.at_level("WARNING"), pytest.raises(HTTPException) as excinfo:
        _authorize_requested_sources(
            Capability.LITERATURE,
            {"sources": ["pubmed"]},
            connectors,
            tenant_id="tenant-1",
            project_id="project-1",
        )

    assert excinfo.value.status_code == 403
    assert cast(dict[str, Any], excinfo.value.detail)["violations"][0]["reason"] == "disabled"
    assert any("Rejected unauthorized connector source request" in record.message for record in caplog.records)


def test_authorize_requested_sources_raises_422_for_unknown_connector() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _authorize_requested_sources(
            Capability.LITERATURE,
            {"sources": ["not-a-real-connector"]},
            WorkspaceStore().connectors(),
            tenant_id="tenant-1",
            project_id="project-1",
        )

    assert excinfo.value.status_code == 422
    assert cast(dict[str, Any], excinfo.value.detail)["violations"][0]["reason"] == "unknown"


def test_authorize_requested_sources_returns_resolved_list_when_authorized() -> None:
    resolved = _authorize_requested_sources(
        Capability.LITERATURE,
        {"sources": ["PubMed"]},
        WorkspaceStore().connectors(),
        tenant_id="tenant-1",
        project_id="project-1",
    )

    assert resolved == ["pubmed"]


def test_authorize_requested_sources_passes_through_none_when_absent() -> None:
    resolved = _authorize_requested_sources(
        Capability.LITERATURE,
        {},
        WorkspaceStore().connectors(),
        tenant_id="tenant-1",
        project_id="project-1",
    )

    assert resolved is None


def test_run_studio_rejects_unauthorized_connector_source_end_to_end() -> None:
    """Confirms the resolver is actually wired into the live
    ``/api/studios/{capability}/run`` online-research path (not just unit
    tested in isolation): a crafted unknown connector id must be rejected
    with a structured 422 before any hosted/connector call is attempted.
    """
    with TestClient(app) as test_client:
        app.state.settings = Settings(execution_mode="hosted")
        response = test_client.post(
            "/api/studios/literature/run",
            json={
                "objective": "Research a current public question",
                "online_research": True,
                "inputs": {
                    "public_search_query": "current public reproducibility guidance",
                    "public_research_acknowledged": True,
                    "sources": ["not-a-real-connector"],
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["violations"][0]["reason"] == "unknown"


def test_run_studio_rejects_disabled_connector_source_as_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector the client is not permitted to use (disabled for this
    project) is a 403, not a 422 -- and it is rejected even though the
    caller only ever saw an already-filtered connector list in the UI.
    """
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
    admin_headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["research-admins"])}

    with TestClient(app) as test_client:
        app.state.settings = Settings()
        arxiv = next(item for item in test_client.get("/api/connectors").json() if item["id"] == "arxiv")
        disable = test_client.put(
            "/api/connectors/arxiv",
            headers=admin_headers,
            json={"enabled": False, "assigned_agents": arxiv["assigned_agents"]},
        )
        assert disable.status_code == 200

        app.state.settings = Settings(execution_mode="hosted")
        response = test_client.post(
            "/api/studios/literature/run",
            json={
                "objective": "Research a current public question",
                "online_research": True,
                "inputs": {
                    "public_search_query": "current public reproducibility guidance",
                    "public_research_acknowledged": True,
                    "sources": ["arxiv"],
                },
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["violations"][0]["reason"] == "disabled"


def test_run_studio_grant_merges_funding_sources_into_authorization_check() -> None:
    """The grant studio UI's connector-id picker sends ``funding_sources``;
    this must now actually gate the live connector selection, not be
    silently ignored server-side."""
    with TestClient(app) as test_client:
        app.state.settings = Settings(execution_mode="hosted")
        response = test_client.post(
            "/api/studios/grant/run",
            json={
                "objective": "Research a current public funding question",
                "online_research": True,
                "inputs": {
                    "public_search_query": "current public grant opportunity guidance",
                    "public_research_acknowledged": True,
                    "funding_sources": ["not-a-real-connector"],
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["violations"][0]["requested"] == "not-a-real-connector"


def test_decide_dataset_approval_request_returns_none_when_missing() -> None:
    from research_assistant_api.identity import IdentityContext
    from research_assistant_api.workspace import DatasetApprovalDecisionRequest

    store = WorkspaceStore()

    result = store.decide_dataset_approval_request(
        "dsapproval-does-not-exist",
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        IdentityContext(
            user_id="reviewer-1",
            display_name="Reviewer One",
            tenant_id="demo",
            groups=("grant-reviewers",),
            source="test",
        ),
    )

    assert result is None


def test_decide_dataset_approval_request_rejects_a_second_decision() -> None:
    from research_assistant_api.identity import IdentityContext
    from research_assistant_api.workspace import DatasetApprovalDecisionRequest

    store = WorkspaceStore()
    identity = IdentityContext(
        user_id="reviewer-1",
        display_name="Reviewer One",
        tenant_id="demo",
        groups=("grant-reviewers",),
        source="test",
    )
    created = store.create_dataset_approval_request(
        plan_fingerprint="fp-abc",
        filename="inline.csv",
        objective="Profile the supplied dataset.",
        requested_by="Researcher One",
        ttl_minutes=60,
        requested_by_principal_id="researcher-1",
    )
    store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed the bounded fixture."),
        identity,
    )

    with pytest.raises(ValueError, match="already been decided"):
        store.decide_dataset_approval_request(
            created.id,
            DatasetApprovalDecisionRequest(decision="rejected", rationale="Changed my mind."),
            identity,
        )


def test_dataset_approval_decision_route_rejects_deciding_twice(client: TestClient) -> None:
    objective = "Profile the supplied two-row dataset."
    filename = "inline.csv"
    csv_text = "group,score\ncontrol,10\nintervention,12\n"
    approval_request = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv_text},
    ).json()
    first = client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/studios/dataset/approval-requests/{approval_request['id']}/decision",
        json={"decision": "rejected", "rationale": "Changed my mind."},
    )

    assert second.status_code == 409
    assert "already been decided" in second.json()["detail"].lower()


def test_dataset_approval_decision_route_404s_for_unknown_request(client: TestClient) -> None:
    response = client.post(
        "/api/studios/dataset/approval-requests/dsapproval-does-not-exist/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )

    assert response.status_code == 404


def test_dataset_approval_decision_route_requires_reviewer_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_ENTRA_AUTH_ENFORCED", "true")
    headers = {"X-MS-CLIENT-PRINCIPAL": _principal("demo", ["researchers"])}
    with TestClient(app) as test_client:
        app.state.settings = Settings()
        created = test_client.post(
            "/api/studios/dataset/approval-requests",
            headers=headers,
            json={
                "filename": "inline.csv",
                "objective": "Profile the supplied dataset.",
                "csv_text": "group,score\ncontrol,10\n",
            },
        ).json()
        response = test_client.post(
            f"/api/studios/dataset/approval-requests/{created['id']}/decision",
            headers=headers,
            json={"decision": "approved", "rationale": "Unauthorized attempt"},
        )

    assert response.status_code == 403


def test_dataset_approval_requests_list_route_returns_created_requests(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/studios/dataset/approval-requests",
        json={
            "filename": "inline.csv",
            "objective": "Profile the supplied dataset.",
            "csv_text": "group,score\ncontrol,10\n",
        },
    ).json()

    listed = client.get("/api/studios/dataset/approval-requests").json()

    assert any(item["id"] == created["id"] for item in listed)


def test_dataset_approval_decision_route_404s_when_record_vanishes_between_check_and_decide(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive branch: the route first checks the request exists, then
    calls ``decide_dataset_approval_request``; if the record vanishes in
    the (vanishingly small) window between those two calls, the route
    must still respond 404 rather than crash or leak an internal
    ``None``."""
    created = client.post(
        "/api/studios/dataset/approval-requests",
        json={
            "filename": "inline.csv",
            "objective": "Profile the supplied dataset.",
            "csv_text": "group,score\ncontrol,10\n",
        },
    ).json()
    store = app.state.workspace
    monkeypatch.setattr(store, "decide_dataset_approval_request", lambda *args, **kwargs: None)

    response = client.post(
        f"/api/studios/dataset/approval-requests/{created['id']}/decision",
        json={"decision": "approved", "rationale": "Reviewed the bounded fixture."},
    )

    assert response.status_code == 404


def test_hosted_dataset_rejects_before_agent_invocation(
    client: TestClient,
) -> None:
    original_settings = app.state.settings
    original_hosted = app.state.hosted
    invocations: list[str] = []

    class FailIfInvoked:
        def invoke(self, message: str, **_kwargs: object) -> None:
            invocations.append(message)

    app.state.settings = Settings(execution_mode="hosted")
    app.state.hosted = FailIfInvoked()
    try:
        response = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "Analyze the supplied dataset.",
                "inputs": {
                    "filename": "inline.csv",
                    "csv_text": "group,score\ncontrol,10\n",
                    "data_classification": "public_or_synthetic",
                },
            },
        )
    finally:
        app.state.settings = original_settings
        app.state.hosted = original_hosted

    assert response.status_code == 422
    assert invocations == []


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
