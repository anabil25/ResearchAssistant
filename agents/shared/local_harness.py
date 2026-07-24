from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .approvals import ApprovalConsumptionAdapter
from .capabilities import (
    ApprovalMode,
    CapabilityExecutor,
    CapabilityRegistry,
    OperationClass,
    ToolRegistration,
)
from .catalog import capabilities_for_manifest
from .contracts import (
    AgentContractBinding,
    AgentManifest,
    ResearchRequest,
    ResearchResponse,
    bind_contracts,
    resolve_authorized_evidence,
)
from .errors import ContractError, ErrorDetail, error_from_exception
from .idempotency import IdempotencyStore
from .release import build_release_metadata

LocalRunner = Callable[
    [ResearchRequest],
    ResearchResponse | dict[str, Any] | Awaitable[ResearchResponse | dict[str, Any]],
]


class LocalInvocation(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_id: str
    payload: dict[str, Any]


class LocalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    response: ResearchResponse | None = None
    error: ErrorDetail | None = None


class LocalHarness:
    def __init__(
        self,
        manifest: AgentManifest,
        runner: LocalRunner,
        *,
        source_bundle_hash: str,
        registrations: tuple[ToolRegistration, ...] = (),
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
    ) -> None:
        self.manifest = manifest
        self._contracts: AgentContractBinding = bind_contracts(manifest)
        self._runner = runner
        self._idempotency_store = idempotency_store
        self._approval_adapter = approval_adapter
        self._requires_idempotency_store = any(
            capability.operation
            in {
                OperationClass.WRITE_REVERSIBLE,
                OperationClass.WRITE_IRREVERSIBLE,
                OperationClass.PRIVILEGED,
            }
            for capability in capabilities_for_manifest(manifest)
        )
        self._requires_approval_adapter = any(
            capability.approval != ApprovalMode.NEVER
            for capability in capabilities_for_manifest(manifest)
        )
        self.release = build_release_metadata(
            manifest,
            model_deployment=manifest.model_policy.deployment_name,
            source_bundle_hash=source_bundle_hash,
            registrations=registrations,
        )

    def capability_executor(self, registry: CapabilityRegistry) -> CapabilityExecutor:
        return CapabilityExecutor(
            registry,
            idempotency_store=self._idempotency_store,
            approval_adapter=self._approval_adapter,
            release_id=self.release.release_id,
            allow_test_idempotency_store=True,
            allow_test_approval_adapter=True,
        )

    def readiness(self) -> dict[str, Any]:
        idempotency_ready = (
            not self._requires_idempotency_store
            or self._idempotency_store is not None
        )
        approval_ready = (
            not self._requires_approval_adapter
            or self._approval_adapter is not None
        )
        return {
            "ready": idempotency_ready and approval_ready,
            "agent": self.manifest.name,
            "manifest_digest": self.release.manifest_digest,
            "release_id": self.release.release_id,
            "input_contract": self.manifest.input_contract,
            "output_contract": self.manifest.output_contract,
            "idempotency_store_configured": idempotency_ready,
            "approval_adapter_configured": approval_ready,
        }

    async def invoke(self, invocation: LocalInvocation) -> LocalResult:
        if invocation.manifest_id != self.manifest.id:
            return LocalResult(
                ok=False,
                error=ContractError("Invocation targets another manifest").detail(),
            )
        try:
            request = self._contracts.input_model.model_validate(invocation.payload)
            value = self._runner(request)
            raw = await value if inspect.isawaitable(value) else value
            response = self._contracts.output_model.model_validate(raw)
            response = resolve_authorized_evidence(
                response,
                request.evidence,
            )
        except ValidationError as exc:
            return LocalResult(
                ok=False,
                error=ContractError(
                    "Local invocation contract validation failed",
                    context={"errors": str(exc.error_count())},
                ).detail(),
            )
        except Exception as exc:
            return LocalResult(ok=False, error=error_from_exception(exc))
        return LocalResult(ok=True, response=response)
