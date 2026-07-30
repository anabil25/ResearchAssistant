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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from research_assistant_api.app import _agent_message, app
from research_assistant_api.config import Settings
from research_assistant_api.foundry import (
    HostedAgentConfigurationError,
    HostedAgentInvocationError,
    HostedAgentNotReadyError,
    HostedAgentReply,
)
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    DatasetApprovalDecisionRequest,
    DatasetApprovalDenialReason,
    DatasetApprovalError,
    DatasetApprovalRequestCreate,
    DatasetSendOutcome,
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
            entra_auth_enforced=True,
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
    with pytest.raises(DatasetApprovalError, match="without a consumed dataset approval grant") as exc:
        _agent_message(Capability.DATASET, payload, _dataset_result(), dataset_grant=None)
    assert exc.value.reason == DatasetApprovalDenialReason.GRANT_INVARIANT


def _grant(
    *,
    fingerprint: str = "fingerprint-for-a-different-csv",
    capability: Capability = Capability.DATASET,
    tenant_id: str = "demo",
    project_id: str = "demo-project",
) -> Any:
    from research_assistant_api.app import DatasetSendGrant

    return DatasetSendGrant(
        approval_request_id="dsapproval-x",
        tenant_id=tenant_id,
        project_id=project_id,
        capability=capability,
        plan_fingerprint=fingerprint,
        invocation_id="inv-x",
        consumed_by_principal_id="req-1",
    )


def test_agent_message_backstop_refuses_a_grant_for_a_different_plan() -> None:
    """A grant bound to one CSV cannot smuggle a different CSV to Foundry."""
    payload = _dataset_studio_request("secret,val\n1,PHI\n")
    with pytest.raises(DatasetApprovalError, match="does not match the dataset plan being sent") as exc:
        _agent_message(Capability.DATASET, payload, _dataset_result(), dataset_grant=_grant())
    assert exc.value.reason == DatasetApprovalDenialReason.GRANT_INVARIANT


def test_agent_message_backstop_refuses_a_grant_minted_for_another_capability() -> None:
    """Capability is bound into the grant, so a grant cannot be cross-used."""
    csv = "a,b\n1,2\n"
    payload = _dataset_studio_request(csv)
    matching_fingerprint = compute_dataset_plan_fingerprint(
        tenant_id="demo", project_id="demo-project", objective="Analyze", filename="d.csv", csv_text=csv
    )
    wrong_capability = _grant(fingerprint=matching_fingerprint, capability=Capability.LITERATURE)
    with pytest.raises(DatasetApprovalError, match="minted for a different capability") as exc:
        _agent_message(Capability.DATASET, payload, _dataset_result(), dataset_grant=wrong_capability)
    assert exc.value.reason == DatasetApprovalDenialReason.GRANT_INVARIANT


def test_agent_message_backstop_refuses_a_grant_from_another_tenant() -> None:
    """A grant minted under tenant A cannot authorize the same CSV under tenant B."""
    csv = "a,b\n1,2\n"
    payload = _dataset_studio_request(csv)
    other_tenant_fingerprint = compute_dataset_plan_fingerprint(
        tenant_id="tenant-b", project_id="demo-project", objective="Analyze", filename="d.csv", csv_text=csv
    )
    # The grant claims tenant "demo" but carries a fingerprint computed for
    # "tenant-b": recomputation under the grant's own tenant must not match.
    with pytest.raises(DatasetApprovalError, match="does not match the dataset plan being sent") as exc:
        _agent_message(
            Capability.DATASET,
            payload,
            _dataset_result(),
            dataset_grant=_grant(fingerprint=other_tenant_fingerprint),
        )
    assert exc.value.reason == DatasetApprovalDenialReason.GRANT_INVARIANT


def test_fingerprint_is_collision_resistant_across_field_boundaries() -> None:
    # Under the old ``"\u241f".join(...)`` encoding these two distinct plans
    # collided (the separator moved across the objective/filename boundary).
    left = compute_dataset_plan_fingerprint(
        tenant_id="t", project_id="p", objective="a\u241fb", filename="c", csv_text="q"
    )
    right = compute_dataset_plan_fingerprint(
        tenant_id="t", project_id="p", objective="a", filename="b\u241fc", csv_text="q"
    )
    assert left != right


def test_fingerprint_binds_tenant_and_project_distinctly() -> None:
    """A grant minted in tenant A / project A must not validate in tenant B /
    project B: both facts are bound into the durable fingerprint, not merely
    enforced by the ambient single-tenant store."""
    base = {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "objective": "obj",
        "filename": "f.csv",
        "csv_text": "a,b\n1,2\n",
    }
    baseline = compute_dataset_plan_fingerprint(**base)
    assert compute_dataset_plan_fingerprint(**{**base, "tenant_id": "tenant-b"}) != baseline
    assert compute_dataset_plan_fingerprint(**{**base, "project_id": "project-b"}) != baseline


def test_fingerprint_is_deterministic() -> None:
    kwargs = {
        "tenant_id": "t",
        "project_id": "p",
        "objective": "obj",
        "filename": "f.csv",
        "csv_text": "a,b\n1,2\n",
    }
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
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        objective="obj",
        filename="f.csv",
        csv_text=csv,
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


def test_sod_fails_closed_when_requester_principal_is_unattributable() -> None:
    """An approval with no attributable requester principal cannot be decided by
    a real identity: "unknown requester" must never be read as "different
    requester". Records predating requester binding are undecidable by design."""
    store = WorkspaceStore()
    record = store.create_dataset_approval_request(
        plan_fingerprint="fp",
        filename="f.csv",
        objective="obj",
        requested_by="Legacy Requester",
        ttl_minutes=60,
        # requested_by_principal_id deliberately omitted (legacy record).
    )
    with pytest.raises(DatasetApprovalError) as excinfo:
        store.decide_dataset_approval_request(
            record.id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Approving legacy."),
            _identity("any-reviewer", ("research-reviewers",)),
        )
    assert excinfo.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER
    assert "attributable requester" in str(excinfo.value)
    # Still PENDING: the decision never landed.
    reread = store.dataset_approval_request(record.id)
    assert reread is not None
    assert reread.state.value == "pending"


