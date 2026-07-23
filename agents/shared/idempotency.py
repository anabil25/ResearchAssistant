from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import IdempotencyConcurrencyError, IdempotencyResultMismatchError


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


class InMemoryIdempotencyBackend:
    def __init__(self) -> None:
        self.records: dict[str, IdempotencyRecord] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()


class InMemoryIdempotencyStore:
    is_durable = False

    def __init__(
        self,
        backend: InMemoryIdempotencyBackend | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._backend = backend or InMemoryIdempotencyBackend()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def claim(
        self,
        key: IdempotencyKey,
        *,
        actor_id: str,
        release_id: str,
        lease_seconds: float,
    ) -> IdempotencyClaim:
        now = self._clock()
        async with self._backend.lock:
            current = self._backend.records.get(key.digest)
            if current is not None:
                if current.state == IdempotencyState.COMPLETED:
                    return IdempotencyClaim(
                        disposition=ClaimDisposition.COMPLETED,
                        record=current,
                    )
                if current.state == IdempotencyState.FAILED or current.lease_expires_at <= now:
                    return IdempotencyClaim(
                        disposition=ClaimDisposition.RECONCILIATION_REQUIRED,
                        record=current,
                    )
                return IdempotencyClaim(
                    disposition=ClaimDisposition.IN_PROGRESS,
                    record=current,
                )
            token = secrets.token_hex(32)
            record = IdempotencyRecord(
                key=key,
                state=IdempotencyState.CLAIMED,
                version="1",
                claim_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                actor_id=actor_id,
                release_id=release_id,
                claimed_at=now,
            )
            self._backend.records[key.digest] = record
            return IdempotencyClaim(
                disposition=ClaimDisposition.ACQUIRED,
                record=record,
                claim_token=token,
            )

    async def mark_in_progress(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        irreversible: bool,
    ) -> IdempotencyRecord:
        async with self._backend.lock:
            current = self._owned_record(key, claim_token, expected_version)
            if current.state != IdempotencyState.CLAIMED:
                raise IdempotencyConcurrencyError("Idempotency claim is no longer claimable")
            updated = current.model_copy(
                update={
                    "state": IdempotencyState.IN_PROGRESS,
                    "version": self._next_version(current.version),
                    "started_at": self._clock(),
                    "irreversible_started": irreversible,
                }
            )
            self._backend.records[key.digest] = updated
            return updated

    async def complete(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        result: dict[str, Any],
        result_hash: str,
    ) -> IdempotencyRecord:
        if canonical_idempotency_digest(result) != result_hash:
            raise IdempotencyResultMismatchError("Completed result does not match its canonical hash")
        async with self._backend.lock:
            current = self._owned_record(key, claim_token, expected_version)
            if current.state != IdempotencyState.IN_PROGRESS:
                raise IdempotencyConcurrencyError("Idempotency operation is not in progress")
            result_ref = f"memory://idempotency/{result_hash}"
            self._backend.results[result_ref] = deepcopy(result)
            updated = current.model_copy(
                update={
                    "state": IdempotencyState.COMPLETED,
                    "version": self._next_version(current.version),
                    "completed_at": self._clock(),
                    "result_hash": result_hash,
                    "result_ref": result_ref,
                }
            )
            self._backend.records[key.digest] = updated
            return updated

    async def fail(
        self,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        failure_code: str,
    ) -> IdempotencyRecord:
        async with self._backend.lock:
            current = self._owned_record(key, claim_token, expected_version)
            if current.state not in {IdempotencyState.CLAIMED, IdempotencyState.IN_PROGRESS}:
                raise IdempotencyConcurrencyError("Idempotency operation cannot transition to failed")
            updated = current.model_copy(
                update={
                    "state": IdempotencyState.FAILED,
                    "version": self._next_version(current.version),
                    "lease_expires_at": self._clock(),
                    "failure_code": failure_code,
                    "reconciliation_required": True,
                }
            )
            self._backend.records[key.digest] = updated
            return updated

    async def load_result(self, result_ref: str) -> dict[str, Any] | None:
        async with self._backend.lock:
            result = self._backend.results.get(result_ref)
            return deepcopy(result) if result is not None else None

    def record_for(self, key: IdempotencyKey) -> IdempotencyRecord | None:
        return self._backend.records.get(key.digest)

    def replace_result(self, result_ref: str, result: dict[str, Any]) -> None:
        self._backend.results[result_ref] = deepcopy(result)

    def _owned_record(
        self,
        key: IdempotencyKey,
        claim_token: str,
        expected_version: str,
    ) -> IdempotencyRecord:
        current = self._backend.records.get(key.digest)
        token_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
        if (
            current is None
            or current.version != expected_version
            or current.claim_token_hash != token_hash
        ):
            raise IdempotencyConcurrencyError(
                "Idempotency record changed under optimistic concurrency"
            )
        return current

    @staticmethod
    def _next_version(version: str) -> str:
        return str(int(version) + 1)


def idempotency_contract_schema_digest() -> str:
    return canonical_idempotency_digest(
        {
            "key": IdempotencyKey.model_json_schema(),
            "record": IdempotencyRecord.model_json_schema(),
            "claim": IdempotencyClaim.model_json_schema(),
        }
    )
