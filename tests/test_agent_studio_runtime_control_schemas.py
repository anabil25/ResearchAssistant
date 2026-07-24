from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import (
    APPROVAL_CONSUMPTION_RECORD_VERSION,
    DeploymentEnvironment,
)
from research_assistant_api.agent_studio.runtime_control_schemas import (
    RUNTIME_CONTROL_PROTOCOL,
    RuntimeConsumptionDisposition,
    RuntimeConsumptionError,
    RuntimeConsumptionReceipt,
    RuntimeConsumptionRequest,
    RuntimeConsumptionResponse,
    RuntimeContextRequest,
    RuntimeContextResponse,
    RuntimeControlError,
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


def _response(**overrides: object) -> RuntimeContextResponse:
    kwargs: dict[str, object] = {
        "deployment_id": "dep-1",
        "mapping_ref": "runtime-deployment-mapping:v1:dep-1",
        "mapping_digest": "runtime-deployment-mapping:v1:sha256:" + DIGEST,
        "tenant_id": "tenant-1",
        "project_id": "project-1",
        "environment": DeploymentEnvironment.DEVELOPMENT,
        "logical_agent_id": "agent-1",
        "backend_release_id": "backend-release-1",
        "backend_version": "1.2.3",
        "binding_id": "binding-1",
        "operation_id": "search",
        "approval_id": "appr-1",
        "approval_decision_version": "approval.decision.v1",
        "invocation_id": "inv-1",
        "request_digest": DIGEST,
    }
    kwargs.update(overrides)
    return RuntimeContextResponse(**kwargs)  # type: ignore[arg-type]


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


def test_success_response_is_fully_resolved_with_non_null_approval_fields() -> None:
    response = _response()
    assert response.approval_id == "appr-1"
    assert response.approval_decision_version == "approval.decision.v1"
    assert response.invocation_id == "inv-1"
    assert response.request_digest == DIGEST


def test_success_response_rejects_missing_approval_field() -> None:
    with pytest.raises(ValidationError):
        _response(approval_decision_version=None)


def test_success_response_rejects_empty_approval_id() -> None:
    with pytest.raises(ValidationError):
        _response(approval_id="")


def test_response_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _response(surprise="x")


def test_response_is_frozen() -> None:
    response = _response()
    with pytest.raises(ValidationError):
        response.approval_id = "other"


def test_control_error_is_strict_uniform_shape() -> None:
    error = RuntimeControlError(detail="The requested runtime deployment is not available.")
    assert error.detail == "The requested runtime deployment is not available."
    with pytest.raises(ValidationError):
        RuntimeControlError(detail="x", extra="y")  # type: ignore[call-arg]


# --- consumption request ---------------------------------------------------

DEST_DIGEST = "b" * 64
IDEM_DIGEST = "c" * 64


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
        "idempotency_key_digest": IDEM_DIGEST,
    }
    kwargs.update(overrides)
    return RuntimeConsumptionRequest(**kwargs)  # type: ignore[arg-type]


def test_consumption_request_valid_and_strict() -> None:
    request = _consumption_request()
    assert request.protocol == RUNTIME_CONTROL_PROTOCOL
    assert request.destination_hash.algorithm == "destination:v1:sha256"
    assert request.idempotency_key_digest == IDEM_DIGEST


def test_consumption_request_rejects_prefixed_idempotency_digest() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(idempotency_key_digest="idem:v1:sha256:" + "c" * 64)


def test_consumption_request_rejects_non_hex_binding_digest() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(binding_digest="nothex")


def test_destination_hash_digest_is_raw_64hex_not_prefixed() -> None:
    assert RuntimeDestinationHash(digest="b" * 64).digest == "b" * 64
    with pytest.raises(ValidationError):
        RuntimeDestinationHash(digest="destination:v1:sha256:" + "b" * 64)


def test_consumption_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _consumption_request(tenant_id="tenant-1")


# --- consumption response --------------------------------------------------