def test_sod_unattributable_record_is_still_decidable_in_demo_sandbox() -> None:
    """The deterministic demo-sandbox exemption keeps local/dev usable, and is
    reachable only when RESEARCH_ALLOW_DEMO_IDENTITY is explicitly enabled."""
    store = WorkspaceStore()
    record = store.create_dataset_approval_request(
        plan_fingerprint="fp",
        filename="f.csv",
        objective="obj",
        requested_by="Demo",
        ttl_minutes=60,
    )
    decided = store.decide_dataset_approval_request(
        record.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Demo approval."),
        _identity("local-developer", ("research-reviewers",), source="local-development"),
    )
    assert decided is not None
    assert decided.state.value == "approved"


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
        requested_by_principal_id="local-developer",
    )
    decided = store.decide_dataset_approval_request(
        demo.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Demo self approval."),
        _identity("local-developer", ("research-reviewers",), source="local-development"),
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
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        objective="obj",
        filename="f.csv",
        csv_text="a,b\n1,2\n",
    )

    pending = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj", requested_by="R", ttl_minutes=60
    )
    with pytest.raises(DatasetApprovalError) as pending_exc:
        store.consume_dataset_approval_request(
            pending.id, plan_fingerprint=fingerprint, invocation_id="i1",
            consumed_by_principal_id="req-9",
        )
    assert pending_exc.value.reason == DatasetApprovalDenialReason.PENDING

    with pytest.raises(DatasetApprovalError) as mismatch_exc:
        store.consume_dataset_approval_request(
            pending.id, plan_fingerprint="different-fp", invocation_id="i2",
            consumed_by_principal_id="req-9",
        )
    assert mismatch_exc.value.reason == DatasetApprovalDenialReason.FINGERPRINT_MISMATCH

    rejected = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename="f.csv",
        objective="obj",
        requested_by="R",
        ttl_minutes=60,
        requested_by_principal_id="req-rejected",
    )
    store.decide_dataset_approval_request(
        rejected.id,
        DatasetApprovalDecisionRequest(decision="rejected", rationale="Contains identifiers."),
        _identity("rev-9", ("research-reviewers",)),
    )
    with pytest.raises(DatasetApprovalError) as rejected_exc:
        store.consume_dataset_approval_request(
            rejected.id, plan_fingerprint=fingerprint, invocation_id="i3",
            consumed_by_principal_id="req-rejected",
        )
    assert rejected_exc.value.reason == DatasetApprovalDenialReason.REJECTED

    approved_id, _ = _seed_decided(store)
    # Force expiry on that exact durable record (looked up by id, not by
    # position) and confirm it fails closed.
    expiring = next(item for item in store._dataset_approvals if item.id == approved_id)
    expiring.expires_at = utc_now() - timedelta(minutes=1)
    with pytest.raises(DatasetApprovalError) as expired_exc:
        store.consume_dataset_approval_request(
            approved_id, plan_fingerprint=fingerprint, invocation_id="i4",
            consumed_by_principal_id="req-9",
        )
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
                approval_id,
                plan_fingerprint=fingerprint,
                invocation_id=f"inv-{index}",
                consumed_by_principal_id="req-9",
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


# --------------------------------------------------------------------------- #
# Post-approval follow-ups: consume only on the sending path; approvals are not
# bearer credentials; the structural backstop denies like every other denial.
# --------------------------------------------------------------------------- #


@contextmanager
def mock_spy_client() -> Iterator[tuple[TestClient, SpyGateway, WorkspaceStore]]:
    """Same harness as :func:`hosted_spy_client` but in mock execution mode,
    where no hosted send occurs."""
    with TestClient(app, raise_server_exceptions=False) as client:
        spy = SpyGateway()
        store = WorkspaceStore()
        app.state.hosted = spy
        app.state.workspace = store
        app.state.settings = Settings(execution_mode="mock", entra_auth_enforced=True)
        yield client, spy, store


def _approve(client: TestClient, store: WorkspaceStore, csv: str, *, requester: str = "alice") -> str:
    approval = _create_approval(
        client, store, objective="obj", filename="f.csv", csv=csv, user_id=requester
    )
    decided = _decide(client, store, approval["id"], "approved", user_id="bob-reviewer")
    assert decided.status_code == 200, decided.text
    return str(approval["id"])


def test_mock_mode_does_not_burn_the_approval_when_nothing_is_sent() -> None:
    """A single-use approval must not be spent when no gateway call follows.
    Otherwise any project member can burn a reviewer-decided approval (an
    availability lever), and an ``action="consumed"`` audit entry stops implying
    that data was actually sent."""
    csv = "group,score\ncontrol,10\n"
    with mock_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert run.status_code == 200, run.text
        assert spy.captured == []
        record = store.dataset_approval_request(approval_id)
        assert record is not None
        assert record.state.value == "approved", "approval was burned with nothing sent"
        assert [entry.action for entry in store.dataset_approval_audit()] == ["decided"]

        # Still spendable afterwards: no availability lever.
        again = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert again.status_code == 200


def test_hosted_mode_consumes_exactly_once_and_audits_the_send() -> None:
    """The mirror of the above: when a send DOES occur, the approval is spent
    and the trail records the delivered outcome alongside the consumption."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert run.status_code == 200, run.text
        assert len(spy.captured) == 1
        record = store.dataset_approval_request(approval_id)
        assert record is not None
        assert record.state.value == "consumed"
        audit = store.dataset_approval_audit()
        assert sorted(e.action for e in audit) == ["consumed", "decided", "send_succeeded"]
        outcome = next(e for e in audit if e.action == "send_succeeded")
        assert outcome.invocation_id == record.consumed_invocation_id


def test_a_decided_approval_is_not_a_bearer_credential_over_http() -> None:
    """Another project member who learns the approval id and holds the exact CSV
    still cannot spend it: consumption is bound to the requesting principal."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv, requester="alice")
        stolen = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="mallory"),
        )
    assert stolen.status_code == 403
    denial = stolen.headers.get("X-Dataset-Approval-Denial")
    assert denial == DatasetApprovalDenialReason.PRINCIPAL_MISMATCH.value
    assert spy.captured == []


def test_consume_denies_a_mismatched_or_unattributable_principal() -> None:
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    with pytest.raises(DatasetApprovalError) as wrong:
        store.consume_dataset_approval_request(
            approval_id,
            plan_fingerprint=fingerprint,
            invocation_id="inv-x",
            consumed_by_principal_id="someone-else",
        )
    assert wrong.value.reason == DatasetApprovalDenialReason.PRINCIPAL_MISMATCH

    with pytest.raises(DatasetApprovalError) as anonymous:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="inv-y"
        )
    assert anonymous.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER

    # Neither failed attempt consumed it.
    record = store.dataset_approval_request(approval_id)
    assert record is not None
    assert record.state.value == "approved"


@pytest.mark.parametrize("odd_csv", [0, False, [], {}, "0", " ", None, 12345, {"x": 1}])
def test_non_string_csv_text_never_500s_and_never_egresses(odd_csv: Any) -> None:
    """The structural backstop must deny like every other denial rather than
    surfacing as an opaque 500, and no shape of client-supplied csv_text may
    reach the gateway."""
    with hosted_spy_client() as (client, spy, store):
        headers = _headers(store, ["researchers"], user_id="alice")
        studios = client.post(
            "/api/studios/dataset/run",
            json={"objective": "obj", "inputs": {"filename": "f.csv", "csv_text": odd_csv}},
            headers=headers,
        )
        research = client.post(
            "/api/research/dataset",
            json={
                "query": "obj",
                "tenant_id": store.tenant_id,
                "project_id": store.project_id,
                "context": {"filename": "f.csv", "csv_text": odd_csv},
            },
            headers=headers,
        )
    for response in (studios, research):
        assert response.status_code != 500
        assert response.status_code in {409, 422}
    assert spy.captured == []


# --------------------------------------------------------------------------- #
# The validate/consume split must hold BOTH properties at once, or it merely
# trades one defect for another:
#   (a) unapproved CSV is rejected BEFORE any local parsing/profiling, and
#   (b) nothing is consumed when zero gateway calls occur.
# --------------------------------------------------------------------------- #


