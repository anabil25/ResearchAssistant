from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_control_schemas import (
    RUNTIME_CONTROL_PROTOCOL,
    RuntimeContextDecision,
    RuntimeContextRequest,
    RuntimeContextResponse,
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
