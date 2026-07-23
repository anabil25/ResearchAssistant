from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from research_assistant_api.app import _reconcile_pending_runs, app
from research_assistant_api.orchestration import RunSchedulingError


class RecordingScheduler:
    configured = True

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, dict[str, Any]]] = []
        self.decisions: list[tuple[str, str, str, bool]] = []
        self.closed = False

    def schedule(
        self,
        *,
        instance_id: str,
        payload: dict[str, Any],
    ) -> str:
        self.scheduled.append((instance_id, payload))
        return instance_id

    def approve(
        self,
        *,
        instance_id: str,
        approval_id: str,
        idempotency_key: str,
        approved: bool,
    ) -> None:
        self.decisions.append((instance_id, approval_id, idempotency_key, approved))

    def close(self) -> None:
        self.closed = True


def test_studio_run_and_approval_use_same_durable_instance() -> None:
    scheduler = RecordingScheduler()
    with TestClient(app) as client:
        app.state.scheduler = scheduler
        response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Build a compliant infrastructure grant package"},
        )
        assert response.status_code == 200
        run = response.json()["run"]

        stored = next(item for item in client.get("/api/runs").json() if item["id"] == run["id"])
        approval = next(item for item in client.get("/api/approvals").json() if item["run_id"] == run["id"])
        decision = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={
                "decision": "approved",
                "rationale": "The exact package and destination were reviewed.",
            },
        )
        retry = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={
                "decision": "approved",
                "rationale": "Retry with the same decision.",
            },
        )
        conflict = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={
                "decision": "rejected",
                "rationale": "A conflicting retry must not emit an event.",
            },
        )

    assert stored["scheduler_managed"] is True
    assert stored["scheduling_state"] == "scheduled"
    assert len(scheduler.scheduled) == 1
    scheduled_instance, scheduled_payload = scheduler.scheduled[0]
    assert scheduled_instance == run["durable_instance_id"]
    assert scheduled_payload["run_id"] == run["id"]
    assert scheduled_payload["source_id"] == "grant-open-science"
    assert scheduled_payload["tenant_id"] == "demo"
    assert scheduled_payload["project_id"] == "demo-project"
    assert scheduled_payload["capability"] == "grant"
    assert scheduled_payload["require_approval"] is True
    assert scheduled_payload["workflow_kind"] == "studio_run"
    assert scheduled_payload["group_ids"] == [
        "researchers",
        "grant-reviewers",
        "research-admins",
    ]
    assert decision.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["event_delivery"] == "delivered"
    assert conflict.status_code == 409
    assert stored["stages"][-1]["status"] == "waiting_for_approval"
    assert all(stage["status"] == "completed" for stage in stored["stages"][:-1])
    assert scheduler.decisions == [
        (
            run["durable_instance_id"],
            approval["id"],
            approval["idempotency_key"],
            True,
        )
    ]


def test_library_ingestion_schedules_the_durable_pipeline() -> None:
    scheduler = RecordingScheduler()
    with TestClient(app) as client:
        app.state.scheduler = scheduler
        response = client.post(
            "/api/library/ingest",
            json={
                "title": "Runtime protocol",
                "kind": "Policy",
                "source": "Workspace upload",
                "access": "internal",
                "license": "Project supplied",
                "description": "A governed runtime ingestion.",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["scheduler_managed"] is True
    assert scheduler.scheduled[0][0] == payload["run"]["durable_instance_id"]
    assert scheduler.scheduled[0][1]["workflow_kind"] == "library_ingestion"


def test_local_approval_reaches_a_terminal_run_state() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Build a locally reviewed grant package"},
        )
        run = response.json()["run"]
        approval = next(
            item
            for item in client.get("/api/approvals").json()
            if item["run_id"] == run["id"]
        )
        decision = client.post(
            f"/api/approvals/{approval['id']}/decision",
            json={
                "decision": "approved",
                "rationale": "The exact local artifact was reviewed.",
            },
        )
        stored = client.get(f"/api/runs/{run['id']}").json()

    assert decision.status_code == 200
    assert decision.json()["event_delivery"] == "not_required"
    assert stored["status"] == "completed"
    assert stored["progress"] == 100
    assert stored["completed_at"] is not None
    assert all(stage["status"] == "completed" for stage in stored["stages"])


def test_ambiguous_schedule_is_persisted_and_reconciled() -> None:
    class AmbiguousScheduler(RecordingScheduler):
        def schedule(
            self,
            *,
            instance_id: str,
            payload: dict[str, Any],
        ) -> str:
            raise RunSchedulingError("Ambiguous scheduler response.", ambiguous=True)

    ambiguous = AmbiguousScheduler()
    with TestClient(app) as client:
        app.state.scheduler = ambiguous
        response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Create a grant run that needs reconciliation"},
        )
        assert response.status_code == 503
        stored = next(
            item
            for item in client.get("/api/runs").json()
            if item["title"] == "Evidence-bounded specific aims and requirement map"
        )
        assert stored["scheduling_state"] == "uncertain"
        assert stored["status"] == "planned"

        recovered = RecordingScheduler()
        _reconcile_pending_runs(app.state.workspace, recovered)
        reconciled = client.get(f"/api/runs/{stored['id']}").json()

    assert recovered.scheduled
    assert reconciled["scheduling_state"] == "scheduled"
    assert reconciled["status"] == "waiting_for_approval"