def test_unapproved_csv_is_rejected_before_any_local_profiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``research.run`` parses and profiles the supplied CSV locally. Moving
    authorization after it would let UNAPPROVED client CSV be parsed first --
    trading an availability/audit defect for a confidentiality-adjacent one. The
    early, non-mutating validation must therefore reject first."""
    import research_assistant_core.service as core_service  # noqa: F401  (patch target)
    from research_assistant_core.dataset import profile_csv as canonical_profile_csv

    profiled: list[str] = []

    def spy_profile_csv(csv_text: str) -> Any:
        profiled.append(csv_text)
        return canonical_profile_csv(csv_text)

    monkeypatch.setattr("research_assistant_core.service.profile_csv", spy_profile_csv)

    for mode in ("mock", "hosted"):
        with hosted_spy_client() as (client, spy, store):
            app.state.settings = Settings(
                execution_mode=mode,
                entra_auth_enforced=True,
                foundry_project_endpoint="https://foundry.example.test",
            )
            headers = _headers(store, ["researchers"], user_id="alice")
            body = {
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": PHI_CSV},
            }
            studios = client.post("/api/studios/dataset/run", json=body, headers=headers)
            research = client.post(
                "/api/research/dataset",
                json={
                    "query": "obj",
                    "tenant_id": store.tenant_id,
                    "project_id": store.project_id,
                    "context": {"filename": "f.csv", "csv_text": PHI_CSV},
                },
                headers=headers,
            )
            assert studios.status_code == 409, f"{mode}: {studios.text}"
            assert research.status_code == 409, f"{mode}: {research.text}"
            assert spy.captured == []
            assert profiled == [], f"{mode}: unapproved CSV was profiled locally before rejection"


def test_nothing_is_consumed_when_no_gateway_call_occurs() -> None:
    """Property (b): zero gateway calls implies zero consumption, so
    ``action="consumed"`` keeps meaning 'bytes were sent'."""
    csv = "group,score\ncontrol,10\n"
    with mock_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert run.status_code == 200, run.text
        gateway_calls = len(spy.captured)
        record = store.dataset_approval_request(approval_id)
        audit = [entry.action for entry in store.dataset_approval_audit()]

    assert gateway_calls == 0
    assert record is not None
    assert record.state.value == "approved"
    assert "consumed" not in audit


def test_a_valid_approval_still_authorizes_exactly_one_send_after_the_split() -> None:
    """The early validation must not become load-bearing: the atomic consume
    remains the sole authority and still yields exactly one send."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        body = {
            "objective": "obj",
            "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
        }
        headers = _headers(store, ["researchers"], user_id="alice")
        first = client.post("/api/studios/dataset/run", json=body, headers=headers)
        second = client.post("/api/studios/dataset/run", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.headers.get("X-Dataset-Approval-Denial") == DatasetApprovalDenialReason.ALREADY_CONSUMED.value
    assert len(spy.captured) == 1


def test_gateway_failure_burns_the_approval_documented_residual() -> None:
    """Consume-immediately-before-send means a gateway failure spends the
    approval with nothing delivered. This residual is INHERENT -- at-most-once
    and never-burn-on-failure cannot both hold -- and burning is the fail-closed
    direction. Consuming after the send would instead risk a double send. This
    test pins the accepted behaviour so it cannot regress silently."""
    csv = "group,score\ncontrol,10\n"

    class FailingGateway:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, message: str, *, agent_name: str | None = None, allow_tools: bool = True) -> Any:
            self.calls += 1
            raise HostedAgentInvocationError("hosted agent exploded")

    with hosted_spy_client() as (client, _spy, store):
        failing = FailingGateway()
        app.state.hosted = failing
        approval_id = _approve(client, store, csv)
        response = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        record = store.dataset_approval_request(approval_id)

    assert response.status_code == 502
    assert failing.calls == 1
    assert record is not None
    assert record.state.value == "consumed", "documented residual: a failed send still spends the approval"


# --------------------------------------------------------------------------- #
# Built to the reviewer's PRE-REGISTERED delta probes.
# --------------------------------------------------------------------------- #


def _plan_fingerprint(store: WorkspaceStore, *, objective: str, filename: str, csv: str) -> str:
    return compute_dataset_plan_fingerprint(
        tenant_id=store.tenant_id,
        project_id=store.project_id,
        objective=objective,
        filename=filename,
        csv_text=csv,
    )


def test_consume_path_reruns_the_whole_cross_use_matrix() -> None:
    """PROBE: the early check is NOT the gate. Every condition is re-verified at
    CONSUME, not merely at validate. Each row must DENY at the consume call
    itself -- if any check were dropped because validation covered it, that row
    would flip from a denial to a success."""
    base_csv = "group,score\ncontrol,10\n"
    other_csv = "group,score\ncontrol,999\n"

    def fresh() -> tuple[WorkspaceStore, str, str]:
        store = WorkspaceStore()
        fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv=base_csv)
        record = store.create_dataset_approval_request(
            plan_fingerprint=fingerprint,
            filename="f.csv",
            objective="obj",
            requested_by="Requester",
            ttl_minutes=60,
            requested_by_principal_id="req-9",
        )
        return store, record.id, fingerprint

    # --- plan variance: each differing fact must break the fingerprint bind ---
    variants: dict[str, Callable[[WorkspaceStore], str]] = {
        "project": lambda s: compute_dataset_plan_fingerprint(
            tenant_id=s.tenant_id, project_id="other-project", objective="obj",
            filename="f.csv", csv_text=base_csv),
        "tenant": lambda s: compute_dataset_plan_fingerprint(
            tenant_id="other-tenant", project_id=s.project_id, objective="obj",
            filename="f.csv", csv_text=base_csv),
        "objective": lambda s: _plan_fingerprint(s, objective="different", filename="f.csv", csv=base_csv),
        "filename": lambda s: _plan_fingerprint(s, objective="obj", filename="other.csv", csv=base_csv),
        "csv": lambda s: _plan_fingerprint(s, objective="obj", filename="f.csv", csv=other_csv),
    }
    for label, make_fingerprint in variants.items():
        store, approval_id, _ = fresh()
        store.decide_dataset_approval_request(
            approval_id,
            DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
            _identity("rev-9", ("research-reviewers",)),
        )
        with pytest.raises(DatasetApprovalError) as exc:
            store.consume_dataset_approval_request(
                approval_id,
                plan_fingerprint=make_fingerprint(store),
                invocation_id="inv-x",
                consumed_by_principal_id="req-9",
            )
        assert exc.value.reason == DatasetApprovalDenialReason.FINGERPRINT_MISMATCH, label

    # --- state variance ---
    store, approval_id, fingerprint = fresh()
    with pytest.raises(DatasetApprovalError) as pending_exc:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="i", consumed_by_principal_id="req-9"
        )
    assert pending_exc.value.reason == DatasetApprovalDenialReason.PENDING

    store, approval_id, fingerprint = fresh()
    store.decide_dataset_approval_request(
        approval_id,
        DatasetApprovalDecisionRequest(decision="rejected", rationale="No."),
        _identity("rev-9", ("research-reviewers",)),
    )
    with pytest.raises(DatasetApprovalError) as rejected_exc:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="i", consumed_by_principal_id="req-9"
        )
    assert rejected_exc.value.reason == DatasetApprovalDenialReason.REJECTED

    store, approval_id, fingerprint = fresh()
    store.decide_dataset_approval_request(
        approval_id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    store.consume_dataset_approval_request(
        approval_id, plan_fingerprint=fingerprint, invocation_id="i1", consumed_by_principal_id="req-9"
    )
    with pytest.raises(DatasetApprovalError) as replay_exc:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="i2", consumed_by_principal_id="req-9"
        )
    assert replay_exc.value.reason == DatasetApprovalDenialReason.ALREADY_CONSUMED

    store, approval_id, fingerprint = fresh()
    store.decide_dataset_approval_request(
        approval_id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    expiring = next(item for item in store._dataset_approvals if item.id == approval_id)
    expiring.expires_at = utc_now() - timedelta(minutes=1)
    with pytest.raises(DatasetApprovalError) as expired_exc:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="i", consumed_by_principal_id="req-9"
        )
    assert expired_exc.value.reason == DatasetApprovalDenialReason.EXPIRED


