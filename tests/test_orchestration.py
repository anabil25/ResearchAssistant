from __future__ import annotations

from typing import Any

import grpc  # type: ignore[import-untyped]
import pytest
import research_assistant_api.orchestration as orchestration
from fastapi.testclient import TestClient
from research_assistant_api.app import _reconcile_pending_runs, app
from research_assistant_api.config import Settings
from research_assistant_api.orchestration import (
    DurableRunScheduler,
    InMemoryRunScheduler,
    RunSchedulingError,
    build_run_scheduler,
)


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


def test_reconciliation_retains_uncertain_state_when_scheduler_still_fails() -> None:
    class AmbiguousScheduler(RecordingScheduler):
        def schedule(
            self,
            *,
            instance_id: str,
            payload: dict[str, Any],
        ) -> str:
            raise RunSchedulingError("Ambiguous scheduler response.", ambiguous=True)

    class FailingScheduler(RecordingScheduler):
        def schedule(
            self,
            *,
            instance_id: str,
            payload: dict[str, Any],
        ) -> str:
            raise RunSchedulingError("Scheduler still unavailable.")

    with TestClient(app) as client:
        app.state.scheduler = AmbiguousScheduler()
        response = client.post(
            "/api/studios/grant/run",
            json={"objective": "Create a grant run that stays uncertain"},
        )
        assert response.status_code == 503
        stored = next(
            item
            for item in client.get("/api/runs").json()
            if item["current_stage"] == "Scheduling reconciliation required"
        )

        _reconcile_pending_runs(app.state.workspace, FailingScheduler())
        retained = client.get(f"/api/runs/{stored['id']}").json()

    assert retained["scheduling_state"] == "uncertain"


def test_in_memory_scheduler_noops() -> None:
    scheduler = InMemoryRunScheduler()

    assert scheduler.schedule(instance_id="run-1", payload={"query": "x"}) == "run-1"
    # approve/close are no-ops that must complete without raising; their
    # None-returning contract is enforced statically, not via assertion.
    scheduler.approve(
        instance_id="run-1",
        approval_id="approval-1",
        idempotency_key="idem-1",
        approved=True,
    )
    scheduler.close()


def test_durable_scheduler_schedules_and_reuses_existing_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    credential = object()

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs
            self.state_calls = 0

        def get_orchestration_state(
            self,
            instance_id: str,
            *,
            fetch_payloads: bool,
        ) -> object | None:
            captured.setdefault("state_calls", []).append((instance_id, fetch_payloads))
            self.state_calls += 1
            return None if self.state_calls == 1 else object()

        def schedule_new_orchestration(
            self,
            name: str,
            *,
            input: dict[str, Any],
            instance_id: str,
            tags: dict[str, str],
        ) -> str:
            captured["schedule"] = {
                "name": name,
                "input": input,
                "instance_id": instance_id,
                "tags": tags,
            }
            return "scheduled-instance"

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(orchestration, "DurableTaskSchedulerClient", FakeClient)
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=credential,  # type: ignore[arg-type]
        secure_channel=True,
    )
    payload = {
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "capability": "grant",
    }

    first = scheduler.schedule(instance_id="instance-1", payload=payload)
    second = scheduler.schedule(instance_id="instance-1", payload=payload)
    scheduler.close()

    assert first == "scheduled-instance"
    assert second == "instance-1"
    assert captured["init"] == {
        "host_address": "scheduler.example",
        "taskhub": "hub",
        "token_credential": credential,
        "secure_channel": True,
    }
    assert captured["schedule"]["tags"] == {
        "tenantId": "tenant-1",
        "projectId": "project-1",
        "capability": "grant",
    }
    assert captured["closed"] is True


def test_durable_scheduler_reports_precheck_rpc_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRpcError(Exception):
        pass

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def get_orchestration_state(self, *_args: Any, **_kwargs: Any) -> object | None:
            raise FakeRpcError("boom")

        def close(self) -> None:
            return None

    monkeypatch.setattr(grpc, "RpcError", FakeRpcError)
    monkeypatch.setattr(orchestration, "DurableTaskSchedulerClient", FakeClient)
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    with pytest.raises(
        RunSchedulingError,
        match="could not be checked before scheduling",
    ) as exc_info:
        scheduler.schedule(
            instance_id="instance-1",
            payload={
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "capability": "grant",
            },
        )

    assert exc_info.value.ambiguous is True


