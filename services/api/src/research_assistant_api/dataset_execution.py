from __future__ import annotations

import json

from research_assistant_core import ResearchResult
from research_assistant_core.studio_models import StudioRunRequest

MAX_INLINE_DATASET_CHARS = 100_000
ALLOWED_DATA_CLASSIFICATION = "public_or_synthetic"


def validate_dataset_execution(payload: StudioRunRequest) -> None:
    inputs = payload.inputs
    if inputs.get("analysis_approved") is not True:
        raise ValueError("Explicit dataset analysis approval is required.")
    if inputs.get("data_classification") != ALLOWED_DATA_CLASSIFICATION:
        raise ValueError(
            "Dataset analysis is limited to data explicitly classified as public or synthetic."
        )

    csv_text = inputs.get("csv_text")
    if csv_text is None:
        return
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("The approved CSV input must contain data.")
    if len(csv_text) > MAX_INLINE_DATASET_CHARS:
        raise ValueError(
            f"Inline Code Interpreter input is limited to {MAX_INLINE_DATASET_CHARS:,} characters."
        )
    filename = str(inputs.get("filename", "")).lower()
    if not filename.endswith(".csv"):
        raise ValueError("Direct Code Interpreter analysis currently supports CSV input only.")


def build_dataset_agent_message(
    payload: StudioRunRequest,
    generic: ResearchResult,
    *,
    workflow_title: str,
    stage_labels: list[str],
) -> str:
    validate_dataset_execution(payload)
    csv_text = payload.inputs.get("csv_text")
    if isinstance(csv_text, str):
        material_kind = "Approved CSV"
        dataset_material = csv_text
    else:
        material_kind = "Deterministic profile JSON"
        dataset_material = json.dumps(
            generic.metadata.get("profile"),
            ensure_ascii=True,
        )

    return (
        f"Workflow: {workflow_title}\n"
        f"Stages: {', '.join(stage_labels)}\n"
        "Policy: Use the Foundry Code Interpreter only for the bounded, "
        "product-approved public or synthetic dataset material provided below. "
        "Network access, package installation, repository access, external "
        "writes, and arbitrary destinations are forbidden. Return executed "
        "code, outputs, and limitations. The product owns approval and "
        "provenance.\n"
        f"Objective: {payload.objective}\n"
        f"Dataset filename: {payload.inputs.get('filename', 'dataset.csv')}\n"
        f"Material format: {material_kind}\n"
        f"Bounded dataset material:\n{dataset_material}"
    )