def test_validation_success_does_not_survive_a_concurrent_consume() -> None:
    """PROBE: the TOCTOU window. Validation succeeds, a SECOND caller consumes
    first, and the original attempt must DENY rather than proceed on stale
    validation."""
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    # First caller validates successfully...
    validated = store.validate_dataset_approval_request(
        approval_id, plan_fingerprint=fingerprint, consumed_by_principal_id="req-9"
    )
    assert validated.state.value == "approved"

    # ...a second request consumes in between...
    store.consume_dataset_approval_request(
        approval_id, plan_fingerprint=fingerprint, invocation_id="winner", consumed_by_principal_id="req-9"
    )

    # ...and the first caller's now-stale validation grants nothing.
    with pytest.raises(DatasetApprovalError) as exc:
        store.consume_dataset_approval_request(
            approval_id, plan_fingerprint=fingerprint, invocation_id="loser", consumed_by_principal_id="req-9"
        )
    assert exc.value.reason == DatasetApprovalDenialReason.ALREADY_CONSUMED
    record = store.dataset_approval_request(approval_id)
    assert record is not None
    assert record.consumed_invocation_id == "winner"


def test_twenty_four_racing_threads_yield_exactly_one_winner() -> None:
    """PROBE: the 24-thread race, re-run against the split implementation. Each
    thread validates then consumes, mimicking the route's two-phase shape."""
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    barrier = threading.Barrier(24)
    winners: list[str] = []
    denials: list[DatasetApprovalDenialReason] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            store.validate_dataset_approval_request(
                approval_id, plan_fingerprint=fingerprint, consumed_by_principal_id="req-9"
            )
        except DatasetApprovalError as exc:
            with guard:
                denials.append(exc.reason)
            return
        try:
            record = store.consume_dataset_approval_request(
                approval_id,
                plan_fingerprint=fingerprint,
                invocation_id=f"inv-{index}",
                consumed_by_principal_id="req-9",
            )
        except DatasetApprovalError as exc:
            with guard:
                denials.append(exc.reason)
        else:
            with guard:
                winners.append(str(record.consumed_invocation_id))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(denials) == 23
    assert all(r == DatasetApprovalDenialReason.ALREADY_CONSUMED for r in denials)


