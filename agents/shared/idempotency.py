from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical_idempotency_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyState(StrEnum):
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ClaimDisposition(StrEnum):
    ACQUIRED = "acquired"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class CompletedReplayMode(StrEnum):
    DENY = "deny"
    RETURN_RESULT = "return_result"
    RETURN_REFERENCE = "return_reference"


class IdempotencyPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_seconds: float = Field(default=300, gt=0, le=3600)
    completed_replay: CompletedReplayMode = CompletedReplayMode.DENY


class IdempotencyKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=512)
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=256)
    destination: str = Field(min_length=1, max_length=1024)
    caller_key: str = Field(min_length=1, max_length=256)
    argument_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def digest(self) -> str:
        return canonical_idempotency_digest(self.model_dump(mode="json"))


class IdempotencyApprovalProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_decision_id: str = Field(min_length=1, max_length=512)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_version: str = Field(min_length=1, max_length=128)
    consumption_id: str = Field(min_length=1, max_length=512)
    consumption_version: str = Field(min_length=1, max_length=128)
    approver_id: str = Field(min_length=1, max_length=512)
    consumed_at: datetime


class IdempotencyRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: IdempotencyKey
    state: IdempotencyState
    version: str = Field(min_length=1, max_length=128)
    claim_token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_expires_at: datetime
    actor_id: str = Field(min_length=1, max_length=512)
    release_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    claimed_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    irreversible_started: bool = False
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)
    reconciliation_required: bool = False
    approval: IdempotencyApprovalProvenance | None = None

    @model_validator(mode="after")
    def state_fields_are_consistent(self) -> IdempotencyRecord:
        if self.state == IdempotencyState.CLAIMED:
            if self.started_at is not None:
                raise ValueError("claimed idempotency records cannot have started")
        elif self.state == IdempotencyState.IN_PROGRESS:
            if self.started_at is None:
                raise ValueError("in-progress idempotency records require started_at")
        elif self.state == IdempotencyState.COMPLETED:
            if self.completed_at is None or self.result_hash is None or self.result_ref is None:
                raise ValueError("completed idempotency records require result provenance")
        elif self.failure_code is None or not self.reconciliation_required:
            raise ValueError("failed idempotency records require deterministic reconciliation")
        if self.irreversible_started and self.started_at is None:
            raise ValueError("irreversible operations must be marked started")
        return self


class IdempotencyClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    disposition: ClaimDisposition
    record: IdempotencyRecord
    claim_token: str | None = Field(default=None, min_length=32, max_length=256)

    @model_validator(mode="after")
    def acquired_claim_has_token(self) -> IdempotencyClaim:
        if (self.disposition == ClaimDisposition.ACQUIRED) != (self.claim_token is not None):
            raise ValueError("only acquired idempotency claims carry a claim token")
        return self


class IdempotencyStore(Protocol):
    is_durable: bool

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        actor_id: str,
        release_id: str,
        lease_seconds: float,
    ) -> IdempotencyClaim: ...

    async def mark_in_progress(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        irreversible: bool,
        approval: IdempotencyApprovalProvenance | None = None,
    ) -> IdempotencyRecord: ...

    async def complete(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        result: dict[str, Any],
        result_hash: str,
    ) -> IdempotencyRecord: ...

    async def fail(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        failure_code: str,
    ) -> IdempotencyRecord: ...

    async def load_result(self, result_ref: str) -> dict[str, Any] | None: ...


def idempotency_contract_schema_digest() -> str:
    return canonical_idempotency_digest(
        {
            "key": IdempotencyKey.model_json_schema(),
            "record": IdempotencyRecord.model_json_schema(),
            "claim": IdempotencyClaim.model_json_schema(),
            "approval_provenance": IdempotencyApprovalProvenance.model_json_schema(),
        }
    )
