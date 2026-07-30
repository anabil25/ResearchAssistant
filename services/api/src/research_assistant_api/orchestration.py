from __future__ import annotations

import logging
from typing import Any

from research_assistant_worker.ingestion import extract_source, index_extracted_source

from research_assistant_api.workspace import WorkspaceStore

logger = logging.getLogger(__name__)


def execute_library_ingestion(
    store: WorkspaceStore,
    payload: dict[str, Any],
) -> None:
    try:
        source = extract_source(payload)
        evidence = index_extracted_source(
            {
                **payload,
                "source_uri": source["blob_uri"],
                **source,
            }
        )
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        store.fail_ingestion(
            str(payload["source_id"]),
            str(payload["run_id"]),
            reason,
        )
        logger.exception("Library ingestion failed for run %s", payload["run_id"])
        return

    evidence_count = int(evidence["passage_count"])
    chunk_count = int(source.get("chunk_count", 0))
    completed = store.complete_ingestion(
        str(payload["source_id"]),
        str(payload["run_id"]),
        evidence_count=evidence_count,
        needs_review=chunk_count == 0 or evidence_count < chunk_count,
    )
    if completed is None:
        logger.error("Library ingestion state disappeared for run %s", payload["run_id"])