def test_grant_fields_are_traceable_to_the_transition_record() -> None:
    """PROBE: the grant is minted BY the transition, so its fields come from the
    stored record rather than from validate-time request inputs."""
    from research_assistant_api.app import _consume_dataset_analysis

    store = WorkspaceStore()
    csv = "group,score\ncontrol,10\n"
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv=csv)
    created = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename="f.csv",
        objective="obj",
        requested_by="Requester",
        ttl_minutes=60,
        requested_by_principal_id="req-9",
    )
    store.decide_dataset_approval_request(
        created.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    payload = StudioRunRequest(
        objective="obj",
        inputs={"filename": "f.csv", "csv_text": csv, "approval_request_id": created.id},
    )
    grant = _consume_dataset_analysis(
        Capability.DATASET, payload, store, _identity("req-9", ("researchers",))
    )
    record = store.dataset_approval_request(created.id)

    assert grant is not None
    assert record is not None
    assert record.state.value == "consumed"
    assert grant.invocation_id == record.consumed_invocation_id
    assert grant.plan_fingerprint == record.plan_fingerprint
    assert grant.approval_request_id == record.id


@pytest.mark.parametrize("mode", ["mock", "hosted"])  # execution_mode literals
@pytest.mark.parametrize("route", ["studios", "research"])
def test_no_send_preserves_a_usable_approval(mode: str, route: str) -> None:
    """PROBE: zero gateway calls implies zero consumption -- and the approval is
    genuinely PRESERVED, proven by a subsequent legitimate run succeeding, not
    merely by a state string that did not change."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        app.state.settings = Settings(
            execution_mode=cast(Literal["mock", "hosted"], mode),
            entra_auth_enforced=True,
            foundry_project_endpoint="https://foundry.example.test",
        )
        approval_id = _approve(client, store, csv)
        headers = _headers(store, ["researchers"], user_id="alice")

        # A run that must NOT send: wrong CSV, so it is denied before any send.
        if route == "studios":
            denied = client.post(
                "/api/studios/dataset/run",
                json={
                    "objective": "obj",
                    "inputs": {
                        "filename": "f.csv",
                        "csv_text": "group,score\ntampered,1\n",
                        "approval_request_id": approval_id,
                    },
                },
                headers=headers,
            )
        else:
            denied = client.post(
                "/api/research/dataset",
                json={
                    "query": "obj",
                    "tenant_id": store.tenant_id,
                    "project_id": store.project_id,
                    "context": {
                        "filename": "f.csv",
                        "csv_text": "group,score\ntampered,1\n",
                        "approval_request_id": approval_id,
                    },
                },
                headers=headers,
            )
        assert denied.status_code == 409
        assert spy.captured == []
        preserved = store.dataset_approval_request(approval_id)
        assert preserved is not None
        assert preserved.state.value == "approved"
        assert "consumed" not in [e.action for e in store.dataset_approval_audit()]

        # The second half of the probe: the approval is still SPENDABLE.
        legitimate = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=headers,
        )
        assert legitimate.status_code == 200, legitimate.text


def test_profile_csv_sees_only_approved_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """PROBE: zero profile_csv invocations on the unapproved path, and on the
    approved path the argument is exactly the approved bytes."""
    from research_assistant_core.dataset import profile_csv as canonical_profile_csv

    seen: list[str] = []

    def spy_profile_csv(csv_text: str) -> Any:
        seen.append(csv_text)
        return canonical_profile_csv(csv_text)

    monkeypatch.setattr("research_assistant_core.service.profile_csv", spy_profile_csv)

    approved_csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        headers = _headers(store, ["researchers"], user_id="alice")
        unapproved = client.post(
            "/api/studios/dataset/run",
            json={"objective": "obj", "inputs": {"filename": "f.csv", "csv_text": PHI_CSV}},
            headers=headers,
        )
        assert unapproved.status_code == 409
        assert seen == [], "unapproved CSV reached profile_csv"
        assert spy.captured == [], "unapproved CSV reached the hosted gateway"

        approval_id = _approve(client, store, approved_csv)
        ok = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {
                    "filename": "f.csv",
                    "csv_text": approved_csv,
                    "approval_request_id": approval_id,
                },
            },
            headers=headers,
        )
        assert ok.status_code == 200, ok.text

    assert seen == [approved_csv]
    assert PHI_CSV not in seen


def test_failed_send_is_audited_as_attempted_not_delivered() -> None:
    """PROBE for item 5: 'consumed' means a send was ATTEMPTED. A gateway
    failure must leave the trail unambiguous via a distinct send_failed
    entry, recoverable through the same pending-outbox machinery."""
    csv = "group,score\ncontrol,10\n"

    class FailingGateway:
        def invoke(self, message: str, *, agent_name: str | None = None, allow_tools: bool = True) -> Any:
            raise HostedAgentInvocationError("hosted agent exploded")

    with hosted_spy_client() as (client, _unused_spy, store):
        approval_id = _approve(client, store, csv)
        app.state.hosted = FailingGateway()
        response = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        record = store.dataset_approval_request(approval_id)
        audit = store.dataset_approval_audit()
        pending = store.pending_dataset_approval_audit()

    assert response.status_code == 502
    assert record is not None
    assert record.state.value == "consumed"
    assert sorted(e.action for e in audit) == ["consumed", "decided", "send_failed"]
    failed = next(e for e in audit if e.action == "send_failed")
    assert failed.invocation_id == record.consumed_invocation_id
    # Recoverable on the same terms as every other outbox intent.
    assert failed.id in {e.id for e in pending}


def test_every_denial_reason_has_a_declared_status() -> None:
    """Mechanical: a missing _DATASET_DENIAL_STATUS entry would still yield 409
    via the default, but by ACCIDENT rather than by DECLARATION."""
    from research_assistant_api.app import _DATASET_DENIAL_STATUS

    undeclared = set(DatasetApprovalDenialReason) - set(_DATASET_DENIAL_STATUS)
    assert undeclared == set(), f"undeclared denial reasons: {sorted(r.value for r in undeclared)}"


# --------------------------------------------------------------------------- #
# Item 5 pre-registered verification. The send outcome is a SEPARATE audit
# entry with its own `action`; `delivery` stays the outbox marker. Absence of an
# outcome entry reads as UNKNOWN, never as success.
# --------------------------------------------------------------------------- #


class _RaisingGateway:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def invoke(self, message: str, *, agent_name: str | None = None, allow_tools: bool = True) -> Any:
        self.calls += 1
        raise self._exc


def _audit_for(store: WorkspaceStore, request_id: str) -> list[str]:
    return [e.action for e in store.dataset_approval_audit() if e.request_id == request_id]


def test_delivered_path_records_exactly_one_success_outcome() -> None:
    """PRE-REGISTERED: gateway returns normally -> the trail for that request_id
    contains decided, consumed, and EXACTLY ONE success outcome, no duplicates."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert run.status_code == 200, run.text
        assert len(spy.captured) == 1
        actions = _audit_for(store, approval_id)
        outcome = store.dataset_send_outcome(approval_id)

    assert sorted(actions) == ["consumed", "decided", "send_succeeded"]
    assert actions.count("send_succeeded") == 1, "duplicate outcome entries on a single run"
    assert actions.count("send_failed") == 0
    assert outcome is DatasetSendOutcome.DELIVERED


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (HostedAgentConfigurationError("no endpoint"), 503),
        (HostedAgentNotReadyError("not ready"), 503),
        (HostedAgentInvocationError("boom"), 502),
    ],
    ids=["configuration", "not_ready", "invocation"],
)
@pytest.mark.parametrize("route", ["studios", "research"])
def test_failed_send_records_failure_outcome_on_both_routes(
    exc: Exception, expected_status: int, route: str
) -> None:
    """PRE-REGISTERED: each hosted failure mode leaves the approval consumed AND
    records a failure outcome, with the route still returning its own status.
    Both routes have separate except ladders, so both are exercised."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, _unused, store):
        approval_id = _approve(client, store, csv)
        gateway = _RaisingGateway(exc)
        app.state.hosted = gateway
        headers = _headers(store, ["researchers"], user_id="alice")
        if route == "studios":
            response = client.post(
                "/api/studios/dataset/run",
                json={
                    "objective": "obj",
                    "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
                },
                headers=headers,
            )
        else:
            response = client.post(
                "/api/research/dataset",
                json={
                    "query": "obj",
                    "tenant_id": store.tenant_id,
                    "project_id": store.project_id,
                    "context": {
                        "filename": "f.csv",
                        "csv_text": csv,
                        "approval_request_id": approval_id,
                    },
                },
                headers=headers,
            )
        record = store.dataset_approval_request(approval_id)
        actions = _audit_for(store, approval_id)
        outcome = store.dataset_send_outcome(approval_id)

    assert response.status_code == expected_status
    assert gateway.calls == 1
    assert record is not None
    assert record.state.value == "consumed"
    assert actions.count("send_failed") == 1
    assert "send_succeeded" not in actions
    assert outcome is DatasetSendOutcome.FAILED


def test_absence_of_an_outcome_entry_reads_as_unknown_not_success() -> None:
    """PRE-REGISTERED: the crash-between case. A consumption with no outcome
    entry -- e.g. the process died between the two writes -- must read UNKNOWN.
    If absence read as delivered, the outcome entry would buy nothing."""
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)

    assert store.dataset_send_outcome(approval_id) is DatasetSendOutcome.UNKNOWN

    store.consume_dataset_approval_request(
        approval_id,
        plan_fingerprint=fingerprint,
        invocation_id="inv-crash",
        consumed_by_principal_id="req-9",
    )
    # Consumed, but the process "crashed" before recording an outcome.
    record = store.dataset_approval_request(approval_id)
    assert record is not None
    assert record.state.value == "consumed"
    assert _audit_for(store, approval_id) == ["decided", "consumed"]
    assert store.dataset_send_outcome(approval_id) is DatasetSendOutcome.UNKNOWN
    assert store.dataset_send_outcome(approval_id, invocation_id="inv-crash") is DatasetSendOutcome.UNKNOWN

    # An unrelated invocation id must also not borrow another entry's outcome.
    store.record_dataset_send_outcome(
        approval_id,
        invocation_id="inv-crash",
        plan_fingerprint=fingerprint,
        delivered=True,
        actor_principal_id="req-9",
    )
    assert store.dataset_send_outcome(approval_id, invocation_id="inv-crash") is DatasetSendOutcome.DELIVERED
    assert store.dataset_send_outcome(approval_id, invocation_id="inv-other") is DatasetSendOutcome.UNKNOWN


def test_send_outcome_does_not_overload_the_outbox_delivery_marker() -> None:
    """PRE-REGISTERED naming trap: `delivery` means "has this audit intent been
    emitted downstream", NOT "did the send succeed". A failed send must still
    leave its own audit entry PENDING for outbox recovery, otherwise a
    send-failure becomes indistinguishable from an unemitted audit record."""
    store = WorkspaceStore()
    approval_id, fingerprint = _seed_decided(store)
    store.consume_dataset_approval_request(
        approval_id, plan_fingerprint=fingerprint, invocation_id="inv-1", consumed_by_principal_id="req-9"
    )
    failed = store.record_dataset_send_outcome(
        approval_id,
        invocation_id="inv-1",
        plan_fingerprint=fingerprint,
        delivered=False,
        actor_principal_id="req-9",
    )

    # The two state machines are independent: send FAILED, audit intent PENDING.
    assert failed.action == "send_failed"
    assert failed.delivery == "pending"
    assert store.dataset_send_outcome(approval_id) is DatasetSendOutcome.FAILED
    assert failed.id in {e.id for e in store.pending_dataset_approval_audit()}

    # Emitting the audit record downstream must NOT change the send outcome.
    store.mark_dataset_approval_audit_delivered(failed.id)
    assert failed.id not in {e.id for e in store.pending_dataset_approval_audit()}
    assert store.dataset_send_outcome(approval_id) is DatasetSendOutcome.FAILED, (
        "outbox emission changed the send outcome -- the two meanings are collapsed"
    )


def test_consumed_invariant_is_documented_as_attempted_not_sent() -> None:
    """PRE-REGISTERED: invariant wording checked verbatim."""
    from research_assistant_api.workspace import DatasetApprovalAuditEntry as Entry

    text = " ".join(
        part for part in (Entry.__doc__, WorkspaceStore.record_dataset_send_outcome.__doc__) if part
    )
    assert "ATTEMPTED" in text
    assert "never that a send happened" in text or "never that a send happened" in (Entry.__doc__ or "")
    assert "consumed means sent" not in text.lower().replace('"', "")


# --------------------------------------------------------------------------- #
# Letter-vs-intent guards. Each of these exists because the corresponding
# requirement is satisfiable in letter by an implementation that defeats it.
# --------------------------------------------------------------------------- #


def test_no_content_dependent_processing_of_unapproved_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTENT (not just the profile_csv spy): NO content-dependent processing of
    unapproved CSV may occur -- not merely that one named function is unreached.

    Asserts three independent layers stay untouched: the research service never
    runs at all, profile_csv is never called, and the CSV parser underneath it
    (polars.read_csv) never sees the bytes.
    """
    import polars
    from research_assistant_core.dataset import profile_csv as canonical_profile_csv

    profiled: list[str] = []
    parsed: list[Any] = []
    service_runs: list[Any] = []

    def spy_profile_csv(csv_text: str) -> Any:
        profiled.append(csv_text)
        return canonical_profile_csv(csv_text)

    original_read_csv = polars.read_csv

    def spy_read_csv(source: Any, **kwargs: Any) -> Any:
        parsed.append(source)
        return original_read_csv(source, **kwargs)

    monkeypatch.setattr("research_assistant_core.service.profile_csv", spy_profile_csv)
    monkeypatch.setattr("research_assistant_core.dataset.pl.read_csv", spy_read_csv)

    for mode in ("mock", "hosted"):
        with hosted_spy_client() as (client, spy, store):
            app.state.settings = Settings(
                execution_mode=mode,
                entra_auth_enforced=True,
                foundry_project_endpoint="https://foundry.example.test",
            )
            research = app.state.research
            original_run = research.run

            def recording_run(*args: Any, _original: Any = original_run, **kwargs: Any) -> Any:
                service_runs.append(args)
                return _original(*args, **kwargs)

            monkeypatch.setattr(research, "run", recording_run)

            headers = _headers(store, ["researchers"], user_id="alice")
            studios = client.post(
                "/api/studios/dataset/run",
                json={"objective": "obj", "inputs": {"filename": "f.csv", "csv_text": PHI_CSV}},
                headers=headers,
            )
            research_route = client.post(
                "/api/research/dataset",
                json={
                    "query": "obj",
                    "tenant_id": store.tenant_id,
                    "project_id": store.project_id,
                    "context": {"filename": "f.csv", "csv_text": PHI_CSV},
                },
                headers=headers,
            )
            assert studios.status_code == 409, f"{mode}: {studios.text}"
            assert research_route.status_code == 409, f"{mode}: {research_route.text}"
            assert spy.captured == [], f"{mode}: unapproved CSV reached the gateway"

    assert service_runs == [], "the research service ran on unapproved CSV"
    assert profiled == [], "unapproved CSV reached profile_csv"
    assert parsed == [], "unapproved CSV reached the CSV parser"


