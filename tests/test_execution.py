from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
import research_assistant_api.orchestration as execution
from fastapi.testclient import TestClient
from research_assistant_api.app import app

app_module = import_module("research_assistant_api.app")


class RecordingStore:
    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.failed: list[tuple[str, str, str]] = []

    def complete_ingestion(
        self,
        item_id: str,
        run_id: str,
        *,
        evidence_count: int,
        needs_review: bool,
    ) -> object:
        self.completed.append(
            {
                "item_id": item_id,
                "run_id": run_id,
                "evidence_count": evidence_count,
                "needs_review": needs_review,
            }
        )
        return object()

    def fail_ingestion(self, item_id: str, run_id: str, reason: str) -> object:
        self.failed.append((item_id, run_id, reason))
        return object()


def test_execute_library_ingestion_completes_without_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingStore()
    monkeypatch.setattr(
        execution,
        "extract_source",
        lambda _payload: {
            "blob_uri": "https://storage.test/sources/source-1",
            "extracted_manifest_uri": "https://storage.test/sources/manifest.json",
            "chunk_count": 3,
        },
    )
    monkeypatch.setattr(
        execution,
        "index_extracted_source",
        lambda payload: {
            "evidence_manifest_uri": payload["extracted_manifest_uri"],
            "passage_count": 2,
        },
    )

    execution.execute_library_ingestion(  # type: ignore[arg-type]
        store,
        {"source_id": "source-1", "run_id": "run-1", "query": "index"},
    )

    assert store.failed == []
    assert store.completed == [
        {
            "item_id": "source-1",
            "run_id": "run-1",
            "evidence_count": 2,
            "needs_review": True,
        }
    ]


def test_execute_library_ingestion_records_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingStore()

    def fail_extract(_payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("unsupported source")

    monkeypatch.setattr(execution, "extract_source", fail_extract)

    execution.execute_library_ingestion(  # type: ignore[arg-type]
        store,
        {"source_id": "source-1", "run_id": "run-1", "query": "index"},
    )

    assert store.completed == []
    assert store.failed == [("source-1", "run-1", "unsupported source")]


def test_library_ingestion_uses_process_local_background_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        app_module,
        "execute_library_ingestion",
        lambda _store, payload: calls.append(payload),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/library/ingest",
            json={
                "title": "Runtime protocol",
                "kind": "Policy",
                "source": "Workspace upload",
                "access": "internal",
                "license": "Project supplied",
                "description": "A process-local runtime ingestion.",
            },
        )

    assert response.status_code == 200
    assert response.json()["run"]["scheduler_managed"] is False
    assert response.json()["run"]["scheduling_state"] == "not_managed"
    assert calls[0]["workflow_kind"] == "library_ingestion"


def test_local_approval_reaches_terminal_state_without_scheduler() -> None:
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