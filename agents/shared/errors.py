from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ErrorDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    retryable: bool = False
    context: dict[str, str] = {}


class HarnessError(RuntimeError):
    code = "harness_error"
    retryable = False

    def __init__(self, message: str, *, context: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.context = context or {}

    def detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            context=self.context,
        )


class ConfigurationError(HarnessError):
    code = "configuration_error"


class ContractError(HarnessError):
    code = "contract_error"


class AuthorizationError(HarnessError):
    code = "authorization_denied"


class ApprovalRequiredError(HarnessError):
    code = "approval_required"


class CapabilityNotFoundError(HarnessError):
    code = "capability_not_found"


class StaleCapabilityBindingError(HarnessError):
    code = "stale_capability_binding"


class DestinationDeniedError(HarnessError):
    code = "destination_denied"


class DeadlineExceededError(HarnessError):
    code = "deadline_exceeded"


class IdempotencyRequiredError(HarnessError):
    code = "idempotency_required"


class IdempotencyStoreUnavailableError(HarnessError):
    code = "idempotency_store_unavailable"


class IdempotencyInProgressError(HarnessError):
    code = "idempotency_in_progress"
    retryable = True


class IdempotencyReconciliationRequiredError(HarnessError):
    code = "idempotency_reconciliation_required"


class IdempotencyReplayDeniedError(HarnessError):
    code = "idempotency_replay_denied"


class IdempotencyResultMismatchError(HarnessError):
    code = "idempotency_result_mismatch"


class IdempotencyConcurrencyError(HarnessError):
    code = "idempotency_concurrency_conflict"
    retryable = True


class InvocationError(HarnessError):
    code = "invocation_failed"


class RetryableInvocationError(InvocationError):
    code = "retryable_invocation_failed"
    retryable = True


class IsolationError(HarnessError):
    code = "isolation_violation"


def error_from_exception(exc: BaseException) -> ErrorDetail:
    if isinstance(exc, HarnessError):
        return exc.detail()
    return ErrorDetail(code="internal_error", message=type(exc).__name__)


def error_response(exc: BaseException) -> dict[str, Any]:
    return {"error": error_from_exception(exc).model_dump(mode="json")}