def test_fix_removes_burns_without_adding_any_gateway_sends() -> None:
    """INTENT: 'zero consumption when gateway_calls == 0' must be achieved by
    REMOVING BURNS, never by widening the guard so the gateway fires on paths
    that previously skipped it. Pins gateway call counts on the
    previously-non-calling paths."""
    csv = "group,score\ncontrol,10\n"

    # Mock mode never calls the gateway -- approved or not, dataset or not.
    with mock_spy_client() as (client, spy, store):
        headers = _headers(store, ["researchers"], user_id="alice")
        approval_id = _approve(client, store, csv)
        client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=headers,
        )
        client.post(
            "/api/studios/dataset/run",
            json={"objective": "obj", "inputs": {"filename": "f.csv", "csv_text": csv}},
            headers=headers,
        )
        client.post(
            "/api/studios/literature/run",
            json={"objective": "Summarize the evidence base."},
            headers=headers,
        )
        client.post(
            "/api/research/dataset",
            json={
                "query": "obj",
                "tenant_id": store.tenant_id,
                "project_id": store.project_id,
                "context": {"filename": "f.csv", "csv_text": csv},
            },
            headers=headers,
        )
        assert spy.captured == [], "a previously-non-calling path now reaches the gateway"

    # Hosted mode: a DENIED dataset run must also never reach the gateway.
    with hosted_spy_client() as (client, spy, store):
        headers = _headers(store, ["researchers"], user_id="alice")
        denied = client.post(
            "/api/studios/dataset/run",
            json={"objective": "obj", "inputs": {"filename": "f.csv", "csv_text": csv}},
            headers=headers,
        )
        assert denied.status_code == 409
        assert spy.captured == []

        # ...and an APPROVED one reaches it exactly once, not more.
        approval_id = _approve(client, store, csv)
        ok = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert len(spy.captured) == 1


