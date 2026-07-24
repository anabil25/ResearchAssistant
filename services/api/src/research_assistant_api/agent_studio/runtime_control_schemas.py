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

from research_assistant_api.agent_studio.models import (
    APPROVAL_CONSUMPTION_RECORD_VERSION,
    ApprovalConsumptionOutcome,
    DeploymentEnvironment,
    utc_now,
)

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
    #: The exact ``request_digest`` the runtime supplied, echoed back so a
    #: runtime/audit can correlate this resolved context to the precise request
    #: attempt. It is a correlation value only -- never treated as approval
    #: authority.
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
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


class RuntimeMappingRetrieveRequest(BaseModel):
    """Body a runtime posts to retrieve its own deployment mapping view.

    The opaque ``deployment_id`` travels in the path; the runtime echoes the
    exact ``mapping_ref``/``mapping_digest`` it was issued so the backend can
    authorize and confirm the request targets the precise mapping the runtime
    believes it is bound to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    mapping_ref: str = Field(min_length=1, max_length=400)
    mapping_digest: str = Field(min_length=1, max_length=200)


class RuntimeBindingView(BaseModel):
    """Runtime-safe projection of the mapping's pinned binding descriptor.

    Carries the operation/binding identity and provider contract the runtime
    needs to reproduce its request facts, but never the server-side
    ``allowed_client_app_role_bindings`` allowlist -- a runtime has no business
    seeing which other identities may load the deployment.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str
    provider_contract_version: str
    descriptor_id: str
    operation_id: str
    destination_hash_algorithm: Literal["destination:v1:sha256"]


class RuntimeMappingView(BaseModel):
    """Runtime-safe view returned by the mapping-retrieval endpoint.

    Fully mapping-derived (scope/environment/logical agent/backend
    release+version/provider contract+artifact/binding), minus the server-side
    client/app-role allowlist. Echoes the exact ``mapping_ref``/``mapping_digest``
    the runtime authorized with so it can pin them for subsequent context/
    consume calls.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str
    mapping_ref: str
    mapping_digest: str
    tenant_id: str
    project_id: str
    environment: DeploymentEnvironment
    logical_agent_id: str
    backend_release_id: str
    backend_version: str
    provider_contract_version: str
    provider_artifact_digest: str
    binding: RuntimeBindingView
    lifecycle_state: str


class RuntimeDestinationHash(BaseModel):
    """The destination hash object a runtime computed for its invocation.

    ``algorithm`` names the versioned scheme (``destination:v1:sha256``, the
    same one the mapping's ``RuntimeDestinationHashPolicy`` pins); ``digest`` is
    the full prefixed value ``scope.compute_destination_hash`` produces. Passing
    the algorithm alongside the digest lets the backend confirm both sides used
    the identical scheme rather than trusting an opaque string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["destination:v1:sha256"] = "destination:v1:sha256"
    digest: str = Field(pattern=r"^destination:v1:sha256:[0-9a-f]{64}$")


class RuntimeConsumptionRequest(BaseModel):
    """The exact local invocation facts a runtime supplies to consume an
    approval, all matched against the loaded mapping server-side.

    Carries the approval + invocation identifiers the runtime received from its
    resolved context, plus content digests of every local fact (approval
    request, binding, arguments, destination, idempotency key). No
    tenant/project, release, url, or key authority -- the backend rederives the
    authoritative binding/destination from the mapping and independently
    revalidates each digest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    mapping_ref: str = Field(min_length=1, max_length=400)
    approval_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    approval_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_hash: RuntimeDestinationHash
    idempotency_digest: str = Field(pattern=r"^idem:v1:sha256:[0-9a-f]{64}$")


class RuntimeConsumptionReceipt(BaseModel):
    """The durable receipt a runtime receives on a successful (or replayed)
    consumption.

    Every field is non-null: a receipt exists only when a real durable
    consumption record does, so ``approver_id`` and ``expires_at`` (copied from
    the spent approval) are always present, as are the approval decision
    version and the durable consumption revision. A same-key replay returns the
    *original* receipt; the runtime adapter can normalize that to a local
    "consumed" without re-spending.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumption_id: str = Field(min_length=1, max_length=200)
    approval_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    consumption_version: str = Field(default=APPROVAL_CONSUMPTION_RECORD_VERSION, min_length=1)
    approval_decision_version: str = Field(min_length=1, max_length=200)
    consumption_revision: str = Field(min_length=1, max_length=200)
    approver_id: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    consumed_at: datetime


class RuntimeConsumptionResponse(BaseModel):
    """Disposition + optional durable receipt for a consumption attempt.

    ``receipt`` is present exactly for the terminal-success dispositions
    (``CONSUMED`` and ``ALREADY_CONSUMED`` -- a completed spend or an idempotent
    replay of the same invocation) and absent for ``DENIED``/``EXHAUSTED``, so a
    runtime can never treat a denial or an exhausted single-use grant as a
    usable receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    disposition: ApprovalConsumptionOutcome
    receipt: RuntimeConsumptionReceipt | None = None

    @model_validator(mode="after")
    def _receipt_present_iff_terminal_success(self) -> RuntimeConsumptionResponse:
        terminal_success = self.disposition in (
            ApprovalConsumptionOutcome.CONSUMED,
            ApprovalConsumptionOutcome.ALREADY_CONSUMED,
        )
        if terminal_success and self.receipt is None:
            raise ValueError(f"A '{self.disposition.value}' consumption must carry a durable receipt.")
        if not terminal_success and self.receipt is not None:
            raise ValueError(f"A '{self.disposition.value}' consumption must not carry a receipt.")
        return self
