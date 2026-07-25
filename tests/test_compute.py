from __future__ import annotations

import pytest
from research_assistant_core.compute import (
    ComputeApprovalRequiredError,
    ComputePlatformNotConfiguredError,
    DisabledLargeScaleComputeAdapter,
    LocalTestComputeAdapter,
)


def test_disabled_adapter_never_claims_processing() -> None:
    adapter = DisabledLargeScaleComputeAdapter()
    estimate = adapter.estimate("blob://raw/tb-scale", estimated_bytes=10**12)

    assert estimate.adapter == "disabled"
    assert estimate.approval_required is True
    assert any("No raw data" in assumption for assumption in estimate.assumptions)
    with pytest.raises(ComputePlatformNotConfiguredError):
        adapter.submit(estimate, approved=True, idempotency_key="job-1")
    with pytest.raises(ComputePlatformNotConfiguredError):
        adapter.get("job-1")


def test_compute_submission_requires_approval_and_is_idempotent() -> None:
    adapter = LocalTestComputeAdapter()
    estimate = adapter.estimate("blob://raw/tb-scale", estimated_bytes=10**12)

    with pytest.raises(ComputeApprovalRequiredError):
        adapter.submit(estimate, approved=False, idempotency_key="job-1")

    first = adapter.submit(estimate, approved=True, idempotency_key="job-1")
    second = adapter.submit(estimate, approved=True, idempotency_key="job-1")

    assert first == second
    assert adapter.get(first.id) == first
    with pytest.raises(KeyError, match="Unknown local compute job"):
        adapter.get("missing")