def test_unattributable_requester_is_observable_on_the_wire() -> None:
    """INTENT: the new denial reason must not be a dead enum member. Reachable
    and observable end-to-end, at BOTH decide time and consume time.

    Simulates the announced breaking case directly: an approval loaded from
    Cosmos that predates requesterPrincipalId, i.e. one whose requester cannot
    be attributed.
    """
    csv = "group,score\ncontrol,10\n"
    reason = DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER.value

    # --- decide time ---
    with hosted_spy_client() as (client, _spy, store):
        legacy = store.create_dataset_approval_request(
            plan_fingerprint=_plan_fingerprint(store, objective="obj", filename="f.csv", csv=csv),
            filename="f.csv",
            objective="obj",
            requested_by="Legacy Requester",
            ttl_minutes=60,
            # no requested_by_principal_id: the pre-existing-record shape
        )
        decision = client.post(
            f"/api/studios/dataset/approval-requests/{legacy.id}/decision",
            json={"decision": "approved", "rationale": "Approving a legacy record."},
            headers=_headers(store, ["research-reviewers"], user_id="reviewer-1"),
        )
    assert decision.status_code == 403
    assert decision.headers.get("X-Dataset-Approval-Denial") == reason

    # --- consume time: an already-APPROVED record whose requester is unknown ---
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv)
        # Simulate the in-flight Cosmos record: decided, but no requester bound.
        store._dataset_requester_principals.pop(approval_id, None)
        run = client.post(
            "/api/studios/dataset/run",
            json={
                "objective": "obj",
                "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
            },
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        record = store.dataset_approval_request(approval_id)

    assert run.status_code == 403
    assert run.headers.get("X-Dataset-Approval-Denial") == reason
    assert spy.captured == [], "unattributable approval still reached the gateway"
    assert record is not None
    assert record.state.value == "approved", "denied consume must not spend the approval"


def test_requester_only_consumption_excludes_even_the_approving_reviewer() -> None:
    """RULING: consumption is REQUESTER-ONLY. It composes with SOD coherently --
    the requester requests, a DIFFERENT reviewer approves, and the REQUESTER
    consumes. The reviewer who approved it cannot spend it either, and neither
    can any other project member."""
    csv = "group,score\ncontrol,10\n"
    with hosted_spy_client() as (client, spy, store):
        approval_id = _approve(client, store, csv, requester="alice")

        body = {
            "objective": "obj",
            "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
        }
        # The approving reviewer is a different principal -> denied.
        by_reviewer = client.post(
            "/api/studios/dataset/run",
            json=body,
            headers=_headers(store, ["researchers"], user_id="bob-reviewer"),
        )
        # An unrelated colleague -> denied ("analyst requests, colleague runs" breaks).
        by_colleague = client.post(
            "/api/studios/dataset/run",
            json=body,
            headers=_headers(store, ["researchers"], user_id="carol"),
        )
        assert by_reviewer.status_code == 403
        assert by_colleague.status_code == 403
        for response in (by_reviewer, by_colleague):
            denial = response.headers.get("X-Dataset-Approval-Denial")
            assert denial == DatasetApprovalDenialReason.PRINCIPAL_MISMATCH.value
        assert spy.captured == []

        # The requester themself succeeds, exactly once.
        by_requester = client.post(
            "/api/studios/dataset/run",
            json=body,
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert by_requester.status_code == 200, by_requester.text
        assert len(spy.captured) == 1


# --------------------------------------------------------------------------- #
# Rulings on the converged consume-time check.
# --------------------------------------------------------------------------- #


def test_consume_has_no_sod_style_exemption_structurally() -> None:
    """RULING 1: _dataset_sod_exempt must NOT be reused at consume. The
    predicate means something different on each side -- at decide it waives
    'the requester may not be the reviewer'; at consume it would waive 'only the
    requester may consume', i.e. ANYONE MAY CONSUME. Different waivers, and the
    second has no use case.

    Structural proof: the consume path never consults it, and
    consume_dataset_approval_request does not even accept an IdentityContext, so
    an identity-keyed exemption cannot be applied there.
    """
    import inspect

    chain = "".join(
        inspect.getsource(fn)
        for fn in (
            WorkspaceStore.consume_dataset_approval_request,
            WorkspaceStore._check_dataset_approval_usable,
            WorkspaceStore._verify_consuming_principal,
        )
    )
    assert "_dataset_sod_exempt" not in chain, "consume reuses the decide-time exemption"

    signature = inspect.signature(WorkspaceStore.consume_dataset_approval_request)
    annotations = {str(p.annotation) for p in signature.parameters.values()}
    assert not any("IdentityContext" in a for a in annotations)


def test_demo_sandbox_passes_by_matching_and_is_not_exempt_at_consume() -> None:
    """RULING 1, behavioural: under demo-sandbox the requester and consumer are
    the same fixed principal, so the match SUCCEEDS NATURALLY -- no exemption is
    needed. And a demo-sandbox consumer with a DIFFERENT stored requester must
    still DENY, proving no waiver was smuggled in."""
    demo_principal = "local-developer"

    # Requester == consumer: passes by MATCHING, not by exemption.
    store = WorkspaceStore()
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv="a,b\n1,2\n")
    own = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename="f.csv",
        objective="obj",
        requested_by="Demo",
        ttl_minutes=60,
        requested_by_principal_id=demo_principal,
    )
    store.decide_dataset_approval_request(
        own.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Demo self-approval."),
        _identity(demo_principal, ("research-reviewers",), source="local-development"),
    )
    consumed = store.consume_dataset_approval_request(
        own.id,
        plan_fingerprint=fingerprint,
        invocation_id="demo-inv",
        consumed_by_principal_id=demo_principal,
    )
    assert consumed.state.value == "consumed"

    # Requester != consumer: the demo consumer is NOT waived through.
    other = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint,
        filename="f.csv",
        objective="obj",
        requested_by="Alice",
        ttl_minutes=60,
        requested_by_principal_id="alice",
    )
    store.decide_dataset_approval_request(
        other.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    with pytest.raises(DatasetApprovalError) as exc:
        store.consume_dataset_approval_request(
            other.id,
            plan_fingerprint=fingerprint,
            invocation_id="demo-steal",
            consumed_by_principal_id=demo_principal,
        )
    assert exc.value.reason == DatasetApprovalDenialReason.PRINCIPAL_MISMATCH
    still = store.dataset_approval_request(other.id)
    assert still is not None
    assert still.state.value == "approved"


def test_the_two_consume_denials_are_distinct_and_both_wire_observable() -> None:
    """RULING 2: the converged check needs TWO reasons, not one, because they
    demand different monitoring responses -- the legacy-unverifiable denial
    SPIKES ON DEPLOY DAY and is expected, while wrong-principal is a live
    authorization failure. Both must be individually reachable on the wire;
    neither may be a dead enum member."""
    csv = "group,score\ncontrol,10\n"
    unverifiable = DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER.value
    wrong_principal = DatasetApprovalDenialReason.PRINCIPAL_MISMATCH.value
    assert unverifiable != wrong_principal

    def body_for(approval_id: str) -> dict[str, Any]:
        return {
            "objective": "obj",
            "inputs": {"filename": "f.csv", "csv_text": csv, "approval_request_id": approval_id},
        }

    # (a) no stored requester -> consumption unverifiable
    with hosted_spy_client() as (client, spy_a, store):
        legacy_id = _approve(client, store, csv, requester="alice")
        store._dataset_requester_principals.pop(legacy_id, None)
        legacy_response = client.post(
            "/api/studios/dataset/run",
            json=body_for(legacy_id),
            headers=_headers(store, ["researchers"], user_id="alice"),
        )
        assert spy_a.captured == []

    # (b) stored requester present but different -> wrong principal
    with hosted_spy_client() as (client, spy_b, store):
        owned_id = _approve(client, store, csv, requester="alice")
        mismatch_response = client.post(
            "/api/studios/dataset/run",
            json=body_for(owned_id),
            headers=_headers(store, ["researchers"], user_id="mallory"),
        )
        assert spy_b.captured == []

    assert legacy_response.status_code == 403
    assert mismatch_response.status_code == 403
    assert legacy_response.headers.get("X-Dataset-Approval-Denial") == unverifiable
    assert mismatch_response.headers.get("X-Dataset-Approval-Denial") == wrong_principal
    # The whole point: an operator can tell the expected deploy-day spike apart
    # from a live authorization failure without parsing prose.
    assert legacy_response.headers.get("X-Dataset-Approval-Denial") != mismatch_response.headers.get(
        "X-Dataset-Approval-Denial"
    )


def test_enumeration_narrows_to_the_genuinely_affected_set() -> None:
    """RULING 3: the number must be ACTIONABLE. Only APPROVED and unexpired
    records are harmed -- consumed ones cannot be consumed again regardless, and
    rejected or expired ones already deny. Counting those too inflates the
    figure without adding a decision, which is the difference between a panic
    and a decision.

    Note the construction: a legacy record can no longer be DECIDED (that is the
    fix working), so the APPROVED-without-requester state is built the way it
    genuinely arises -- decided while attributed, then loaded from a document
    that omits requesterPrincipalId.
    """
    store = WorkspaceStore()
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv="a,b\n1,2\n")
    reviewer = _identity("rev-9", ("research-reviewers",))

    def make(
        requested_by: str, decision: Literal["approved", "rejected"] | None, *, legacy: bool
    ) -> Any:
        record = store.create_dataset_approval_request(
            plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
            requested_by=requested_by, ttl_minutes=60,
            requested_by_principal_id=f"principal-{requested_by}",
        )
        if decision is not None:
            store.decide_dataset_approval_request(
                record.id,
                DatasetApprovalDecisionRequest(decision=decision, rationale="Reviewed."),
                reviewer,
            )
        if legacy:
            # Exactly what _reload_dataset_state produces for a document that
            # omits requesterPrincipalId.
            store._dataset_requester_principals.pop(record.id, None)
        return record

    at_risk = make("AtRisk", "approved", legacy=True)
    attributed = make("Attributed", "approved", legacy=False)
    still_pending = make("Pending", None, legacy=True)
    rejected = make("Rejected", "rejected", legacy=True)
    expired = make("Expired", "approved", legacy=True)
    next(r for r in store._dataset_approvals if r.id == expired.id).expires_at = utc_now() - timedelta(minutes=1)

    affected = store.dataset_approvals_blocked_by_requester_attribution()
    affected_ids = {record.id for record in affected}

    assert affected_ids == {at_risk.id}, "enumeration must report only the genuinely affected records"
    for excluded, why in (
        (attributed.id, "has a requester principal"),
        (still_pending.id, "not approved"),
        (rejected.id, "already denies"),
        (expired.id, "already denies"),
    ):
        assert excluded not in affected_ids, why
    assert all(record.requested_by for record in affected)
    # And the reported record really is the one that denies at consume.
    with pytest.raises(DatasetApprovalError) as exc:
        store.consume_dataset_approval_request(
            at_risk.id, plan_fingerprint=fingerprint, invocation_id="i",
            consumed_by_principal_id="principal-AtRisk",
        )
    assert exc.value.reason == DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER


