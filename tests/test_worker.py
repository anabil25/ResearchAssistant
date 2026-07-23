from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from durabletask.task import TaskFailedError
from research_assistant_worker.config import parse_scheduler_settings
from research_assistant_worker.workflows import (
    _canonical_graph_hash,
    execute_workflow_step,
    research_pipeline,
)


@dataclass(frozen=True)
class ScheduledTask:
    kind: str
    name: str
    payload: Any


class FakeContext:
    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.retry_attempts: list[tuple[str, int]] = []

    def set_custom_status(self, status: dict[str, Any]) -> None:
        self.statuses.append(status)

    def call_activity(
        self,
        activity: Any,
        *,
        input: Any,
        retry_policy: Any,
    ) -> ScheduledTask:
        self.retry_attempts.append((activity.__name__, retry_policy.max_number_of_attempts))
        return ScheduledTask("activity", activity.__name__, input)

    def wait_for_external_event(
        self,
        name: str,
        *,
        data_type: type[Any],
    ) -> ScheduledTask:
        return ScheduledTask("event", name, data_type)


def test_local_scheduler_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DURABLE_TASK_SCHEDULER_CONNECTION_STRING", raising=False)

    settings = parse_scheduler_settings()

    assert settings.host_address == "localhost:8080"
    assert settings.task_hub == "default"
    assert settings.secure_channel is False


def test_managed_scheduler_connection_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        (
            "Endpoint=https://scheduler.eastus2.durabletask.io;"
            "TaskHub=research;Authentication=ManagedIdentity;ClientID=client-123"
        ),
    )

    settings = parse_scheduler_settings()

    assert settings.host_address == "scheduler.eastus2.durabletask.io"
    assert settings.task_hub == "research"
    assert settings.secure_channel is True
    assert settings.managed_identity_client_id == "client-123"


def test_research_pipeline_waits_for_approval() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-1",
            "source_id": "source-1",
            "query": "compare evidence",
            "require_approval": True,
        },
    )

    first = next(pipeline)
    assert first.name == "ingest_source"
    second = pipeline.send({"source_id": "source-1", "blob_uri": "blob://source", "status": "verified"})
    assert second.name == "retrieve_evidence"
    third = pipeline.send(
        {
            "query": "compare evidence",
            "evidence_manifest_uri": "blob://evidence",
            "passage_count": 3,
        }
    )
    assert third.name == "synthesize_artifact"
    fourth = pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 3,
            "status": "needs_review",
        }
    )
    assert fourth.name == "verify_artifact"
    approval = pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 3,
            "status": "needs_review",
            "verification": "citation_references_resolved",
        }
    )
    assert approval == ScheduledTask("event", "review_decision", dict)

    completion = pipeline.send(
        {
            "approved": True,
            "approval_id": "approval-1",
            "idempotency_key": "grant-run-1",
        }
    )
    assert completion.name == "complete_run"
    assert completion.payload["approved"] is True

    try:
        pipeline.send({**completion.payload, "status": "completed"})
    except StopIteration as completed:
        assert completed.value["status"] == "completed"
    else:
        raise AssertionError("Pipeline did not complete after approval")

    assert context.statuses[-1] == {"step": "complete", "progress": 100}


def test_library_ingestion_completes_after_indexing() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-ingest-1",
            "source_id": "source-1",
            "query": "ingest source",
            "workflow_kind": "library_ingestion",
            "require_approval": False,
        },
    )

    first = next(pipeline)
    assert first.name == "ingest_source"
    second = pipeline.send(
        {
            "source_id": "source-1",
            "blob_uri": "https://storage/source.txt",
            "extracted_manifest_uri": "https://storage/manifest.json",
            "status": "extracted",
        }
    )
    assert second.name == "retrieve_evidence"

    completion = pipeline.send(
        {
            "query": "ingest source",
            "evidence_manifest_uri": "https://storage/manifest.json",
            "passage_count": 4,
        }
    )
    assert completion.name == "complete_run"

    try:
        pipeline.send({**completion.payload, "status": "completed"})
    except StopIteration as completed:
        assert completed.value["status"] == "completed"
        assert completed.value["passage_count"] == 4
    else:
        raise AssertionError("Library ingestion did not complete after indexing")

    assert context.statuses[-1] == {"step": "complete", "progress": 100}


