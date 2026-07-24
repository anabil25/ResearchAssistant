"""Wire schemas for the internal runtime-control protocol
(``research-assistant.runtime-control.v1``).

These are the request/response bodies the ``/internal/v1/runtime`` control
plane exchanges with a hosted-agent runtime. They are deliberately *separate*
from this package's internal domain types (``approval_context``,
``approval_consumption``, ``idempotency``): the runtime never sees or supplies
internal scope/approval/binding authority, only an opaque deployment reference
plus the exact request facts it can compute on its own. The backend derives
everything authoritative from the loaded ``RuntimeDeploymentMapping``.

Context request carries **only** protocol + deployment_id + mapping_ref +
operation_id + request_digest -- no tenant/project, no approval_id, no
invocation_id, no release/binding/destination/url/key. The response is fully
mapping-derived (scope/environment/logical agent/backend release+version/
binding/operation) plus the server's approval decision, the durable decision
version, and a server-minted invocation id -- values a runtime could never
forge because the server chooses them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_assistant_api.agent_studio.models import DeploymentEnvironment, utc_now

#: Strict protocol identifier for the runtime-control wire. A runtime pins this
#: exact string; the backend rejects any other value.
RUNTIME_CONTROL_PROTOCOL: Literal["research-assistant.runtime-control.v1"] = "research-assistant.runtime-control.v1"


class RuntimeContextRequest(BaseModel):
    """The minimal facts a runtime supplies to resolve a trusted context.

    Excludes every authority field: no tenant/project (the mapping's stored
    scope is authoritative), no approval_id/invocation_id (server-chosen), no
    release/binding/destination/url/key. ``request_digest`` is the runtime's
    canonical digest of the exact local request facts, echoed so the backend
    can bind the resolved context to this precise attempt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    mapping_ref: str = Field(min_length=1, max_length=400)
    operation_id: str = Field(min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeContextDecision(StrEnum):
    """Server decision for a context request.

    ``RESOLVED``: a currently-effective approval authorizes this operation;
    ``approval_id``/``approval_decision_version``/``invocation_id`` are
    populated. ``NOT_APPROVED``: the binding/operation is valid but no
    effective approval currently covers it. ``NOT_FOUND``: the operation is not
    part of the loaded mapping's binding. All three are reported without
    revealing anything a runtime did not already legitimately know.
    """

    RESOLVED = "resolved"
    NOT_APPROVED = "not_approved"
    NOT_FOUND = "not_found"


class RuntimeContextResponse(BaseModel):
    """Fully mapping-derived context returned to the runtime.

    Every field except the approval decision/invocation is copied from the
    server-loaded ``RuntimeDeploymentMapping``; the approval fields are the
    server's own decision. When ``decision`` is not ``RESOLVED``, all three
    approval fields are ``None`` so a runtime can never mistake a denial for a
    usable context by probing a single field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    mapping_ref: str = Field(min_length=1, max_length=400)
    mapping_digest: str = Field(min_length=1, max_length=200)

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    logical_agent_id: str = Field(min_length=1, max_length=200)
    backend_release_id: str = Field(min_length=1, max_length=500)
    backend_version: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)

    decision: RuntimeContextDecision
    approval_id: str | None = Field(default=None, max_length=200)
    approval_decision_version: str | None = Field(default=None, max_length=200)
    invocation_id: str | None = Field(default=None, max_length=200)
    resolved_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _approval_fields_consistent_with_decision(self) -> RuntimeContextResponse:
        approval_fields = (self.approval_id, self.approval_decision_version, self.invocation_id)
        if self.decision is RuntimeContextDecision.RESOLVED:
            if any(field is None for field in approval_fields):
                raise ValueError(
                    "A RESOLVED context must carry approval_id, approval_decision_version, and invocation_id."
                )
        elif any(field is not None for field in approval_fields):
            raise ValueError(
                "A non-RESOLVED context must not carry approval_id, approval_decision_version, or invocation_id."
            )
        return self