def test_fingerprint_bump_invalidates_more_than_the_legacy_population() -> None:
    """ITEM 4: the v2->v3 domain bump invalidates EVERY unspent approval, not
    only the legacy ones missing a requester principal. It is a strictly larger
    population and a THIRD monitoring signal (fingerprint_mismatch), so it must
    be announced and quantified separately rather than folded into the
    requester-attribution breaking change."""
    store = WorkspaceStore()
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv="a,b\n1,2\n")
    reviewer = _identity("rev-9", ("research-reviewers",))

    fully_attributed = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
        requested_by="Alice", ttl_minutes=60, requested_by_principal_id="alice",
    )
    store.decide_dataset_approval_request(
        fully_attributed.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        reviewer,
    )
    pending = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
        requested_by="Bob", ttl_minutes=60, requested_by_principal_id="bob",
    )
    legacy = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
        requested_by="Legacy", ttl_minutes=60, requested_by_principal_id="legacy",
    )
    store.decide_dataset_approval_request(
        legacy.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        reviewer,
    )
    store._dataset_requester_principals.pop(legacy.id, None)

    attribution_population = {
        r.id for r in store.dataset_approvals_blocked_by_requester_attribution()
    }
    fingerprint_population = {
        r.id for r in store.dataset_approvals_invalidated_by_fingerprint_version()
    }

    # Requester-attribution catches only the legacy record...
    assert attribution_population == {legacy.id}
    # ...while the fingerprint bump invalidates every unspent approval, including
    # fully-attributed and still-pending ones.
    assert fingerprint_population == {fully_attributed.id, pending.id, legacy.id}
    assert attribution_population < fingerprint_population, (
        "the fingerprint bump is a strictly LARGER breaking change and must be "
        "announced on its own, not folded into the requester-attribution one"
    )

    # And it is a distinct denial reason: a v2-era fingerprint no longer matches.
    stale = compute_dataset_plan_fingerprint(
        tenant_id=store.tenant_id, project_id=store.project_id,
        objective="obj", filename="f.csv", csv_text="a,b\n1,2\nDIFFERENT\n",
    )
    with pytest.raises(DatasetApprovalError) as exc:
        store.consume_dataset_approval_request(
            fully_attributed.id, plan_fingerprint=stale,
            invocation_id="i", consumed_by_principal_id="alice",
        )
    assert exc.value.reason == DatasetApprovalDenialReason.FINGERPRINT_MISMATCH
    assert exc.value.reason not in {
        DatasetApprovalDenialReason.UNATTRIBUTABLE_REQUESTER,
        DatasetApprovalDenialReason.PRINCIPAL_MISMATCH,
    }


def test_fingerprint_domain_version_is_pinned() -> None:
    """Pins the announced version so a future silent bump cannot invalidate
    every in-flight approval without a corresponding announcement."""
    from research_assistant_api.workspace import (
        _DATASET_FINGERPRINT_DOMAIN,
        _DATASET_FINGERPRINT_VERSION,
    )

    assert _DATASET_FINGERPRINT_VERSION == 3
    assert _DATASET_FINGERPRINT_DOMAIN == "research_assistant.dataset_plan"


def test_audit_order_is_total_and_causal_under_identical_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latent-fragility class, fixed STRUCTURALLY rather than pinned.

    Ordering previously survived only because Python's sort is STABLE and the
    Windows clock (~15.6 ms) lets a decision and its consumption share a
    ``recorded_at``. Stability is an implementation property nobody wrote down
    in this code. Here every entry is forced to the SAME timestamp, so a
    timestamp-only key provides no order at all -- correctness must come from
    the persisted append sequence, and it must stay CAUSAL (decided before
    consumed), which an id tiebreaker would not guarantee.
    """
    frozen = utc_now()
    monkeypatch.setattr("research_assistant_api.workspace.utc_now", lambda: frozen)

    store = WorkspaceStore()
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv="a,b\n1,2\n")
    record = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
        requested_by="Alice", ttl_minutes=60, requested_by_principal_id="alice",
    )
    store.decide_dataset_approval_request(
        record.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    store.consume_dataset_approval_request(
        record.id, plan_fingerprint=fingerprint, invocation_id="inv-1",
        consumed_by_principal_id="alice",
    )
    store.record_dataset_send_outcome(
        record.id, invocation_id="inv-1", plan_fingerprint=fingerprint,
        delivered=True, actor_principal_id="alice",
    )

    trail = store.dataset_approval_audit()
    # Every entry really does share a timestamp: a timestamp-only sort is no order.
    assert len({entry.recorded_at for entry in trail}) == 1
    # Causal order is preserved anyway, and sequences are strictly increasing.
    assert [entry.action for entry in trail] == ["decided", "consumed", "send_succeeded"]
    sequences = [entry.sequence for entry in trail]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)

    # Repeated reads are identical -- the order does not depend on iteration luck.
    for _ in range(5):
        assert [entry.id for entry in store.dataset_approval_audit()] == [e.id for e in trail]

    # And the outcome lookup still resolves the right entry with no clock signal.
    assert store.dataset_send_outcome(record.id) is DatasetSendOutcome.DELIVERED


def test_audit_order_survives_a_shuffled_underlying_list() -> None:
    """Order must come from the key, not from list position: shuffling the
    backing list must not change the returned trail."""
    import random

    store = WorkspaceStore()
    fingerprint = _plan_fingerprint(store, objective="obj", filename="f.csv", csv="a,b\n1,2\n")
    record = store.create_dataset_approval_request(
        plan_fingerprint=fingerprint, filename="f.csv", objective="obj",
        requested_by="Alice", ttl_minutes=60, requested_by_principal_id="alice",
    )
    store.decide_dataset_approval_request(
        record.id,
        DatasetApprovalDecisionRequest(decision="approved", rationale="Reviewed."),
        _identity("rev-9", ("research-reviewers",)),
    )
    store.consume_dataset_approval_request(
        record.id, plan_fingerprint=fingerprint, invocation_id="inv-1",
        consumed_by_principal_id="alice",
    )
    store.record_dataset_send_outcome(
        record.id, invocation_id="inv-1", plan_fingerprint=fingerprint,
        delivered=False, actor_principal_id="alice",
    )
    expected = [entry.id for entry in store.dataset_approval_audit()]

    rng = random.Random(20260724)
    for _ in range(10):
        rng.shuffle(store._dataset_audit)
        assert [entry.id for entry in store.dataset_approval_audit()] == expected
        assert store.dataset_send_outcome(record.id) is DatasetSendOutcome.FAILED
