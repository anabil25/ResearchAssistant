from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import research_assistant_worker.workflows as workflows
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


def _automation_graph(*steps: dict[str, Any], graph_hash: str | None = None) -> dict[str, Any]:
    graph = {
        "template_id": "automation-template",
        "trigger": "Manual",
        "steps": list(steps),
    }
    graph["hash"] = graph_hash or _canonical_graph_hash(graph)
    return graph


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


def test_scheduler_parser_skips_empty_parts_and_uses_env_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "fallback-client")
    monkeypatch.setenv(
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        "Endpoint=https://scheduler.example.test;TaskHub=research;;ClientID=;BrokenPart",
    )

    settings = parse_scheduler_settings()

    assert settings.host_address == "scheduler.example.test"
    assert settings.task_hub == "research"
    assert settings.managed_identity_client_id == "fallback-client"


def test_scheduler_parser_requires_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        "TaskHub=research;ClientID=client-123;;",
    )

    with pytest.raises(ValueError, match="missing Endpoint"):
        parse_scheduler_settings()


def test_activity_wrappers_delegate_to_ingestion_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_extract(payload: dict[str, Any]) -> dict[str, str]:
        seen.append(("extract", payload))
        return {"status": "extracted"}

    def fake_index(payload: dict[str, Any]) -> dict[str, str]:
        seen.append(("index", payload))
        return {"status": "indexed"}

    monkeypatch.setattr(workflows, "extract_source", fake_extract)
    monkeypatch.setattr(workflows, "index_extracted_source", fake_index)

    extract_result = workflows.ingest_source(None, {"source_id": "source-1"})  # type: ignore[arg-type]
    index_result = workflows.retrieve_evidence(None, {"query": "collect"})  # type: ignore[arg-type]

    assert extract_result == {"status": "extracted"}
    assert index_result == {"status": "indexed"}
    assert seen == [
        ("extract", {"source_id": "source-1"}),
        ("index", {"query": "collect"}),
    ]


def test_synthesize_and_verify_artifact_update_run_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[dict[str, Any]] = []
    monkeypatch.setattr(
        workflows,
        "set_run_state",
        lambda _payload, **state: states.append(state),
    )

    artifact = workflows.synthesize_artifact(
        None,  # type: ignore[arg-type]
        {"run_id": "run-1", "passage_count": "3"},
    )
    waiting = workflows.verify_artifact(
        None,  # type: ignore[arg-type]
        {"artifact_uri": artifact["artifact_uri"], "require_approval": True},
    )
    finalized = workflows.verify_artifact(
        None,  # type: ignore[arg-type]
        {"artifact_uri": artifact["artifact_uri"], "require_approval": False, "extra": "value"},
    )

    assert artifact == {
        "artifact_uri": "blob://artifacts/run-1.md",
        "citation_count": 3,
        "status": "needs_review",
    }
    assert waiting["verification"] == "citation_references_resolved"
    assert finalized["extra"] == "value"
    assert states == [
        {
            "status": "running",
            "progress": 70,
            "current_stage": "Synthesize artifact",
        },
        {
            "status": "waiting_for_approval",
            "progress": 90,
            "current_stage": "Human review",
        },
        {
            "status": "running",
            "progress": 92,
            "current_stage": "Finalize run",
        },
    ]


def test_complete_run_sets_completed_blocked_and_failed_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_states: list[dict[str, Any]] = []
    library_states: list[dict[str, Any]] = []
    monkeypatch.setattr(
        workflows,
        "set_run_state",
        lambda _payload, **state: run_states.append(state),
    )
    monkeypatch.setattr(
        workflows,
        "set_library_state",
        lambda _payload, **state: library_states.append(state),
    )

    completed = workflows.complete_run(None, {"approved": True})  # type: ignore[arg-type]
    blocked = workflows.complete_run(None, {"approved": False})  # type: ignore[arg-type]
    failed = workflows.complete_run(
        None,  # type: ignore[arg-type]
        {"workflow_kind": "library_ingestion", "terminal_status": "failed"},
    )

    assert completed["status"] == "completed"
    assert blocked["status"] == "blocked"
    assert failed["status"] == "failed"
    assert run_states == [
        {
            "status": "completed",
            "progress": 100,
            "current_stage": "Complete",
            "completed": True,
        },
        {
            "status": "blocked",
            "progress": 100,
            "current_stage": "Approval rejected",
            "completed": True,
        },
        {
            "status": "failed",
            "progress": 100,
            "current_stage": "Workflow failed",
            "completed": True,
        },
    ]
    assert library_states == [{"status": "blocked"}]


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