def test_automation_step_persists_the_next_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[dict[str, Any]] = []
    graph: dict[str, Any] = {
        "template_id": "approval-test",
        "trigger": "Manual",
        "steps": [
            {
                "id": "prepare",
                "label": "Prepare evidence",
                "kind": "activity",
                "depends_on": [],
                "retry_limit": 1,
                "approval_required": False,
            },
            {
                "id": "release",
                "label": "Release approved package",
                "kind": "external_action",
                "depends_on": ["prepare"],
                "retry_limit": 1,
                "approval_required": True,
            },
        ],
    }
    graph["hash"] = _canonical_graph_hash(graph)
    monkeypatch.setattr(
        "research_assistant_worker.workflows.set_run_state",
        lambda _payload, **state: states.append(state),
    )

    result = execute_workflow_step(
        None,  # type: ignore[arg-type]
        {
            "run_id": "run-approval",
            "workflow_graph": graph,
            "step": graph["steps"][0],
            "step_index": 0,
            "step_count": 2,
        },
    )

    assert result["status"] == "completed"
    assert states == [
        {
            "status": "running",
            "progress": 48,
            "current_stage": "Prepare evidence",
        },
        {
            "status": "waiting_for_approval",
            "progress": 80,
            "current_stage": "Release approved package",
        },
    ]


def test_pipeline_persists_terminal_failure_after_activity_retries() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-failed",
            "source_id": "source-failed",
            "query": "ingest source",
            "workflow_kind": "library_ingestion",
            "require_approval": False,
        },
    )

    first = next(pipeline)
    assert first.name == "ingest_source"
    finalizer = pipeline.throw(TaskFailedError("Extraction failed", ValueError("invalid document")))

    assert finalizer.name == "complete_run"
    assert finalizer.payload["terminal_status"] == "failed"
    try:
        pipeline.send({**finalizer.payload, "status": "failed"})
    except StopIteration as completed:
        assert completed.value["status"] == "failed"
    else:
        raise AssertionError("Failed workflow did not terminate")
    assert context.statuses[-1] == {"step": "failed", "progress": 100}


def test_automation_pipeline_executes_hashed_graph_and_waits_at_gate() -> None:
    context = FakeContext()
    graph: dict[str, Any] = {
        "version": "2.0",
        "template_id": "minimal-v2",
        "trigger": "Manual",
        "steps": [
            {
                "id": "prepare",
                "label": "Prepare",
                "kind": "activity",
                "depends_on": [],
                "retry_limit": 1,
                "approval_required": False,
            },
            {
                "id": "release",
                "label": "Release",
                "kind": "external_action",
                "depends_on": ["prepare"],
                "retry_limit": 1,
                "approval_required": True,
            },
        ],
    }
    graph["hash"] = _canonical_graph_hash(graph)
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-automation",
            "source_id": "workspace-request",
            "query": "execute graph",
            "workflow_kind": "automation_graph",
            "workflow_graph": graph,
        },
    )

    prepare = next(pipeline)
    assert prepare.name == "execute_workflow_step"
    assert prepare.payload["step"]["id"] == "prepare"
    approval = pipeline.send({"step_id": "prepare", "status": "completed"})
    assert approval == ScheduledTask("event", "review_decision", dict)
    release = pipeline.send(
        {
            "approved": True,
            "approval_id": "approval-1",
            "idempotency_key": "automation-1",
        }
    )
    assert release.name == "execute_workflow_step"
    assert release.payload["step"]["id"] == "release"
    completion = pipeline.send({"step_id": "release", "status": "simulated_no_external_write"})
    assert completion.name == "complete_run"
    try:
        pipeline.send({**completion.payload, "status": "completed"})
    except StopIteration as completed:
        assert completed.value["status"] == "completed"
    else:
        raise AssertionError("Automation workflow did not complete")
    step_attempts = [attempts for name, attempts in context.retry_attempts if name == "execute_workflow_step"]
    assert step_attempts == [2, 2]
