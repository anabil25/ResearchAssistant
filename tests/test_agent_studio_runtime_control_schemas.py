from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import (
    APPROVAL_CONSUMPTION_RECORD_VERSION,
    ApprovalConsumptionOutcome,
    DeploymentEnvironment,
)
from research_assistant_api.agent_studio.runtime_control_schemas import (
    RUNTIME_CONTROL_PROTOCOL,
    RuntimeConsumptionReceipt,
    RuntimeConsumptionRequest,
    RuntimeConsumptionResponse,
    RuntimeContextDecision,
    RuntimeContextRequest,
    RuntimeContextResponse,
    RuntimeDestinationHash,
)

DIGEST = "a" * 64


def _request(**overrides: object) -> RuntimeContextRequest:
    kwargs: dict[str, object] = {
        "deployment_id": "dep-1",
        "mapping_ref": "runtime-deployment-mapping:v1:dep-1",
        "operation_id": "search",
        "request_digest": DIGEST,
    }
    kwargs.update(overrides)
    return RuntimeContextRequest(**kwargs)  # type: ignore[arg-type]


def _response(
    *,
    decision: RuntimeContextDecision,
    approval_id: str | None = None,
    approval_decision_version: str | None = None,
    invocation_id: str | None = None,
) -> RuntimeContextResponse:
    return RuntimeContextResponse(
        deployment_id="dep-1",
        mapping_ref="runtime-deployment-mapping:v1:dep-1",
        mapping_digest="runtime-deployment-mapping:v1:sha256:" + DIGEST,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-1",
        backend_release_id="backend-release-1",
        backend_version="1.2.3",
        binding_id="binding-1",
        operation_id="search",
        decision=decision,
        approval_id=approval_id,
        approval_decision_version=approval_decision_version,
        invocation_id=invocation_id,
        request_digest=DIGEST,
    )


# --- request ---------------------------------------------------------------


def test_request_defaults_protocol_and_is_strict() -> None:
    request = _request()
    assert request.protocol == RUNTIME_CONTROL_PROTOCOL == "research-assistant.runtime-control.v1"


def test_request_rejects_wrong_protocol() -> None:
    with pytest.raises(ValidationError):
        _request(protocol="something-else")


def test_request_rejects_non_hex_request_digest() -> None:
    with pytest.raises(ValidationError):
        _request(request_digest="not-a-digest")


def test_request_forbids_extra_authority_fields() -> None:
    with pytest.raises(ValidationError):
        _request(tenant_id="tenant-1")


def test_request_is_frozen() -> None:
    request = _request()
    with pytest.raises(ValidationError):
        request.deployment_id = "other"


# --- response --------------------------------------------------------------


def test_resolved_response_requires_all_approval_fields() -> None:
    response = _response(
        decision=RuntimeContextDecision.RESOLVED,
        approval_id="appr-1",
        approval_decision_version="approval.decision.v1",
        invocation_id="inv-1",
    )
    assert response.decision is RuntimeContextDecision.RESOLVED
    assert response.approval_id == "appr-1"


def test_resolved_response_rejects_missing_approval_field() -> None:
    with pytest.raises(ValidationError, match="RESOLVED context must carry"):
        _response(
            decision=RuntimeContextDecision.RESOLVED,
            approval_id="appr-1",
            approval_decision_version=None,
            invocation_id="inv-1",
        )


def test_not_approved_response_rejects_approval_fields() -> None:
    with pytest.raises(ValidationError, match="non-RESOLVED context must not carry"):
        _response(decision=RuntimeContextDecision.NOT_APPROVED, approval_id="appr-1")


def test_not_found_response_is_clean() -> None:
    response = _response(decision=RuntimeContextDecision.NOT_FOUND)
    assert response.approval_id is None
    assert response.approval_decision_version is None
    assert response.invocation_id is None


def test_not_approved_response_is_clean() -> None:
    response = _response(decision=RuntimeContextDecision.NOT_APPROVED)
    assert response.decision is RuntimeContextDecision.NOT_APPROVED