def test_research_pipeline_completes_without_human_approval() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-auto-complete",
            "source_id": "source-1",
            "query": "compare evidence",
            "require_approval": False,
        },
    )

    assert next(pipeline).name == "ingest_source"
    assert pipeline.send(
        {"source_id": "source-1", "blob_uri": "blob://source", "status": "verified"}
    ).name == "retrieve_evidence"
    assert pipeline.send(
        {
            "query": "compare evidence",
            "evidence_manifest_uri": "blob://evidence",
            "passage_count": 3,
        }
    ).name == "synthesize_artifact"
    assert pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 3,
            "status": "needs_review",
        }
    ).name == "verify_artifact"

    completion = pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 3,
            "status": "needs_review",
            "verification": "citation_references_resolved",
        }
    )

    assert completion.name == "complete_run"
    assert completion.payload["approved"] is True
    try:
        pipeline.send({**completion.payload, "status": "completed"})
    except StopIteration as completed:
        assert completed.value["status"] == "completed"
    else:
        raise AssertionError("Automatic completion did not terminate the workflow")
    assert context.statuses[-1] == {"step": "complete", "progress": 100}


def test_automation_step_persists_the_next_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states: list[dict[str, Any]] = []
    graph = _automation_graph(
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
    )
    monkeypatch.setattr(
        workflows,
        "set_run_state",
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


def test_execute_workflow_step_handles_non_gated_and_last_external_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _automation_graph(
        {
            "id": "alpha",
            "label": "Alpha",
            "kind": "activity",
            "depends_on": [],
            "retry_limit": 0,
            "approval_required": False,
        },
        {
            "id": "beta",
            "label": "Beta",
            "kind": "external_action",
            "depends_on": ["alpha"],
            "retry_limit": 0,
            "approval_required": False,
        },
    )
    states: list[dict[str, Any]] = []
    monkeypatch.setattr(
        workflows,
        "set_run_state",
        lambda _payload, **state: states.append(state),
    )

    first = execute_workflow_step(
        None,  # type: ignore[arg-type]
        {
            "workflow_graph": graph,
            "step": graph["steps"][0],
            "step_index": 0,
            "step_count": 2,
        },
    )
    second = execute_workflow_step(
        None,  # type: ignore[arg-type]
        {
            "workflow_graph": graph,
            "step": graph["steps"][1],
            "step_index": 1,
            "step_count": 2,
        },
    )

    assert first["status"] == "completed"
    assert second["status"] == "simulated_no_external_write"
    assert states == [
        {"status": "running", "progress": 48, "current_stage": "Alpha"},
        {"status": "running", "progress": 85, "current_stage": "Beta"},
    ]


def test_topological_steps_reject_cycles() -> None:
    with pytest.raises(ValueError, match="cyclic or has unresolved dependencies"):
        workflows._topological_steps(
            {
                "steps": [
                    {
                        "id": "a",
                        "depends_on": ["b"],
                    },
                    {
                        "id": "b",
                        "depends_on": ["a"],
                    },
                ]
            }
        )


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


def test_pipeline_persists_failure_when_retrieval_activity_errors() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-retrieve-failed",
            "source_id": "source-1",
            "query": "collect evidence",
            "require_approval": False,
        },
    )

    first = next(pipeline)
    assert first.name == "ingest_source"
    second = pipeline.send({"source_id": "source-1", "blob_uri": "blob://source", "status": "verified"})
    assert second.name == "retrieve_evidence"

    finalizer = pipeline.throw(TaskFailedError("Retrieval failed", RuntimeError("search unavailable")))

    assert finalizer.name == "complete_run"
    assert finalizer.payload["terminal_status"] == "failed"
    assert "Retrieval failed" in finalizer.payload["failure"]
    try:
        pipeline.send({**finalizer.payload, "status": "failed"})
    except StopIteration as completed:
        assert completed.value["status"] == "failed"
    else:
        raise AssertionError("Retrieval failure did not terminate the workflow")
    assert context.statuses[-1] == {"step": "failed", "progress": 100}


def test_research_pipeline_rejects_human_approval() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-rejected",
            "source_id": "source-1",
            "query": "compare evidence",
            "require_approval": True,
        },
    )

    assert next(pipeline).name == "ingest_source"
    assert pipeline.send(
        {"source_id": "source-1", "blob_uri": "blob://source", "status": "verified"}
    ).name == "retrieve_evidence"
    assert pipeline.send(
        {
            "query": "compare evidence",
            "evidence_manifest_uri": "blob://evidence",
            "passage_count": 2,
        }
    ).name == "synthesize_artifact"
    assert pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 2,
            "status": "needs_review",
        }
    ).name == "verify_artifact"
    approval = pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 2,
            "status": "needs_review",
            "verification": "citation_references_resolved",
        }
    )

    assert approval == ScheduledTask("event", "review_decision", dict)
    finalizer = pipeline.send({"approved": False, "reason": "missing citation"})

    assert finalizer.name == "complete_run"
    assert finalizer.payload["approved"] is False
    try:
        pipeline.send({**finalizer.payload, "status": "blocked"})
    except StopIteration as completed:
        assert completed.value["status"] == "blocked"
    else:
        raise AssertionError("Approval rejection did not terminate the workflow")
    assert context.statuses[-1] == {"step": "complete", "progress": 100}


