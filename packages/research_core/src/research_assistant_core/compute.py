from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class ComputeJobStatus(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ComputeEstimate(BaseModel):
    adapter: str
    dataset_uri: str
    estimated_bytes: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    estimated_duration_minutes: int | None = Field(default=None, ge=0)
    assumptions: list[str]
    approval_required: bool = True


class ComputeJob(BaseModel):
    id: str
    adapter: str
    dataset_uri: str
    status: ComputeJobStatus
    submitted_at: datetime
    external_job_id: str | None = None
    result_uri: str | None = None
    message: str


class ComputeApprovalRequiredError(PermissionError):
    pass


class ComputePlatformNotConfiguredError(RuntimeError):
    pass


class LargeScaleComputeAdapter(Protocol):
    def estimate(self, dataset_uri: str, *, estimated_bytes: int) -> ComputeEstimate: ...

    def submit(
        self,
        estimate: ComputeEstimate,
        *,
        approved: bool,
        idempotency_key: str,
    ) -> ComputeJob: ...

    def get(self, job_id: str) -> ComputeJob: ...


class DisabledLargeScaleComputeAdapter:
    def estimate(self, dataset_uri: str, *, estimated_bytes: int) -> ComputeEstimate:
        return ComputeEstimate(
            adapter="disabled",
            dataset_uri=dataset_uri,
            estimated_bytes=estimated_bytes,
            assumptions=[
                "No institutional Fabric, Databricks, Azure Batch, or other adapter is configured.",
                "No raw data has been transferred or processed.",
            ],
        )

    def submit(
        self,
        estimate: ComputeEstimate,
        *,
        approved: bool,
        idempotency_key: str,
    ) -> ComputeJob:
        del estimate, approved, idempotency_key
        raise ComputePlatformNotConfiguredError("Large-scale compute requires an institution-configured adapter")

    def get(self, job_id: str) -> ComputeJob:
        del job_id
        raise ComputePlatformNotConfiguredError("Large-scale compute requires an institution-configured adapter")


class LocalTestComputeAdapter:
    def __init__(self) -> None:
        self._jobs: dict[str, ComputeJob] = {}
        self._idempotency: dict[str, str] = {}

    def estimate(self, dataset_uri: str, *, estimated_bytes: int) -> ComputeEstimate:
        gibibytes = estimated_bytes / (1024**3)
        return ComputeEstimate(
            adapter="local-test",
            dataset_uri=dataset_uri,
            estimated_bytes=estimated_bytes,
            estimated_cost_usd=round(gibibytes * 0.01, 2),
            estimated_duration_minutes=max(1, round(gibibytes / 10)),
            assumptions=[
                "Deterministic test estimate only; no external platform is billed.",
                "Production adapters must replace this cost model.",
            ],
        )

    def submit(
        self,
        estimate: ComputeEstimate,
        *,
        approved: bool,
        idempotency_key: str,
    ) -> ComputeJob:
        if not approved:
            raise ComputeApprovalRequiredError("Explicit approval is required before large-scale compute submission")
        existing_id = self._idempotency.get(idempotency_key)
        if existing_id:
            return self._jobs[existing_id]

        job = ComputeJob(
            id=f"compute-{uuid4().hex[:12]}",
            adapter=self.__class__.__name__,
            dataset_uri=estimate.dataset_uri,
            status=ComputeJobStatus.QUEUED,
            submitted_at=datetime.now(UTC),
            external_job_id=None,
            message="Queued in deterministic local test adapter; no raw data was processed.",
        )
        self._jobs[job.id] = job
        self._idempotency[idempotency_key] = job.id
        return job

    def get(self, job_id: str) -> ComputeJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"Unknown local compute job: {job_id}") from exc
