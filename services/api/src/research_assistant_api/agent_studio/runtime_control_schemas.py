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
    DeploymentEnvironment,
    utc_now,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION,
)

#: Strict protocol identifier for the runtime-control wire. A runtime pins this
#: exact string; the backend rejects any other value.
RUNTIME_CONTROL_PROTOCOL: Literal["research-assistant.runtime-control.v1"] = "research-assistant.runtime-control.v1"


class RuntimeMappingRef(BaseModel):
    """Canonical, in-body mapping reference a runtime echoes on every request.

    Ruling A: the mapping reference is a single structured object living *inside*
    the request body -- one source of truth -- rather than a flat ``mapping_ref``
    string paired with a separate ``x-runtime-mapping-digest`` header. Flaw C: it
    also carries the monotonic ``revision`` so the ref identifies exactly ONE
    document (one revision), making post-supersession staleness structurally
    distinguishable from tampering. It carries the opaque deployment ``id``, the
    strict ``schema_version``, the ``revision`` sequence, and the full ``digest``
    the runtime was issued. The backend reconstructs the flat ref
    (``<schema_version>:<id>:<revision>``) and matches BOTH ref and digest
    exactly against the server-loaded mapping; nothing here is authority, only
    material the server re-verifies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    schema_version: Literal["runtime-deployment-mapping:v1"] = RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION
    revision: int = Field(ge=1)
    #: Full prefixed mapping digest (``runtime-deployment-mapping:v1:sha256:...``).
    digest: str = Field(min_length=1, max_length=200)

    @property
    def flat_ref(self) -> str:
        """The flat ``<schema_version>:<id>:<revision>`` ref the backend compares to ``mapping.mapping_ref``."""
        return f"{self.schema_version}:{self.id}:{self.revision}"


class RuntimeContextRequest(BaseModel):
    """The minimal facts a runtime supplies to resolve a trusted context.

    Excludes every authority field: no tenant/project (the mapping's stored
    scope is authoritative), no approval_id/invocation_id (server-chosen), no
    release/binding/destination/url/key. The opaque deployment id and the
    mapping digest both live inside the single canonical ``mapping_ref`` object
    (Ruling A) -- there is no separate top-level ``deployment_id`` and no digest
    header. ``request_digest`` is the runtime's canonical digest of the exact
    local request facts, echoed so the backend can bind the resolved context to
    this precise attempt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    mapping_ref: RuntimeMappingRef
    operation_id: str = Field(min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeContextResponse(BaseModel):
    """The **success** body of ``POST /internal/v1/runtime/context`` (HTTP 200).

    The locked wire has no non-resolved success variant: a 200 is returned
    only when a currently-effective approval authorizes the operation, and
    therefore ``approval_id``/``approval_version``/``invocation_id``
    are always present and non-null. Every non-resolved outcome (no effective
    approval, operation not part of the mapping, mapping/release absent,
    expired, unavailable) is a strict HTTP error with uniform external
    semantics -- never a 200 carrying nullable/forbidden approval fields.

    All non-approval fields are copied from the server-loaded
    ``RuntimeDeploymentMapping``; the approval fields are the server's own
    decision; ``request_digest`` is echoed purely as correlation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    mapping_ref: str = Field(min_length=1, max_length=400)
    mapping_digest: str = Field(min_length=1, max_length=200)
    #: Flaw A5: the monotonic revision sequence, returned so the harness can keep
    #: a per-deployment high-water mark and reject a lower sequence (best-effort
    #: client-side rollback detection).
    revision_sequence: int = Field(ge=1)

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    logical_agent_id: str = Field(min_length=1, max_length=200)
    backend_release_id: str = Field(min_length=1, max_length=500)
    backend_version: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)

    approval_id: str = Field(min_length=1, max_length=200)
    approval_version: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    #: The exact ``request_digest`` the runtime supplied, echoed back so a
    #: runtime/audit can correlate this resolved context to the precise request
    #: attempt. It is a correlation value only -- never treated as approval
    #: authority.
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_at: datetime = Field(default_factory=utc_now)


class RuntimeControlError(BaseModel):
    """Uniform, typed error body for every non-success runtime-control outcome.

    Denied, not-found, no-effective-approval, expired, and unavailable all
    render this single shape with an identical opaque ``detail`` so an external
    caller cannot distinguish them -- the typed error documented in the runtime
    OpenAPI, never a per-cause message.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: str


class RuntimeMappingRetrieveRequest(BaseModel):
    """Body a runtime posts to retrieve its own deployment mapping view.

    The opaque ``deployment_id`` travels in the path; the runtime echoes the
    single canonical ``mapping_ref`` object (Ruling A) -- carrying the id,
    schema_version, and digest it was issued -- so the backend can authorize and
    confirm the request targets the precise mapping the runtime believes it is
    bound to. The path deployment_id and ``mapping_ref.id`` must agree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    mapping_ref: RuntimeMappingRef


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
    revision_sequence: int = Field(ge=1)
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
    #: Raw lowercase 64-hex SHA-256 digest -- the ``digest`` field itself is
    #: never prefixed; the scheme lives in ``algorithm``.
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeConsumptionRequest(BaseModel):
    """The exact local invocation facts a runtime supplies to consume an
    approval, all matched against the loaded mapping server-side.

    Carries the approval + invocation identifiers the runtime received from its
    resolved context, plus content digests of every local fact (approval
    request, binding, arguments, destination, idempotency key). No
    tenant/project, release, url, or key authority -- the backend rederives the
    authoritative binding/destination from the mapping and independently
    revalidates each digest. All raw digests are lowercase 64-hex.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    mapping_ref: RuntimeMappingRef
    approval_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    approval_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_hash: RuntimeDestinationHash
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeConsumptionDisposition(StrEnum):
    """The full set of runtime-control consumption dispositions.

    Success: ``CONSUMED`` (first, only durable spend) and ``REPLAYED`` (a
    same-key verified retry of the *same* invocation, returning the original
    receipt). Non-success (each a strict typed HTTP error): ``DENIED`` (never
    authorized), ``EXPIRED`` (approval past expiry), ``NOT_FOUND`` (approval/
    binding not present), ``MISMATCH`` (a supplied digest did not match),
    ``REVOKED`` (approval revoked), and ``EXHAUSTED`` (a *different* invocation
    already spent a single-use grant).
    """

    CONSUMED = "consumed"
    REPLAYED = "replayed"
    DENIED = "denied"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"
    REVOKED = "revoked"
    EXHAUSTED = "exhausted"


_CONSUMPTION_SUCCESS_DISPOSITIONS = (
    RuntimeConsumptionDisposition.CONSUMED,
    RuntimeConsumptionDisposition.REPLAYED,
)


class RuntimeConsumptionReceipt(BaseModel):
    """The durable receipt returned on a successful (or replayed) consumption.

    Every field is non-null: a receipt exists only when a real durable
    consumption record does. ``approval_version`` is the durable approval
    *decision* revision; ``consumption_version`` is the durable *record*
    revision -- both distinct from ``record_contract_version`` (the record's
    schema/contract version tag). A same-key verified replay returns the
    original receipt with ``replayed=True``; a fresh spend has ``replayed=False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumption_id: str = Field(min_length=1, max_length=200)
    approval_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)
    approval_version: str = Field(min_length=1, max_length=200)
    consumption_version: str = Field(min_length=1, max_length=200)
    approver_id: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    consumed_at: datetime
    mapping_digest: str = Field(min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    one_time: bool = True
    replayed: bool
    #: Separate from the durable ``consumption_version`` revision: the schema/
    #: contract version tag of this record's field set.
    record_contract_version: str = Field(default=APPROVAL_CONSUMPTION_RECORD_VERSION, min_length=1)


class RuntimeConsumptionResponse(BaseModel):
    """Success (HTTP 200) body for a consumption: a ``consumed``/``replayed``
    disposition and the complete durable receipt.

    There is no non-success 200 variant: ``disposition`` is restricted to the
    two success values and ``receipt`` is always present. Every non-success
    disposition is a strict ``RuntimeConsumptionError`` HTTP error instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: Literal["research-assistant.runtime-control.v1"] = RUNTIME_CONTROL_PROTOCOL
    deployment_id: str = Field(min_length=1, max_length=200)
    disposition: RuntimeConsumptionDisposition
    receipt: RuntimeConsumptionReceipt

    @model_validator(mode="after")
    def _success_disposition_matches_receipt(self) -> RuntimeConsumptionResponse:
        if self.disposition not in _CONSUMPTION_SUCCESS_DISPOSITIONS:
            raise ValueError("A consumption success response disposition must be 'consumed' or 'replayed'.")
        expected_replayed = self.disposition is RuntimeConsumptionDisposition.REPLAYED
        if self.receipt.replayed != expected_replayed:
            raise ValueError("receipt.replayed must agree with the disposition (replayed <-> True).")
        return self


class RuntimeConsumptionError(BaseModel):
    """Strict typed error body for every non-success consumption disposition.

    Unlike the fully-opaque context denial, a consumption failure surfaces its
    exact ``disposition`` (denied/expired/not_found/mismatch/revoked/exhausted)
    so the runtime can react correctly (e.g. stop retrying an exhausted single-
    use grant vs re-requesting an expired approval), alongside an opaque
    ``detail``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: RuntimeConsumptionDisposition
    detail: str

    @model_validator(mode="after")
    def _disposition_is_non_success(self) -> RuntimeConsumptionError:
        if self.disposition in _CONSUMPTION_SUCCESS_DISPOSITIONS:
            raise ValueError("A consumption error disposition must not be a success value.")
        return self
