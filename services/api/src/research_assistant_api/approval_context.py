from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

DATASET_COMPUTE_OPERATION: Literal["dataset.compute"] = "dataset.compute"
DATASET_COMPUTE_DESTINATION: Literal["foundry-code-interpreter"] = (
    "foundry-code-interpreter"
)
ApprovalRejectionCode = Literal["expired", "forged", "mismatch", "missing", "revoked"]
CLIENT_AUTHORITY_FIELDS = frozenset(
    {
        "analysis_approved",
        "approval_decision_id",
        "approved_compute",
        "invocation_id",
    }
)


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


class ApprovalContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=200)
    approval_reference: str = Field(min_length=1, max_length=200)
    operation_id: Literal["dataset.compute"] = DATASET_COMPUTE_OPERATION
    arguments_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    destination: Literal["foundry-code-interpreter"] = DATASET_COMPUTE_DESTINATION
    idempotency_key: str = Field(min_length=1, max_length=200)

    @property
    def digest(self) -> str:
        return _canonical_digest(self.model_dump(mode="json"))

    @classmethod
    def from_inputs(
        cls,
        *,
        tenant_id: str,
        project_id: str,
        actor_id: str,
        inputs: dict[str, Any],
    ) -> ApprovalContextRequest:
        forbidden = CLIENT_AUTHORITY_FIELDS.intersection(inputs)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ClientApprovalAuthorityError(
                f"Client-supplied approval authority fields are forbidden: {names}."
            )
        approval_reference = inputs.get("approval_reference")
        idempotency_key = inputs.get("idempotency_key")
        if not isinstance(approval_reference, str) or not approval_reference.strip():
            raise ApprovalContextRejectedError(
                "missing",
                "A server-issued approval reference is required for dataset compute.",
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ApprovalContextRejectedError(
                "missing",
                "A stable idempotency key is required for dataset compute.",
            )
        arguments = {
            key: value
            for key, value in inputs.items()
            if key not in {"approval_reference", "idempotency_key"}
        }
        return cls(
            tenant_id=tenant_id,
            project_id=project_id,
            actor_id=actor_id,
            approval_reference=approval_reference,
            arguments_digest=_canonical_digest(arguments),
            idempotency_key=idempotency_key,
        )


class ResolvedApprovalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_decision_id: str = Field(min_length=1, max_length=200)
    invocation_id: str = Field(min_length=1, max_length=200)


class ApprovalContextResolver(Protocol):
    async def resolve(self, request: ApprovalContextRequest) -> ResolvedApprovalContext: ...


class ApprovalContextUnavailableError(RuntimeError):
    pass


class ClientApprovalAuthorityError(ValueError):
    pass


class ApprovalContextRejectedError(RuntimeError):
    def __init__(
        self,
        code: ApprovalRejectionCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


async def resolve_approval_context(
    resolver: ApprovalContextResolver | None,
    approval_request: ApprovalContextRequest,
) -> ResolvedApprovalContext:
    if resolver is None:
        raise ApprovalContextUnavailableError(
            "Dataset compute is unavailable because no trusted approval context resolver is configured."
        )
    resolution = await resolver.resolve(approval_request)
    if resolution.request_digest != approval_request.digest:
        raise ApprovalContextRejectedError(
            "mismatch",
            "The resolved approval context does not match this dataset operation.",
        )
    return resolution
