from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from .capabilities import ToolRegistration
from .contracts import (
    AgentContractBinding,
    AgentManifest,
    ResearchRequest,
    ResearchResponse,
    bind_contracts,
    resolve_authorized_evidence,
)
from .errors import ContractError, ErrorDetail, error_from_exception
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
        registrations: tuple[ToolRegistration, ...] = (),
    ) -> None:
        self.manifest = manifest
        self._contracts: AgentContractBinding = bind_contracts(manifest)
        self._runner = runner
        self.release = build_release_metadata(
            manifest,
            model_deployment=manifest.model_policy.deployment_name,
            registrations=registrations,
        )

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": True,
            "agent": self.manifest.name,
            "manifest_digest": self.release.manifest_digest,
            "release_id": self.release.release_id,
            "input_contract": self.manifest.input_contract,
            "output_contract": self.manifest.output_contract,
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