def test_durable_scheduler_reconciles_or_reports_ambiguous_schedule_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRpcError(Exception):
        pass

    monkeypatch.setattr(grpc, "RpcError", FakeRpcError)

    class ExistingAfterFailureClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        def get_orchestration_state(self, *_args: Any, **_kwargs: Any) -> object | None:
            self.calls += 1
            return None if self.calls == 1 else object()

        def schedule_new_orchestration(self, *_args: Any, **_kwargs: Any) -> str:
            raise FakeRpcError("conflict")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        orchestration,
        "DurableTaskSchedulerClient",
        ExistingAfterFailureClient,
    )
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    assert (
        scheduler.schedule(
            instance_id="instance-1",
            payload={
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "capability": "grant",
            },
        )
        == "instance-1"
    )

    class MissingAfterFailureClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        def get_orchestration_state(self, *_args: Any, **_kwargs: Any) -> object | None:
            self.calls += 1
            return None

        def schedule_new_orchestration(self, *_args: Any, **_kwargs: Any) -> str:
            raise FakeRpcError("conflict")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        orchestration,
        "DurableTaskSchedulerClient",
        MissingAfterFailureClient,
    )
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    with pytest.raises(RunSchedulingError, match="was not scheduled") as exc_info:
        scheduler.schedule(
            instance_id="instance-2",
            payload={
                "tenant_id": "tenant-2",
                "project_id": "project-2",
                "capability": "matching",
            },
        )

    assert exc_info.value.ambiguous is True

    class ReconciliationFailureClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.calls = 0

        def get_orchestration_state(self, *_args: Any, **_kwargs: Any) -> object | None:
            self.calls += 1
            if self.calls == 1:
                return None
            raise FakeRpcError("reconciliation failed")

        def schedule_new_orchestration(self, *_args: Any, **_kwargs: Any) -> str:
            raise FakeRpcError("conflict")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        orchestration,
        "DurableTaskSchedulerClient",
        ReconciliationFailureClient,
    )
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    with pytest.raises(
        RunSchedulingError,
        match="ambiguous scheduling result",
    ) as ambiguous:
        scheduler.schedule(
            instance_id="instance-3",
            payload={
                "tenant_id": "tenant-3",
                "project_id": "project-3",
                "capability": "literature",
            },
        )

    assert ambiguous.value.ambiguous is True


def test_durable_scheduler_approval_and_close_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRpcError(Exception):
        pass

    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def raise_orchestration_event(self, **kwargs: Any) -> None:
            captured["event"] = kwargs

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(grpc, "RpcError", FakeRpcError)
    monkeypatch.setattr(orchestration, "DurableTaskSchedulerClient", FakeClient)
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    scheduler.approve(
        instance_id="instance-1",
        approval_id="approval-1",
        idempotency_key="idem-1",
        approved=True,
    )
    scheduler.close()

    assert captured["event"] == {
        "instance_id": "instance-1",
        "event_name": "review_decision",
        "data": {
            "approved": True,
            "approval_id": "approval-1",
            "idempotency_key": "idem-1",
        },
    }
    assert captured["closed"] is True

    class FailingClient(FakeClient):
        def raise_orchestration_event(self, **kwargs: Any) -> None:
            del kwargs
            raise FakeRpcError("delivery failed")

    monkeypatch.setattr(orchestration, "DurableTaskSchedulerClient", FailingClient)
    scheduler = DurableRunScheduler(
        host_address="scheduler.example",
        task_hub="hub",
        credential=object(),  # type: ignore[arg-type]
        secure_channel=True,
    )

    with pytest.raises(
        RunSchedulingError,
        match="Approval event could not be delivered",
    ):
        scheduler.approve(
            instance_id="instance-2",
            approval_id="approval-2",
            idempotency_key="idem-2",
            approved=False,
        )


def test_scheduler_credential_selection_and_connection_string_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_calls: list[str | None] = []
    default_calls = 0

    class FakeManagedIdentityCredential:
        def __init__(self, *, client_id: str | None = None) -> None:
            managed_calls.append(client_id)

    class FakeDefaultAzureCredential:
        def __init__(self) -> None:
            nonlocal default_calls
            default_calls += 1

    monkeypatch.setattr(
        orchestration,
        "ManagedIdentityCredential",
        FakeManagedIdentityCredential,
    )
    monkeypatch.setattr(
        orchestration,
        "DefaultAzureCredential",
        FakeDefaultAzureCredential,
    )

    assert isinstance(
        orchestration._credential("managed-client"),
        FakeManagedIdentityCredential,
    )
    assert isinstance(orchestration._credential(None), FakeDefaultAzureCredential)
    assert managed_calls == ["managed-client"]
    assert default_calls == 1

    assert isinstance(build_run_scheduler(Settings()), InMemoryRunScheduler)
    with pytest.raises(ValueError, match="missing Endpoint"):
        build_run_scheduler(
            Settings(durable_task_connection_string="TaskHub=research")
        )
    with pytest.raises(ValueError, match="invalid Endpoint"):
        build_run_scheduler(
            Settings(durable_task_connection_string="Endpoint=https://;TaskHub=research")
        )


def test_build_run_scheduler_configures_https_and_http_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials: list[str | None] = []

    class FakeCredential:
        pass

    def fake_credential(client_id: str | None) -> FakeCredential:
        credentials.append(client_id)
        return FakeCredential()

    monkeypatch.setattr(orchestration, "_credential", fake_credential)
    created: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr(orchestration, "DurableTaskSchedulerClient", FakeClient)

    secure = build_run_scheduler(
        Settings(
            durable_task_connection_string=(
                "Endpoint=https://scheduler.example;;TaskHub=research;ClientId=client-a;Ignored"
            )
        )
    )
    insecure = build_run_scheduler(
        Settings(
            durable_task_connection_string="Endpoint=http://localhost:4001;TaskHub=lab",
            managed_identity_client_id="fallback-client",
        )
    )

    assert isinstance(secure, DurableRunScheduler)
    assert isinstance(insecure, DurableRunScheduler)
    assert credentials == ["client-a", "fallback-client"]
    assert created[0]["host_address"] == "scheduler.example"
    assert created[0]["taskhub"] == "research"
    assert created[0]["secure_channel"] is True
    assert isinstance(created[0]["token_credential"], FakeCredential)
    assert created[1] == {
        "host_address": "localhost:4001",
        "taskhub": "lab",
        "token_credential": None,
        "secure_channel": False,
    }