def test_pipeline_persists_failure_when_verification_activity_errors() -> None:
    context = FakeContext()
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-verify-failed",
            "source_id": "source-1",
            "query": "collect evidence",
            "require_approval": False,
        },
    )

    assert next(pipeline).name == "ingest_source"
    assert pipeline.send(
        {"source_id": "source-1", "blob_uri": "blob://source", "status": "verified"}
    ).name == "retrieve_evidence"
    assert pipeline.send(
        {
            "query": "collect evidence",
            "evidence_manifest_uri": "blob://evidence",
            "passage_count": 2,
        }
    ).name == "synthesize_artifact"
    verification = pipeline.send(
        {
            "artifact_uri": "blob://artifact",
            "citation_count": 2,
            "status": "needs_review",
        }
    )
    assert verification.name == "verify_artifact"

    finalizer = pipeline.throw(TaskFailedError("Verification failed", RuntimeError("citation missing")))

    assert finalizer.name == "complete_run"
    assert finalizer.payload["terminal_status"] == "failed"
    assert "Verification failed" in finalizer.payload["failure"]
    try:
        pipeline.send({**finalizer.payload, "status": "failed"})
    except StopIteration as completed:
        assert completed.value["status"] == "failed"
    else:
        raise AssertionError("Verification failure did not terminate the workflow")
    assert context.statuses[-1] == {"step": "failed", "progress": 100}


def test_automation_pipeline_executes_hashed_graph_and_waits_at_gate() -> None:
    context = FakeContext()
    graph = _automation_graph(
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
    )
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


def test_automation_pipeline_fails_when_graph_hash_does_not_match() -> None:
    context = FakeContext()
    graph = _automation_graph(
        {
            "id": "prepare",
            "label": "Prepare",
            "kind": "activity",
            "depends_on": [],
            "retry_limit": 0,
            "approval_required": False,
        },
        graph_hash="bad-hash",
    )
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-hash-failed",
            "source_id": "source-1",
            "query": "execute graph",
            "workflow_kind": "automation_graph",
            "workflow_graph": graph,
        },
    )

    finalizer = next(pipeline)

    assert finalizer.name == "complete_run"
    assert finalizer.payload["terminal_status"] == "failed"
    assert finalizer.payload["failure"] == "Workflow graph hash mismatch"
    try:
        pipeline.send({**finalizer.payload, "status": "failed"})
    except StopIteration as completed:
        assert completed.value["status"] == "failed"
    else:
        raise AssertionError("Hash mismatch did not terminate the workflow")


def test_automation_pipeline_stops_when_approval_is_rejected() -> None:
    context = FakeContext()
    graph = _automation_graph(
        {
            "id": "review",
            "label": "Review release",
            "kind": "external_action",
            "depends_on": [],
            "retry_limit": 0,
            "approval_required": True,
        }
    )
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-automation-rejected",
            "source_id": "source-1",
            "query": "execute graph",
            "workflow_kind": "automation_graph",
            "workflow_graph": graph,
        },
    )

    approval = next(pipeline)

    assert approval == ScheduledTask("event", "review_decision", dict)
    finalizer = pipeline.send({"approved": False, "reason": "denied"})

    assert finalizer.name == "complete_run"
    assert finalizer.payload["approved"] is False
    try:
        pipeline.send({**finalizer.payload, "status": "blocked"})
    except StopIteration as completed:
        assert completed.value["status"] == "blocked"
    else:
        raise AssertionError("Automation approval rejection did not terminate")
    assert context.statuses == [
        {
            "step": "review",
            "progress": 80,
            "state": "waiting_for_approval",
        }
    ]


def test_automation_pipeline_persists_failed_step_execution() -> None:
    context = FakeContext()
    graph = _automation_graph(
        {
            "id": "prepare",
            "label": "Prepare",
            "kind": "activity",
            "depends_on": [],
            "retry_limit": 2,
            "approval_required": False,
        }
    )
    pipeline = research_pipeline(
        context,  # type: ignore[arg-type]
        {
            "run_id": "run-step-failed",
            "source_id": "source-1",
            "query": "execute graph",
            "workflow_kind": "automation_graph",
            "workflow_graph": graph,
        },
    )

    step = next(pipeline)
    assert step.name == "execute_workflow_step"

    finalizer = pipeline.throw(TaskFailedError("step failed", RuntimeError("network")))

    assert finalizer.name == "complete_run"
    assert finalizer.payload["terminal_status"] == "failed"
    assert "step failed" in finalizer.payload["failure"]
    try:
        pipeline.send({**finalizer.payload, "status": "failed"})
    except StopIteration as completed:
        assert completed.value["status"] == "failed"
    else:
        raise AssertionError("Step failure did not terminate the automation workflow")
