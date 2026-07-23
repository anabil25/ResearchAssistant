from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Never, cast

from agent_framework import Workflow, WorkflowBuilder, WorkflowContext, executor
from azure.ai.projects import AIProjectClient
from pydantic import ValidationError

from .capabilities import CapabilityPolicy, InvocationContext
from .catalog import capabilities_for_manifest
from .contracts import (
    AgentManifest,
    CoordinatorRequest,
    CoordinatorResponse,
    DatasetRequest,
    GrantRequest,
    InstitutionRequest,
    LiteratureRequest,
    MatchingRequest,
    PublicGrantRequest,
    PublicLiteratureRequest,
    PublicMatchingRequest,
    ResearchRequest,
    Sensitivity,
    SpecialistCapability,
    SpecialistPolicy,
    SpecialistRequest,
    SpecialistRequestPayload,
    SpecialistResult,
    bind_contracts,
)
from .credentials import get_credential
from .errors import ContractError, HarnessError, InvocationError, error_from_exception
from .invocation import RetryingResponsesInvoker
from .profiles import get_manifest
from .settings import HarnessSettings

SpecialistInvoker = Callable[[SpecialistRequest], Awaitable[SpecialistResult]]
ProjectClientFactory = Callable[..., Any]


class CoordinatorRouter:
    def __init__(
        self,
        *,
        specialist_policy: SpecialistPolicy | None = None,
        offline_names: dict[SpecialistCapability, str] | None = None,
        online_names: dict[SpecialistCapability, str] | None = None,
    ) -> None:
        policy = specialist_policy or get_manifest("coordinator").specialist_policy
        if policy is None:
            raise ContractError("Coordinator manifest requires a specialist policy")
        self._policy = policy
        pinned_offline = {
            item.capability: item.agent_name for item in policy.specialists if item.sensitivity != Sensitivity.PUBLIC
        }
        pinned_online = {
            item.capability: item.agent_name for item in policy.specialists if item.sensitivity == Sensitivity.PUBLIC
        }
        self._offline = offline_names or pinned_offline
        self._online = online_names or pinned_online

    def route(self, request: CoordinatorRequest) -> tuple[SpecialistRequest, ...]:
        if len(request.requested_capabilities) > self._policy.budget_units:
            raise ContractError("Coordinator specialist budget exceeded")
        routed: list[SpecialistRequest] = []
        for index, capability in enumerate(request.requested_capabilities):
            target = self.target(capability, request.sensitivity)
            if target is None:
                raise ContractError(
                    "Coordinator has no pinned specialist for the requested sensitivity",
                    context={
                        "capability": capability,
                        "sensitivity": request.sensitivity,
                    },
                )
            routed.append(
                SpecialistRequest(
                    request_id=f"{request.session_id}:{index}",
                    capability=capability,
                    request=self._typed_request(request, capability),
                    target_agent=target,
                )
            )
        return tuple(routed)

    def target(
        self,
        capability: SpecialistCapability,
        sensitivity: Sensitivity,
    ) -> str | None:
        targets = self._online if sensitivity == Sensitivity.PUBLIC else self._offline
        return targets.get(capability)

    @staticmethod
    def _typed_request(
        request: CoordinatorRequest,
        capability: SpecialistCapability,
    ) -> SpecialistRequestPayload:
        public = request.sensitivity == Sensitivity.PUBLIC
        contracts: dict[SpecialistCapability, type[ResearchRequest]] = {
            SpecialistCapability.LITERATURE: (PublicLiteratureRequest if public else LiteratureRequest),
            SpecialistCapability.GRANT: (PublicGrantRequest if public else GrantRequest),
            SpecialistCapability.MATCHING: (PublicMatchingRequest if public else MatchingRequest),
            SpecialistCapability.DATASET: DatasetRequest,
            SpecialistCapability.INSTITUTION: InstitutionRequest,
        }
        payload = {
            **request.specialist_inputs.get(capability, {}),
            **request.model_dump(
                exclude={"requested_capabilities", "specialist_inputs"},
            ),
        }
        try:
            return cast(
                SpecialistRequestPayload,
                contracts[capability].model_validate(payload),
            )
        except ValidationError as exc:
            raise ContractError(
                "Coordinator specialist input does not match its pinned contract",
                context={"capability": capability},
            ) from exc


