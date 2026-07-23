from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .idempotency import canonical_idempotency_digest


class ApprovalConsumptionDisposition(StrEnum):
    CONSUMED = "consumed"
    DENIED = "denied"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"
    ALREADY_CONSUMED = "already_consumed"
    REVOKED = "revoked"


class ApprovalGrantState(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ApprovalConsumptionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_decision_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=512)
    actor_id: str = Field(min_length=1, max_length=512)
    scopes: tuple[str, ...]
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=256)
    operation_version: str = Field(min_length=1, max_length=128)
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: str = Field(min_length=1, max_length=1024)
    policy_id: str = Field(min_length=1, max_length=512)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    invocation_id: str = Field(min_length=1, max_length=512)
    idempotency_key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def scopes_are_canonical(self) -> ApprovalConsumptionRequest:
        if self.scopes != tuple(sorted(set(self.scopes))):
            raise ValueError("approval scopes must be sorted and unique")
        if any(not scope for scope in self.scopes):
            raise ValueError("approval scopes cannot contain empty values")
        return self

    @property
    def digest(self) -> str:
        return canonical_approval_digest(self.model_dump(mode="json"))


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_decision_id: str = Field(min_length=1, max_length=512)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_version: str = Field(min_length=1, max_length=128)
    consumption_id: str = Field(min_length=1, max_length=512)
    consumption_version: str = Field(min_length=1, max_length=128)
    approver_id: str = Field(min_length=1, max_length=512)
    consumed_at: datetime
    expires_at: datetime
    one_time: bool = True

    @model_validator(mode="after")
    def receipt_is_one_time_and_current(self) -> ApprovalReceipt:
        if not self.one_time:
            raise ValueError("governed capability approvals must be one-time")
        if self.consumed_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval receipt timestamps must be timezone-aware")
        if self.consumed_at > self.expires_at:
            raise ValueError("approval cannot be consumed after expiry")
        return self

    @property
    def digest(self) -> str:
        return canonical_approval_digest(self.model_dump(mode="json"))


class ApprovalConsumptionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: ApprovalConsumptionDisposition
    approval_decision_id: str = Field(min_length=1, max_length=512)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_version: str | None = Field(default=None, min_length=1, max_length=128)
    receipt: ApprovalReceipt | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def receipt_matches_disposition(self) -> ApprovalConsumptionResult:
        consumed = self.disposition == ApprovalConsumptionDisposition.CONSUMED
        if consumed != (self.receipt is not None):
            raise ValueError("only consumed approvals carry a receipt")
        if consumed and self.reason_code is not None:
            raise ValueError("consumed approvals cannot carry a denial reason")
        if not consumed and self.reason_code is None:
            raise ValueError("non-consumed approvals require a reason code")
        return self


class ApprovalConsumptionAdapter(Protocol):
    is_durable: bool

    async def consume(
        self,
        request: ApprovalConsumptionRequest,
    ) -> ApprovalConsumptionResult: ...


class ApprovalGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ApprovalConsumptionRequest
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(min_length=1, max_length=128)
    state: ApprovalGrantState = ApprovalGrantState.APPROVED
    approver_id: str = Field(min_length=1, max_length=512)
    approved_at: datetime
    expires_at: datetime
    denial_reason: str | None = Field(default=None, min_length=1, max_length=128)
    receipt: ApprovalReceipt | None = None

    @model_validator(mode="after")
    def grant_is_consistent(self) -> ApprovalGrant:
        if self.request_digest != self.request.digest:
            raise ValueError("approval grant request digest does not match its request")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval grant timestamps must be timezone-aware")
        if self.approved_at > self.expires_at:
            raise ValueError("approval grant expires before it is approved")
        if (self.state == ApprovalGrantState.DENIED) != (self.denial_reason is not None):
            raise ValueError("only denied approval grants carry a denial reason")
        if (self.state == ApprovalGrantState.CONSUMED) != (self.receipt is not None):
            raise ValueError("only consumed approval grants carry a receipt")
        return self


class InMemoryApprovalBackend:
    def __init__(self) -> None:
        self.grants: dict[str, ApprovalGrant] = {}
        self.lock = asyncio.Lock()


class InMemoryApprovalConsumptionAdapter:
    is_durable = False

    def __init__(
        self,
        backend: InMemoryApprovalBackend | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend or InMemoryApprovalBackend()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def issue(self, grant: ApprovalGrant) -> None:
        async with self._backend.lock:
            if grant.request.approval_decision_id in self._backend.grants:
                raise ValueError("approval grant already exists")
            self._backend.grants[grant.request.approval_decision_id] = grant

    async def consume(
        self,
        request: ApprovalConsumptionRequest,
    ) -> ApprovalConsumptionResult:
        async with self._backend.lock:
            grant = self._backend.grants.get(request.approval_decision_id)
            if grant is None:
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.NOT_FOUND,
                    "approval_not_found",
                )
            if grant.request_digest != request.digest:
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.MISMATCH,
                    "approval_binding_mismatch",
                    version=grant.version,
                )
            if grant.state == ApprovalGrantState.CONSUMED:
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.ALREADY_CONSUMED,
                    "approval_already_consumed",
                    version=grant.version,
                )
            if grant.state == ApprovalGrantState.DENIED:
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.DENIED,
                    cast(str, grant.denial_reason),
                    version=grant.version,
                )
            if grant.state == ApprovalGrantState.REVOKED:
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.REVOKED,
                    "approval_revoked",
                    version=grant.version,
                )
            now = self._clock()
            if grant.state == ApprovalGrantState.EXPIRED or grant.expires_at <= now:
                if grant.state != ApprovalGrantState.EXPIRED:
                    grant = grant.model_copy(
                        update={
                            "state": ApprovalGrantState.EXPIRED,
                            "version": self._next_version(grant.version),
                        }
                    )
                    self._backend.grants[request.approval_decision_id] = grant
                return self._terminal_result(
                    request,
                    ApprovalConsumptionDisposition.EXPIRED,
                    "approval_expired",
                    version=grant.version,
                )
            receipt = ApprovalReceipt(
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version=grant.version,
                consumption_id=secrets.token_hex(32),
                consumption_version=self._next_version(grant.version),
                approver_id=grant.approver_id,
                consumed_at=now,
                expires_at=grant.expires_at,
            )
            consumed = grant.model_copy(
                update={
                    "state": ApprovalGrantState.CONSUMED,
                    "version": receipt.consumption_version,
                    "receipt": receipt,
                }
            )
            self._backend.grants[request.approval_decision_id] = consumed
            return ApprovalConsumptionResult(
                disposition=ApprovalConsumptionDisposition.CONSUMED,
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version=receipt.approval_version,
                receipt=receipt,
            )

    @staticmethod
    def _terminal_result(
        request: ApprovalConsumptionRequest,
        disposition: ApprovalConsumptionDisposition,
        reason_code: str,
        *,
        version: str | None = None,
    ) -> ApprovalConsumptionResult:
        return ApprovalConsumptionResult(
            disposition=disposition,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
            approval_version=version,
            reason_code=reason_code,
        )

    @staticmethod
    def _next_version(version: str) -> str:
        return str(int(version) + 1)


def canonical_approval_digest(payload: object) -> str:
    return canonical_idempotency_digest(payload)


def approval_contract_schema_digest() -> str:
    return canonical_approval_digest(
        {
            "request": ApprovalConsumptionRequest.model_json_schema(),
            "receipt": ApprovalReceipt.model_json_schema(),
            "result": ApprovalConsumptionResult.model_json_schema(),
        }
    )
