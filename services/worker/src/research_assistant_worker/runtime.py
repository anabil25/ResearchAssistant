from __future__ import annotations

import logging
import os
from threading import Event

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

from research_assistant_worker.config import parse_scheduler_settings
from research_assistant_worker.telemetry import (
    configure_telemetry,
    shutdown_telemetry,
)
from research_assistant_worker.workflows import (
    complete_run,
    execute_workflow_step,
    ingest_source,
    research_pipeline,
    retrieve_evidence,
    synthesize_artifact,
    verify_artifact,
)


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_worker() -> DurableTaskSchedulerWorker:
    settings = parse_scheduler_settings()
    worker = DurableTaskSchedulerWorker(
        host_address=settings.host_address,
        taskhub=settings.task_hub,
        token_credential=(_credential(settings.managed_identity_client_id) if settings.secure_channel else None),
        secure_channel=settings.secure_channel,
    )
    worker.add_orchestrator(research_pipeline)  # type: ignore[arg-type]
    worker.add_activity(ingest_source)
    worker.add_activity(retrieve_evidence)
    worker.add_activity(synthesize_artifact)
    worker.add_activity(verify_artifact)
    worker.add_activity(complete_run)
    worker.add_activity(execute_workflow_step)
    return worker


def main() -> None:
    configure_telemetry(
        "research-assistant-worker",
        environment=os.getenv("RESEARCH_ENVIRONMENT") or os.getenv("AZURE_ENV_NAME"),
    )
    logging.basicConfig(level=logging.INFO)
    worker: DurableTaskSchedulerWorker | None = None
    try:
        worker = build_worker()
        worker.start()  # type: ignore[no-untyped-call]
        Event().wait()
    except KeyboardInterrupt:
        logging.info("Worker shutdown requested")
    finally:
        if worker is not None:
            worker.stop()  # type: ignore[no-untyped-call]
        shutdown_telemetry()