def build_coordinator_workflow(
    invoker: SpecialistInvoker,
    *,
    router: CoordinatorRouter | None = None,
    specialist_policy: SpecialistPolicy | None = None,
) -> Workflow:
    effective_policy = specialist_policy or get_manifest("coordinator").specialist_policy
    if effective_policy is None:
        raise ContractError("Coordinator manifest requires a specialist policy")
    effective_router = router or CoordinatorRouter(specialist_policy=effective_policy)

    @executor(id="validate_request")
    async def validate_request(
        messages: Any,
        ctx: WorkflowContext[CoordinatorRequest],
    ) -> None:
        if isinstance(messages, CoordinatorRequest):
            await ctx.send_message(messages)
            return
        if not messages or messages[-1].role != "user":
            raise ContractError("Coordinator requires a final user request envelope")
        try:
            request = CoordinatorRequest.model_validate_json(messages[-1].text)
        except ValidationError as exc:
            raise ContractError("Coordinator input contract is invalid") from exc
        await ctx.send_message(request)

    @executor(id="deterministic_route")
    async def deterministic_route(
        request: CoordinatorRequest,
        ctx: WorkflowContext[tuple[SpecialistRequest, ...]],
    ) -> None:
        await ctx.send_message(effective_router.route(request))

    @executor(id="invoke_specialists")
    async def invoke_specialists(
        requests: tuple[SpecialistRequest, ...],
        ctx: WorkflowContext[Never, str],
    ) -> None:
        semaphore = asyncio.Semaphore(effective_policy.max_parallelism)

        async def invoke_one(request: SpecialistRequest) -> SpecialistResult:
            async with semaphore:
                return await invoker(request)

        async with asyncio.timeout(effective_policy.deadline_seconds):
            results = tuple(await asyncio.gather(*(invoke_one(item) for item in requests)))
        evidence = tuple(
            evidence for result in results if result.response is not None for evidence in result.response.evidence
        )
        claims = tuple(claim for result in results if result.response is not None for claim in result.response.claims)
        limitations = tuple(
            limitation
            for result in results
            if result.response is not None
            for limitation in result.response.limitations
        )
        response = CoordinatorResponse(
            summary="Specialist results collected under deterministic routing.",
            claims=claims,
            limitations=limitations,
            evidence=evidence,
            specialist_results=results,
        )
        await ctx.yield_output(response.model_dump_json())

    return (
        WorkflowBuilder(
            name="governed-coordinator",
            start_executor=validate_request,
            output_from="all",
        )
        .add_edge(validate_request, deterministic_route)
        .add_edge(deterministic_route, invoke_specialists)
        .build()
    )


class FoundrySpecialistInvoker:
    def __init__(
        self,
        settings: HarnessSettings,
        *,
        project_factory: ProjectClientFactory = AIProjectClient,
        responses_invoker: RetryingResponsesInvoker | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._project_factory = project_factory
        self._responses = responses_invoker or RetryingResponsesInvoker()
        self._monotonic = monotonic
        self._credential = get_credential(settings.managed_identity_client_id)
        coordinator = get_manifest("coordinator")
        self._delegate = capabilities_for_manifest(coordinator, settings)[0]
        self._policy = CapabilityPolicy()

    async def __call__(self, request: SpecialistRequest) -> SpecialistResult:
        if request.target_agent != _specialist_manifest(request).name:
            raise ContractError(
                "Specialist target agent does not match the pinned manifest",
                context={"agent_name": request.target_agent},
            )
        try:
            response = await asyncio.to_thread(self._invoke, request)
            return SpecialistResult(
                request_id=request.request_id,
                capability=request.capability,
                agent_name=request.target_agent,
                response=response,
            )
        except ValidationError:
            error_code = ContractError.code
        except HarnessError as exc:
            error_code = error_from_exception(exc).code
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            error_code=error_code,
        )

    def _invoke(self, request: SpecialistRequest) -> Any:
        self._policy.authorize(
            self._delegate,
            InvocationContext(
                tenant_id=request.request.tenant_id,
                principal_id=request.request.principal_id,
                scopes=frozenset({"research.specialist.invoke"}),
                destination=request.target_agent,
                idempotency_key=request.request_id,
                deadline_monotonic=self._monotonic() + self._settings.default_timeout_seconds,
            ),
        )
        manifest = _specialist_manifest(request)
        contracts = bind_contracts(manifest)
        agent_request = contracts.input_model.model_validate(_specialist_payload(request, manifest.id))
        try:
            with self._project_factory(
                endpoint=str(self._settings.foundry_project_endpoint),
                credential=self._credential,
                allow_preview=True,
            ) as project:
                client = project.get_openai_client(agent_name=request.target_agent)
                reply = self._responses.invoke(
                    client,
                    agent_request.model_dump_json(),
                    request.target_agent,
                    deadline_monotonic=self._monotonic() + self._settings.default_timeout_seconds,
                )
        except HarnessError:
            raise
        except Exception as exc:
            raise InvocationError(
                "Specialist invocation setup failed",
                context={"agent_name": request.target_agent},
            ) from exc
        return contracts.output_model.model_validate_json(reply.content)


def _specialist_manifest(request: SpecialistRequest) -> AgentManifest:
    profile_id = {
        SpecialistCapability.LITERATURE: "literature",
        SpecialistCapability.GRANT: "grant",
        SpecialistCapability.MATCHING: "matching",
        SpecialistCapability.DATASET: "dataset",
        SpecialistCapability.INSTITUTION: "institution",
    }[request.capability]
    if request.request.sensitivity == Sensitivity.PUBLIC and request.capability in {
        SpecialistCapability.LITERATURE,
        SpecialistCapability.GRANT,
        SpecialistCapability.MATCHING,
    }:
        profile_id = f"{profile_id}_online"
    return get_manifest(profile_id)


def _specialist_payload(request: SpecialistRequest, profile_id: str) -> dict[str, Any]:
    payload = request.request.model_dump(mode="json")
    if profile_id == "dataset":
        payload["approved_compute"] = False
    return payload