def test_response_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeContextResponse(  # type: ignore[call-arg]
            deployment_id="dep-1",
            mapping_ref="r",
            mapping_digest="d",
            tenant_id="t",
            project_id="p",
            environment=DeploymentEnvironment.DEVELOPMENT,
            logical_agent_id="a",
            backend_release_id="b",
            backend_version="1",
            binding_id="bind",
            operation_id="op",
            decision=RuntimeContextDecision.NOT_FOUND,
            surprise="x",
        )


def test_decision_enum_is_exhaustive() -> None:
    assert {d.value for d in RuntimeContextDecision} == {"resolved", "not_approved", "not_found"}


# --- consumption request ---------------------------------------------------

DEST_DIGEST = "destination:v1:sha256:" + "b" * 64
IDEM_DIGEST = "idem:v1:sha256:" + "c" * 64


def _consumption_request(**overrides: object) -> RuntimeConsumptionRequest:
    kwargs: dict[str, object] = {
        "deployment_id": "dep-1",
        "mapping_ref": "runtime-deployment-mapping:v1:dep-1",
        "approval_id": "appr-1",
        "invocation_id": "inv-1",
        "approval_request_digest": "a" * 64,
        "binding_digest": "d" * 64,
        "argument_hash": "e" * 64,
        "destination_hash": RuntimeDestinationHash(digest=DEST_DIGEST),
        "idempotency_digest": IDEM_DIGEST,
    }
    kwargs.update(overrides)
    return RuntimeConsumptionRequest(**kwargs)  # type: ignore[arg-type]


def test_consumption_request_valid_and_strict() -> None:
    request = _consumption_request()
    assert request.protocol == RUNTIME_CONTROL_PROTOCOL
    assert request.destination_hash.algorithm == "destination:v1:sha256"


def test_consumption_request_rejects_bad_idempotency_digest() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(idempotency_digest="idem:v2:sha256:" + "c" * 64)


def test_consumption_request_rejects_non_hex_binding_digest() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(binding_digest="nothex")


def test_destination_hash_rejects_bad_prefix() -> None:
    with pytest.raises(ValidationError):
        RuntimeDestinationHash(digest="sha256:" + "b" * 64)


def test_consumption_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(tenant_id="tenant-1")


# --- consumption response --------------------------------------------------


def _receipt() -> RuntimeConsumptionReceipt:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return RuntimeConsumptionReceipt(
        consumption_id="cons-1",
        approval_id="appr-1",
        invocation_id="inv-1",
        approval_decision_version="approval.decision.v1",
        consumption_revision="rev-1",
        approver_id="reviewer-1",
        expires_at=now,
        consumed_at=now,
    )


def test_receipt_defaults_consumption_version() -> None:
    assert _receipt().consumption_version == APPROVAL_CONSUMPTION_RECORD_VERSION


def test_consumed_response_requires_receipt() -> None:
    response = RuntimeConsumptionResponse(
        deployment_id="dep-1", disposition=ApprovalConsumptionOutcome.CONSUMED, receipt=_receipt()
    )
    assert response.receipt is not None


def test_already_consumed_response_requires_receipt() -> None:
    response = RuntimeConsumptionResponse(
        deployment_id="dep-1", disposition=ApprovalConsumptionOutcome.ALREADY_CONSUMED, receipt=_receipt()
    )
    assert response.receipt is not None


def test_consumed_response_without_receipt_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must carry a durable receipt"):
        RuntimeConsumptionResponse(deployment_id="dep-1", disposition=ApprovalConsumptionOutcome.CONSUMED)


def test_denied_response_must_not_carry_receipt() -> None:
    with pytest.raises(ValidationError, match="must not carry a receipt"):
        RuntimeConsumptionResponse(
            deployment_id="dep-1", disposition=ApprovalConsumptionOutcome.DENIED, receipt=_receipt()
        )


def test_exhausted_response_is_clean_without_receipt() -> None:
    response = RuntimeConsumptionResponse(
        deployment_id="dep-1", disposition=ApprovalConsumptionOutcome.EXHAUSTED
    )
    assert response.receipt is None
