"""Dataset Studio / Research central approval-boundary remediation tests.

These prove the absolute contract: no ``csv_text`` bytes (or any Hosted
Foundry / Code Interpreter invocation) may reach the hosted gateway before an
exact-bound, durable dataset approval has been atomically consumed -- across
``/api/studios/dataset/run``, ``/api/research/dataset``, and any other
``_agent_message``/gateway caller. A valid approval must permit exactly one
invocation.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from research_assistant_api.app import _agent_message, app
from research_assistant_api.config import Settings
from research_assistant_api.foundry import HostedAgentReply
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    DatasetApprovalDecisionRequest,
    DatasetApprovalDenialReason,
    DatasetApprovalError,
    DatasetApprovalRequestCreate,
    WorkspaceStore,
    compute_dataset_plan_fingerprint,
    utc_now,
)
from research_assistant_core.models import Capability, ResearchResult
from research_assistant_core.studio_models import StudioRunRequest

PHI_CSV = "patient_id,ssn,dx\n1,999-99-9999,SECRET-CONDITION\n"


class SpyGateway:
    """Records every hosted-agent invocation instead of calling Foundry."""

    def __init__(self) -> None:
        self.captured: list[dict[str, Any]] = []

    def invoke(
        self,
        message: str,
        *,
        agent_name: str | None = None,
        allow_tools: bool = True,
    ) -> HostedAgentReply:
        self.captured.append(
            {"message": message, "agent_name": agent_name, "allow_tools": allow_tools}
        )
        return HostedAgentReply(
            agent_name=agent_name or "dataset-agent",
            content="Bounded analysis complete.",
            response_id="resp-spy-1",
        )

    def csv_bytes_sent(self) -> bool:
        return any("999-99-9999" in call["message"] and "SECRET-CONDITION" in call["message"] for call in self.captured)


def principal(tenant_id: str, groups: list[str], *, user_id: str = "user-1", name: str = "User One") -> str:
    payload = {
        "userId": user_id,
        "userDetails": name,
        "claims": [
            {"typ": "tid", "val": tenant_id},
            *({"typ": "groups", "val": group} for group in groups),
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


@contextmanager
def hosted_spy_client() -> Iterator[tuple[TestClient, SpyGateway, WorkspaceStore]]:
    """A TestClient in hosted execution mode with a spy gateway, a fresh
    workspace store, and platform-identity headers trusted (development is a
    safe environment so no Entra-enforcement self-report is required)."""
    with TestClient(app, raise_server_exceptions=False) as client:
        spy = SpyGateway()
        store = WorkspaceStore()
        app.state.hosted = spy
        app.state.workspace = store
        app.state.settings = Settings(
            execution_mode="hosted",
            trust_platform_identity_headers=True,
            foundry_project_endpoint="https://foundry.example.test",
        )
        yield client, spy, store


def _headers(store: WorkspaceStore, groups: list[str], **kw: Any) -> dict[str, str]:
    return {"X-MS-CLIENT-PRINCIPAL": principal(store.tenant_id, groups, **kw)}


def test_research_dataset_sends_no_bytes_without_an_approval() -> None:
    """Reproduction of the HIGH bypass: ``/api/research/dataset`` must not send
    any csv_text to the hosted gateway when no approval was consumed."""
    with hosted_spy_client() as (client, spy, store):
        response = client.post(
            "/api/research/dataset",
            json={
                "query": "Analyze this dataset",
                "tenant_id": store.tenant_id,
                "project_id": store.project_id,
                "context": {"csv_text": PHI_CSV, "filename": "phi.csv", "online_research": False},
            },
            headers=_headers(store, ["researchers"]),
        )

    assert spy.captured == [], "csv_text reached the hosted gateway with no approval consumed"
    assert not spy.csv_bytes_sent()
    assert response.status_code == 409
    assert len(store.dataset_approval_requests()) == 0


def _create_approval(
    client: TestClient,
    store: WorkspaceStore,
    *,
    objective: str,
    filename: str,
    csv: str,
    user_id: str = "requester-1",
) -> dict[str, Any]:
    response = client.post(
        "/api/studios/dataset/approval-requests",
        json={"filename": filename, "objective": objective, "csv_text": csv},
        headers=_headers(store, ["researchers"], user_id=user_id, name="Requester"),
    )
    assert response.status_code == 201, response.text
    approval: dict[str, Any] = response.json()
    return approval


def _decide(
    client: TestClient,
    store: WorkspaceStore,
    approval_id: str,
    decision: str,
    *,
    user_id: str = "reviewer-1",
    rationale: str = "Reviewed the bounded fixture.",
) -> Any:
    return client.post(
        f"/api/studios/dataset/approval-requests/{approval_id}/decision",
        json={"decision": decision, "rationale": rationale},
        headers=_headers(store, ["research-reviewers"], user_id=user_id, name="Reviewer"),
    )


def test_research_dataset_sends_exactly_one_invocation_after_valid_approval() -> None:
    objective = "Analyze this dataset"
    filename = "phi.csv"
    with hosted_spy_client() as (client, spy, store):
        approval = _create_approval(client, store, objective=objective, filename=filename, csv=PHI_CSV, user_id="req-1")
        decided = _decide(client, store, approval["id"], "approved", user_id="rev-1")
        assert decided.status_code == 200, decided.text

        body = {
            "query": objective,
            "tenant_id": store.tenant_id,
            "project_id": store.project_id,
            "context": {
                "csv_text": PHI_CSV,
                "filename": filename,
                "online_research": False,
                "approval_request_id": approval["id"],
            },
        }
        headers = _headers(store, ["researchers"], user_id="req-1")
        first = client.post("/api/research/dataset", json=body, headers=headers)
        second = client.post("/api/research/dataset", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert len(spy.captured) == 1
    assert spy.csv_bytes_sent()
    # Replay of the consumed approval fails closed with no second invocation.
    assert second.status_code == 409
    assert second.headers.get("X-Dataset-Approval-Denial") == DatasetApprovalDenialReason.ALREADY_CONSUMED.value
    assert len(spy.captured) == 1


def test_studios_dataset_sends_no_bytes_without_approval_then_one_after() -> None:
    objective = "Profile the supplied dataset."
    filename = "inline.csv"
    csv = "group,score\ncontrol,10\nintervention,12\n"
    with hosted_spy_client() as (client, spy, store):
        unapproved = client.post(
            "/api/studios/dataset/run",
            json={"objective": objective, "inputs": {"filename": filename, "csv_text": csv}},
            headers=_headers(store, ["researchers"], user_id="req-2"),
        )
        assert unapproved.status_code == 409
        assert spy.captured == []

        approval = _create_approval(client, store, objective=objective, filename=filename, csv=csv, user_id="req-2")
        _decide(client, store, approval["id"], "approved", user_id="rev-2")
        approved_run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": objective,
                "inputs": {"filename": filename, "csv_text": csv, "approval_request_id": approval["id"]},
            },
            headers=_headers(store, ["researchers"], user_id="req-2"),
        )

    assert approved_run.status_code == 200, approved_run.text
    assert len(spy.captured) == 1
    assert csv in spy.captured[0]["message"]


def test_research_dataset_missing_approval_reference_is_rejected_with_denial_header() -> None:
    with hosted_spy_client() as (client, spy, store):
        response = client.post(
            "/api/research/dataset",
            json={
                "query": "Analyze this dataset",
                "tenant_id": store.tenant_id,
                "project_id": store.project_id,
                "context": {"csv_text": PHI_CSV, "filename": "phi.csv", "online_research": False},
            },
            headers=_headers(store, ["researchers"]),
        )
    assert response.status_code == 409
    denial = response.headers.get("X-Dataset-Approval-Denial")
    assert denial == DatasetApprovalDenialReason.MISSING_APPROVAL_REFERENCE.value
    assert spy.captured == []


def test_client_supplied_approval_flags_grant_no_authority_on_research_route() -> None:
    """The legacy ``analysis_approved``/``compute_adapter_configured`` booleans
    are inert: asserting them without a consumed approval sends nothing."""
    with hosted_spy_client() as (client, spy, store):
        response = client.post(
            "/api/research/dataset",
            json={
                "query": "Analyze this dataset",
                "tenant_id": store.tenant_id,
                "project_id": store.project_id,
                "context": {
                    "csv_text": PHI_CSV,
                    "filename": "phi.csv",
                    "online_research": False,
                    "analysis_approved": True,
                    "compute_adapter_configured": True,
                },
            },
            headers=_headers(store, ["researchers"]),
        )
    assert response.status_code == 409
    assert spy.captured == []


def test_research_dataset_project_mismatch_is_forbidden_without_send() -> None:
    with hosted_spy_client() as (client, spy, store):
        response = client.post(
            "/api/research/dataset",
            json={
                "query": "Analyze this dataset",
                "tenant_id": store.tenant_id,
                "project_id": "someone-elses-project",
                "context": {"csv_text": PHI_CSV, "filename": "phi.csv", "online_research": False},
            },
            headers=_headers(store, ["researchers"]),
        )
    assert response.status_code == 403
    assert spy.captured == []


def test_untrusted_tenant_cannot_reach_dataset_send() -> None:
    with hosted_spy_client() as (client, spy, store):
        response = client.post(
            "/api/research/dataset",
            json={
                "query": "Analyze this dataset",
                "tenant_id": "intruder-tenant",
                "project_id": store.project_id,
                "context": {"csv_text": PHI_CSV, "filename": "phi.csv", "online_research": False},
            },
            headers={"X-MS-CLIENT-PRINCIPAL": principal("intruder-tenant", ["researchers"], user_id="intruder-1")},
        )
    assert response.status_code == 403
    assert spy.captured == []


def test_separation_of_duties_blocks_self_approval_for_real_identity() -> None:
    with hosted_spy_client() as (client, _spy, store):
        approval = _create_approval(
            client, store, objective="o", filename="f.csv", csv="a,b\n1,2\n", user_id="dual-1"
        )
        # Same principal id tries to approve its own request.
        self_decision = client.post(
            f"/api/studios/dataset/approval-requests/{approval['id']}/decision",
            json={"decision": "approved", "rationale": "Approving my own request."},
            headers=_headers(store, ["research-reviewers"], user_id="dual-1", name="Requester"),
        )
    assert self_decision.status_code == 403
    denial = self_decision.headers.get("X-Dataset-Approval-Denial")
    assert denial == DatasetApprovalDenialReason.SEPARATION_OF_DUTIES.value


def test_separation_of_duties_allows_a_distinct_reviewer() -> None:
    with hosted_spy_client() as (client, _spy, store):
        approval = _create_approval(
            client, store, objective="o", filename="f.csv", csv="a,b\n1,2\n", user_id="req-3"
        )
        decided = _decide(client, store, approval["id"], "approved", user_id="rev-3")
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "approved"


# --------------------------------------------------------------------------- #
# Structural backstop + deterministic fingerprint (unit level)
# --------------------------------------------------------------------------- #


def _dataset_studio_request(csv: str) -> StudioRunRequest:
    return StudioRunRequest(objective="Analyze", inputs={"filename": "d.csv", "csv_text": csv})


def _dataset_result() -> ResearchResult:
    from research_assistant_api.search_repository import build_research_service
    from research_assistant_core.models import ResearchRequest

    service = build_research_service(Settings(execution_mode="mock"))
    return service.run(
        Capability.DATASET,
        ResearchRequest(query="Analyze", project_id="demo-project", context={"csv_text": "a,b\n1,2\n"}),
    )


def test_agent_message_backstop_refuses_dataset_csv_without_a_grant() -> None:
    payload = _dataset_studio_request("secret,val\n1,PHI\n")
    with pytest.raises(RuntimeError, match="without a consumed dataset approval grant"):
        _agent_message(Capability.DATASET, payload, _dataset_result(), dataset_grant=None)


def test_agent_message_backstop_refuses_a_grant_for_a_different_plan() -> None:
    """A grant bound to one CSV cannot smuggle a different CSV to Foundry."""
    from research_assistant_api.app import DatasetSendGrant

    payload = _dataset_studio_request("secret,val\n1,PHI\n")
    mismatched = DatasetSendGrant(
        approval_request_id="dsapproval-x",
        project_id="demo-project",
        plan_fingerprint="fingerprint-for-a-different-csv",
        invocation_id="inv-x",
    )
    with pytest.raises(RuntimeError, match="does not match the dataset plan being sent"):
        _agent_message(Capability.DATASET, payload, _dataset_result(), dataset_grant=mismatched)


def test_fingerprint_is_collision_resistant_across_field_boundaries() -> None:
    # Under the old ``"\u241f".join(...)`` encoding these two distinct plans
    # collided (the separator moved across the objective/filename boundary).
    left = compute_dataset_plan_fingerprint(
        project_id="p", objective="a\u241fb", filename="c", csv_text="q"
    )
    right = compute_dataset_plan_fingerprint(
        project_id="p", objective="a", filename="b\u241fc", csv_text="q"
    )
    assert left != right


def test_fingerprint_is_deterministic() -> None:
    kwargs = {"project_id": "p", "objective": "obj", "filename": "f.csv", "csv_text": "a,b\n1,2\n"}
    assert compute_dataset_plan_fingerprint(**kwargs) == compute_dataset_plan_fingerprint(**kwargs)


# --------------------------------------------------------------------------- #
# Store-level: SOD, idempotency, races, expiry, audit outbox/recovery
# --------------------------------------------------------------------------- #


def _identity(user_id: str, groups: tuple[str, ...], *, source: str = "container-apps-auth") -> IdentityContext:
    return IdentityContext(
        user_id=user_id,
        display_name=user_id.title(),
        tenant_id="demo",
        groups=groups,
        source=source,
    )


def _seed_decided(store: WorkspaceStore, *, csv: str = "a,b\n1,2\n") -> tuple[str, str]:
    fingerprint = compute_dataset_plan_fingerprint(
        project_id=store.project_id, objective="obj", filename="f.csv", csv_text=csv
    )
    record = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename="f.csv",
        objective="obj",
        requested_by="Requester",
        ttl_minutes=60,
        requested_by_principal_id="req-9",
    )
    store.decide_dataset_approval_request(
        record.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    return record.id, fingerprint


def test_store_separation_of_duties_is_enforced_and_demo_exempt() -> None:
    store = WorkspaceStore()
    record = store.create_dataset_approval_request(
        plan_fingerprint="fp",
        filename="f.csv",
        objective="obj",
        requested_by="Requester",
        ttl_minutes=60,
        requested_by_principal_id="same-1",
    )
    with pytest.raises(DatasetApprovalError) as excinfo:
        store.decide_dataset_approval_request(
            record.id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Self approval."),
            _identity("same-1", ("research-reviewers",)),
        )
    assert excinfo.value.reason == DatasetApprovalDenialReason.SEPARATION_OF_DUTIES

    # Deterministic policy: the demo-sandbox identity is exempt (local/dev only).
    demo = store.create_dataset_approval_request(
        plan_fingerprint="fp2",
        filename="f.csv",
        objective="obj",
        requested_by="Demo",
        ttl_minutes=60,
        requested_by_principal_id="demo-researcher",
    )
    decided = store.decide_dataset_approval_request(
        demo.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Demo self approval."),
        _identity("demo-researcher", ("research-reviewers",), source="demo-sandbox"),
    )
    assert decided is not None
    assert decided.state.value == "approved"


def test_same_decision_retry_is_idempotent_and_conflicts_fail() -> None:
    store = WorkspaceStore()
    record = store.create_dataset_approval_request(
        plan_fingerprint="fp",
        filename="f.csv",
        objective="obj",
        requested_by="Requester",
        ttl_minutes=60,
        requested_by_principal_id="req-9",
    )
    reviewer = _identity("rev-9", ("research-reviewers",))
    first = store.decide_dataset_approval_request(
        record.id, DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."), reviewer
    )
    again = store.decide_dataset_approval_request(
        record.id, DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed twice."), reviewer
    )
    assert first is not None and again is not None
    assert first.state.value == again.state.value == "approved"

    with pytest.raises(DatasetApprovalError) as excinfo:
        store.decide_dataset_approval_request(
            record.id, DatasetApprovalDecisionRequest(decision="rejected", rationale="Changed mind."), reviewer
        )
    assert excinfo.value.reason == DatasetApprovalDenialReason.ALREADY_DECIDED


def test_pending_rejected_expired_and_replay_fail_closed_with_reasons() -> None:
    store = WorkspaceStore()
    fingerprint = compute_dataset_plan_fingerprint(
        project_id=store.project_id, objective="obj", filename="f.csv", csv_text="a,b\n1,2\n"
    )

    pending = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj", requested_by="R", ttl_minutes=60
    )
    with pytest.raises(DatasetApprovalError) as pending_exc:
        store.consume_dataset_approval_request(pending.id, plan_fingerprint=fingerprint, invocation_id="i1")
    assert pending_exc.value.reason == DatasetApprovalDenialReason.PENDING

    with pytest.raises(DatasetApprovalError) as mismatch_exc:
        store.consume_dataset_approval_request(pending.id, plan_fingerprint="different-fp", invocation_id="i2")
    assert mismatch_exc.value.reason == DatasetApprovalDenialReason.FINGERPRINT_MISMATCH

    rejected = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj", requested_by="R", ttl_minutes=60
    )
    store.decide_dataset_approval_request(
        rejected.id,
        DatasetApprovalDecisionRequest(decision="rejected", rationale="Contains identifiers."),
        _identity("rev-9", ("research-reviewers",)),
    )
    with pytest.raises(DatasetApprovalError) as rejected_exc:
        store.consume_dataset_approval_request(rejected.id, plan_fingerprint=fingerprint, invocation_id="i3")
    assert rejected_exc.value.reason == DatasetApprovalDenialReason.REJECTED

    approved_id, _ = _seed_decided(store)
    # Force expiry on the durable record and confirm it fails closed.
    store._dataset_approvals[-1].expires_at = utc_now() - timedelta(minutes=1)
    with pytest.raises(DatasetApprovalError) as expired_exc:
        store.consume_dataset_approval_request(approved_id, plan_fingerprint=fingerprint, invocation_id="i4")
    assert expired_exc.value.reason == DatasetApprovalDenialReason.EXPIRED


def test_concurrent_consume_authorizes_exactly_one_invocation() -> None:
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    barrier = threading.Barrier(8)
    successes: list[str] = []
    denials: list[DatasetApprovalDenialReason] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            record = store.consume_dataset_approval_request(
                approval_id, plan_fingerprint=fingerprint, invocation_id=f"inv-{index}"
            )
        except DatasetApprovalError as exc:
            with lock:
                denials.append(exc.reason)
        else:
            with lock:
                successes.append(str(record.consumed_invocation_id))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(denials) == 7
    assert all(reason == DatasetApprovalDenialReason.ALREADY_CONSUMED for reason in denials)


def test_dataset_audit_intents_are_durable_atomic_and_recoverable() -> None:
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    # The decision emitted a durable audit intent atomically with the mutation.
    after_decide = store.dataset_approval_audit()
    assert [entry.action for entry in after_decide] == ["decided"]
    assert after_decide[0].delivery == "pending"

    store.consume_dataset_approval_request(
        approval_id, plan_fingerprint=fingerprint, invocation_id="inv-audit", consumed_by_principal_id="req-9"
    )
    actions = sorted(entry.action for entry in store.dataset_approval_audit())
    assert actions == ["consumed", "decided"]

    pending = store.pending_dataset_approval_audit()
    assert len(pending) == 2  # both intents durable and awaiting delivery (recoverable)

    store.mark_dataset_approval_audit_delivered(pending[0].id)
    still_pending = store.pending_dataset_approval_audit()
    assert len(still_pending) == 1  # recovery only re-emits what was never delivered


def test_dataset_approval_create_caps_are_enforced() -> None:
    # Valid baseline.
    DatasetApprovalRequestCreate(filename="f.csv", objective="obj", csv_text="a,b\n1,2\n", ttl_minutes=60)

    with pytest.raises(ValidationError):
        DatasetApprovalRequestCreate(filename="f.csv", objective="obj", csv_text="x" * 2_000_001, ttl_minutes=60)
    with pytest.raises(ValidationError):
        DatasetApprovalRequestCreate(filename="f.csv", objective="o" * 4001, csv_text="a,b\n1,2\n", ttl_minutes=60)
    with pytest.raises(ValidationError):
        DatasetApprovalRequestCreate(filename="f" * 241, objective="obj", csv_text="a,b\n1,2\n", ttl_minutes=60)
    with pytest.raises(ValidationError):
        DatasetApprovalRequestCreate(filename="f.csv", objective="obj", csv_text="a,b\n1,2\n", ttl_minutes=0)
    with pytest.raises(ValidationError):
        DatasetApprovalRequestCreate(filename="f.csv", objective="obj", csv_text="a,b\n1,2\n", ttl_minutes=1441)
