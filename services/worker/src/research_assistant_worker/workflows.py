from __future__ import annotations

import json
from collections.abc import Generator
from datetime import timedelta
from hashlib import sha256
from typing import Any

from durabletask.task import (
    ActivityContext,
    OrchestrationContext,
    RetryPolicy,
    TaskFailedError,
)

from research_assistant_worker.ingestion import (
    extract_source,
    index_extracted_source,
    set_library_state,
    set_run_state,
)


def ingest_source(_context: ActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    return extract_source(payload)


def retrieve_evidence(
    _context: ActivityContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return index_extracted_source(payload)


def synthesize_artifact(
    _context: ActivityContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    set_run_state(
        payload,
        status="running",
        progress=70,
        current_stage="Synthesize artifact",
    )
    return {
        "artifact_uri": f"blob://artifacts/{payload['run_id']}.md",
        "citation_count": int(payload["passage_count"]),
        "status": "needs_review",
    }


def verify_artifact(
    _context: ActivityContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    waiting = bool(payload.get("require_approval", True))
    set_run_state(
        payload,
        status="waiting_for_approval" if waiting else "running",
        progress=90 if waiting else 92,
        current_stage="Human review" if waiting else "Finalize run",
    )
    return {
        **payload,
        "verification": "citation_references_resolved",
    }


def complete_run(_context: ActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    terminal_status = payload.get("terminal_status")
    if isinstance(terminal_status, str):
        status = terminal_status
    else:
        approved = bool(payload.get("approved", True))
        status = "completed" if approved else "blocked"
    set_run_state(
        payload,
        status=status,
        progress=100,
        current_stage=(
            "Complete" if status == "completed" else "Approval rejected" if status == "blocked" else "Workflow failed"
        ),
        completed=True,
    )
    if payload.get("workflow_kind") == "library_ingestion" and status == "failed":
        set_library_state(payload, status="blocked")
    return {**payload, "status": status}


def execute_workflow_step(
    _context: ActivityContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    step = payload["step"]
    index = int(payload["step_index"])
    total = int(payload["step_count"])
    progress = min(90, 10 + round(((index + 1) / total) * 75))
    set_run_state(
        payload,
        status="running",
        progress=progress,
        current_stage=str(step["label"]),
    )
    ordered_steps = _topological_steps(payload["workflow_graph"])
    if index + 1 < len(ordered_steps):
        next_step = ordered_steps[index + 1]
        if next_step["approval_required"]:
            set_run_state(
                payload,
                status="waiting_for_approval",
                progress=80,
                current_stage=str(next_step["label"]),
            )
    return {
        "step_id": step["id"],
        "kind": step["kind"],
        "status": ("simulated_no_external_write" if step["kind"] == "external_action" else "completed"),
        "graph_hash": payload["workflow_graph"]["hash"],
    }


def _canonical_graph_hash(graph: dict[str, Any]) -> str:
    canonical = {
        "template_id": graph["template_id"],
        "trigger": graph["trigger"],
        "steps": sorted(graph["steps"], key=lambda item: str(item["id"])),
    }
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _topological_steps(graph: dict[str, Any]) -> list[dict[str, Any]]:
    steps = {str(step["id"]): step for step in graph["steps"]}
    ordered: list[dict[str, Any]] = []
    completed: set[str] = set()
    while len(ordered) < len(steps):
        ready = sorted(
            (
                step
                for step in steps.values()
                if step["id"] not in completed and set(step["depends_on"]).issubset(completed)
            ),
            key=lambda item: str(item["id"]),
        )
        if not ready:
            raise ValueError("Workflow graph is cyclic or has unresolved dependencies")
        for step in ready:
            ordered.append(step)
            completed.add(str(step["id"]))
    return ordered


def _automation_pipeline(
    context: OrchestrationContext,
    payload: dict[str, Any],
    retry: RetryPolicy,
) -> Generator[Any, Any, dict[str, Any]]:
    graph = payload["workflow_graph"]
    if _canonical_graph_hash(graph) != graph["hash"]:
        failed_hash: dict[str, Any] = yield context.call_activity(
            complete_run,
            input={
                **payload,
                "terminal_status": "failed",
                "failure": "Workflow graph hash mismatch",
            },
            retry_policy=retry,
        )
        return failed_hash

    steps = _topological_steps(graph)
    approved = False
    outputs: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if step["approval_required"] and not approved:
            context.set_custom_status(
                {
                    "step": step["id"],
                    "progress": 80,
                    "state": "waiting_for_approval",
                }
            )
            approval_event = yield context.wait_for_external_event(
                "review_decision",
                data_type=dict,
            )
            approved = bool(approval_event["approved"])
            if not approved:
                rejected: dict[str, Any] = yield context.call_activity(
                    complete_run,
                    input={**payload, "approved": False},
                    retry_policy=retry,
                )
                return rejected
        try:
            step_retry = RetryPolicy(
                max_number_of_attempts=int(step["retry_limit"]) + 1,
                first_retry_interval=timedelta(seconds=5),
                backoff_coefficient=2,
                max_retry_interval=timedelta(seconds=60),
                retry_timeout=timedelta(minutes=5),
            )
            output = yield context.call_activity(
                execute_workflow_step,
                input={
                    **payload,
                    "step": step,
                    "step_index": index,
                    "step_count": len(steps),
                },
                retry_policy=step_retry,
            )
        except TaskFailedError as exc:
            failed_step: dict[str, Any] = yield context.call_activity(
                complete_run,
                input={
                    **payload,
                    "terminal_status": "failed",
                    "failure": str(exc),
                },
                retry_policy=retry,
            )
            return failed_step
        outputs.append(output)

    completed: dict[str, Any] = yield context.call_activity(
        complete_run,
        input={
            **payload,
            "approved": True,
            "step_outputs": outputs,
        },
        retry_policy=retry,
    )
    return completed


def research_pipeline(
    context: OrchestrationContext,
    payload: dict[str, Any],
) -> Generator[Any, Any, dict[str, Any]]:
    retry = RetryPolicy(
        max_number_of_attempts=3,
        first_retry_interval=timedelta(seconds=5),
        backoff_coefficient=2,
        max_retry_interval=timedelta(seconds=60),
        retry_timeout=timedelta(minutes=5),
    )
    if payload.get("workflow_kind") == "automation_graph":
        return (yield from _automation_pipeline(context, payload, retry))

    context.set_custom_status({"step": "ingest", "progress": 10})
    try:
        source = yield context.call_activity(
            ingest_source,
            input=payload,
            retry_policy=retry,
        )

        context.set_custom_status({"step": "retrieve", "progress": 35})
        evidence = yield context.call_activity(
            retrieve_evidence,
            input={
                **payload,
                "source_uri": source["blob_uri"],
                **source,
            },
            retry_policy=retry,
        )
    except TaskFailedError as exc:
        context.set_custom_status({"step": "failed", "progress": 100})
        failed_retrieval: dict[str, Any] = yield context.call_activity(
            complete_run,
            input={
                **payload,
                "terminal_status": "failed",
                "failure": str(exc),
            },
            retry_policy=retry,
        )
        return failed_retrieval

    if payload.get("workflow_kind") == "library_ingestion":
        context.set_custom_status({"step": "complete", "progress": 100})
        completed_ingestion: dict[str, Any] = yield context.call_activity(
            complete_run,
            input={**payload, **evidence, "approved": True},
            retry_policy=retry,
        )
        return completed_ingestion

    try:
        context.set_custom_status({"step": "synthesize", "progress": 60})
        artifact = yield context.call_activity(
            synthesize_artifact,
            input={**payload, **evidence},
            retry_policy=retry,
        )

        context.set_custom_status({"step": "verify", "progress": 80})
        verified = yield context.call_activity(
            verify_artifact,
            input={**payload, **artifact},
            retry_policy=retry,
        )
    except TaskFailedError as exc:
        context.set_custom_status({"step": "failed", "progress": 100})
        failed_synthesis: dict[str, Any] = yield context.call_activity(
            complete_run,
            input={
                **payload,
                "terminal_status": "failed",
                "failure": str(exc),
            },
            retry_policy=retry,
        )
        return failed_synthesis

    if payload.get("require_approval", True):
        context.set_custom_status({"step": "approval", "progress": 90})
        approval_event = yield context.wait_for_external_event(
            "review_decision",
            data_type=dict,
        )
        approved = bool(approval_event["approved"])
        if not approved:
            context.set_custom_status({"step": "complete", "progress": 100})
            rejected_result: dict[str, Any] = yield context.call_activity(
                complete_run,
                input={**payload, **verified, "approved": False},
                retry_policy=retry,
            )
            return rejected_result

    context.set_custom_status({"step": "complete", "progress": 100})
    completed: dict[str, Any] = yield context.call_activity(
        complete_run,
        input={**payload, **verified, "approved": True},
        retry_policy=retry,
    )
    return completed