def _receipt(*, replayed: bool = False) -> RuntimeConsumptionReceipt:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return RuntimeConsumptionReceipt(
        consumption_id="cons-1",
        approval_id="appr-1",
        invocation_id="inv-1",
        approval_version="version-1",
        consumption_version="rev-1",
        approver_id="reviewer-1",
        expires_at=now,
        consumed_at=now,
        mapping_digest="runtime-deployment-mapping:v1:sha256:" + "a" * 64,
        request_digest="f" * 64,
        idempotency_key_digest="c" * 64,
        replayed=replayed,
    )


def test_receipt_defaults_contract_version_and_one_time() -> None:
    receipt = _receipt()
    assert receipt.record_contract_version == APPROVAL_CONSUMPTION_RECORD_VERSION
    assert receipt.one_time is True
    assert receipt.approval_version == "version-1"
    assert receipt.consumption_version == "rev-1"


def test_receipt_rejects_prefixed_idempotency_key_digest() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        RuntimeConsumptionReceipt(
            consumption_id="cons-1",
            approval_id="appr-1",
            invocation_id="inv-1",
            approval_version="version-1",
            consumption_version="rev-1",
            approver_id="reviewer-1",
            expires_at=now,
            consumed_at=now,
            mapping_digest="runtime-deployment-mapping:v1:sha256:" + "a" * 64,
            request_digest="f" * 64,
            idempotency_key_digest="idem:v1:sha256:" + "c" * 64,
            replayed=False,
        )


def test_consumed_response_carries_fresh_receipt() -> None:
    response = RuntimeConsumptionResponse(
        deployment_id="dep-1", disposition=RuntimeConsumptionDisposition.CONSUMED, receipt=_receipt(replayed=False)
    )
    assert response.disposition is RuntimeConsumptionDisposition.CONSUMED
    assert response.receipt.replayed is False


def test_replayed_response_carries_replayed_receipt() -> None:
    response = RuntimeConsumptionResponse(
        deployment_id="dep-1", disposition=RuntimeConsumptionDisposition.REPLAYED, receipt=_receipt(replayed=True)
    )
    assert response.disposition is RuntimeConsumptionDisposition.REPLAYED
    assert response.receipt.replayed is True


def test_success_response_rejects_non_success_disposition() -> None:
    with pytest.raises(ValidationError, match="must be 'consumed' or 'replayed'"):
        RuntimeConsumptionResponse(
            deployment_id="dep-1", disposition=RuntimeConsumptionDisposition.EXHAUSTED, receipt=_receipt()
        )


def test_success_response_rejects_replayed_flag_mismatch() -> None:
    with pytest.raises(ValidationError, match="must agree with the disposition"):
        RuntimeConsumptionResponse(
            deployment_id="dep-1", disposition=RuntimeConsumptionDisposition.CONSUMED, receipt=_receipt(replayed=True)
        )


def test_consumption_error_carries_non_success_disposition() -> None:
    for disposition in (
        RuntimeConsumptionDisposition.DENIED,
        RuntimeConsumptionDisposition.EXPIRED,
        RuntimeConsumptionDisposition.NOT_FOUND,
        RuntimeConsumptionDisposition.MISMATCH,
        RuntimeConsumptionDisposition.REVOKED,
        RuntimeConsumptionDisposition.EXHAUSTED,
    ):
        error = RuntimeConsumptionError(disposition=disposition, detail="opaque")
        assert error.disposition is disposition


def test_consumption_error_rejects_success_disposition() -> None:
    with pytest.raises(ValidationError, match="must not be a success value"):
        RuntimeConsumptionError(disposition=RuntimeConsumptionDisposition.CONSUMED, detail="x")


def test_consumption_dispositions_are_exhaustive() -> None:
    assert {d.value for d in RuntimeConsumptionDisposition} == {
        "consumed",
        "replayed",
        "denied",
        "expired",
        "not_found",
        "mismatch",
        "revoked",
        "exhausted",
    }
