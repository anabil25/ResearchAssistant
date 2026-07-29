from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Annotation,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
    Message,
    ResponseStream,
)
from pydantic import BaseModel, ConfigDict, HttpUrl, ValidationError

from .approvals import ApprovalConsumptionAdapter
from .capabilities import (
    CapabilityDescriptor,
    CapabilityExecutor,
    CapabilityRegistry,
    InvocationContext,
    ToolRegistration,
)
from .contracts import (
    AgentManifest,
    CoordinatorResponse,
    DatasetRequest,
    EvidenceRef,
    MemoryScope,
    ResearchRequest,
    ResearchResponse,
    bind_contracts,
    canonical_digest,
    resolve_authorized_evidence,
)
from .errors import (
    AuthorizationError,
    ConfigurationError,
    ContractError,
    IsolationError,
    error_from_exception,
)
from .idempotency import IdempotencyStore
from .settings import HarnessSettings
from .state import (
    ConversationRecord,
    ConversationStore,
    from_agent_session,
    to_agent_session,
)
from .telemetry import (
    GovernanceAuditEvent,
    GovernanceAuditSink,
    OpenTelemetryGovernanceAuditSink,
    telemetry_identity_digest,
)

_GOVERNANCE_CONTEXT_KEY = "governance_context"
_TOOL_EVIDENCE_KEY = "authorized_tool_evidence"
_CONNECTOR_OPERATIONS = frozenset(
    {
        "searchLiteratureMetadata",
        "searchGrantOpportunities",
        "searchMatchingMetadata",
    }
)


class _ConnectorToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    query: str
    records: list[dict[str, Any]]
    terms_url: HttpUrl
    retrieved_from: HttpUrl
    warnings: list[str]
    notice: str


class ContractMiddleware(AgentMiddleware):
    def __init__(
        self,
        manifest: AgentManifest,
        settings: HarnessSettings | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        release_id: str | None = None,
        audit_sink: GovernanceAuditSink | None = None,
        conversation_store: ConversationStore | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
    ) -> None:
        self._manifest = manifest
        self._contracts = bind_contracts(manifest)
        self._settings = settings
        self._monotonic = monotonic
        self._release_id = release_id
        self._audit_sink = audit_sink
        self._conversation_store = conversation_store
        self._trusted_tenant_id = trusted_tenant_id
        self._trusted_project_id = trusted_project_id
        self._persistent_conversation = manifest.memory.for_scope(
            MemoryScope.CONVERSATION
        ).persistent
        if audit_sink is not None and release_id is None:
            raise ValueError("governance audit requires immutable release provenance")
        if (trusted_tenant_id is None) != (trusted_project_id is None):
            raise ValueError("trusted invocation tenant and project scopes must be supplied together")

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        request = self._validate(context.messages)
        await self._load_conversation(context, request)
        self._emit_audit("agent.invocation", "accepted", request)
        tool_evidence: list[EvidenceRef] = []
        context.messages = [
            *context.messages[:-1],
            Message(role="user", contents=[self._model_input(request)]),
        ]
        context.function_invocation_kwargs[_GOVERNANCE_CONTEXT_KEY] = self._invocation_context(request).model_dump(
            mode="json"
        )
        context.function_invocation_kwargs[_TOOL_EVIDENCE_KEY] = tool_evidence
        try:
            await call_next()
            if context.stream:
                if not isinstance(context.result, ResponseStream):
                    raise ContractError(
                        "Streaming agent invocation did not return a response stream"
                    )
                context.result = self._buffered_governed_stream(
                    context.result,
                    context,
                    request,
                    tool_evidence,
                )
            elif isinstance(context.result, AgentResponse):
                context.result = self._normalize_response(
                    context.result,
                    request.evidence,
                    tool_evidence,
                )
            if not context.stream:
                await self._save_conversation(context, request)
                self._emit_audit("agent.invocation", "completed", request)
        except BaseException as exc:
            self._emit_audit(
                "agent.invocation",
                "failed",
                request,
                error_code=error_from_exception(exc).code,
            )
            raise

    def _validate(self, messages: list[Message]) -> ResearchRequest:
        if not messages or messages[-1].role != "user":
            raise ContractError("Hosted invocation requires a final user request envelope")
        try:
            return self._contracts.input_model.model_validate_json(messages[-1].text)
        except ValidationError as exc:
            raise ContractError(
                "Hosted invocation does not match the agent input contract",
                context={"contract": self._manifest.input_contract},
            ) from exc

    @staticmethod
    def _model_input(request: ResearchRequest) -> str:
        excluded = {
            "tenant_id",
            "principal_id",
            "session_id",
            "approved_compute",
            "approval_decision_id",
            "invocation_id",
            "idempotency_key",
        }
        return request.model_dump_json(exclude=excluded)

    def _normalize_response(
        self,
        response: AgentResponse[Any],
        request_evidence: tuple[EvidenceRef, ...],
        tool_evidence: list[EvidenceRef],
    ) -> AgentResponse[Any]:
        raw = response.value
        if raw is None:
            raw = self._contracts.output_model.model_validate_json(response.text)
        typed = self._contracts.output_model.model_validate(raw)
        authorized_evidence = self._merge_authorized_evidence(
            request_evidence,
            tool_evidence,
            typed,
        )
        normalized = resolve_authorized_evidence(
            typed,
            authorized_evidence,
        )
        messages = list(response.messages)
        replacement = Message(
            role="assistant",
            contents=[normalized.model_dump_json()],
        )
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "assistant":
                original = messages[index]
                messages[index] = Message(
                    role=original.role,
                    contents=[normalized.model_dump_json()],
                    author_name=original.author_name,
                    message_id=original.message_id,
                    additional_properties=original.additional_properties,
                    raw_representation=original.raw_representation,
                )
                break
        else:
            messages.append(replacement)
        return AgentResponse(
            messages=messages,
            response_id=response.response_id,
            agent_id=response.agent_id,
            created_at=response.created_at,
            finish_reason=response.finish_reason,
            usage_details=response.usage_details,
            value=normalized,
            response_format=self._contracts.output_model,
            continuation_token=response.continuation_token,
            raw_representation=response.raw_representation,
            additional_properties=response.additional_properties,
        )

    def _buffered_governed_stream(
        self,
        source: ResponseStream[AgentResponseUpdate, AgentResponse[Any]],
        context: AgentContext,
        request: ResearchRequest,
        tool_evidence: list[EvidenceRef],
    ) -> ResponseStream[AgentResponseUpdate, AgentResponse[Any]]:
        async def governed_updates() -> AsyncIterator[AgentResponseUpdate]:
            try:
                async for _ in source:
                    pass
                normalized = self._normalize_response(
                    await source.get_final_response(),
                    request.evidence,
                    tool_evidence,
                )
                await self._save_conversation(context, request)
                self._emit_audit("agent.invocation", "completed", request)
                value = cast(ResearchResponse, normalized.value)
                yield AgentResponseUpdate(
                    contents=[Content.from_text(text=value.model_dump_json())],
                    role="assistant",
                    agent_id=normalized.agent_id,
                    response_id=normalized.response_id,
                    finish_reason=normalized.finish_reason,
                )
            except BaseException as exc:
                self._emit_audit(
                    "agent.invocation",
                    "failed",
                    request,
                    error_code=error_from_exception(exc).code,
                )
                raise

        return ResponseStream[AgentResponseUpdate, AgentResponse[Any]](
            governed_updates(),
            finalizer=self._finalize_governed_updates,
        )

    def _finalize_governed_updates(
        self,
        updates: Sequence[AgentResponseUpdate],
    ) -> AgentResponse[Any]:
        return AgentResponse.from_updates(
            updates,
            output_format_type=self._contracts.output_model,
        )

    def _merge_authorized_evidence(
        self,
        request_evidence: tuple[EvidenceRef, ...],
        tool_evidence: list[EvidenceRef],
        response: ResearchResponse,
    ) -> tuple[EvidenceRef, ...]:
        merged = {item.evidence_id: item for item in request_evidence}
        for item in tool_evidence:
            merged.setdefault(item.evidence_id, item)
        if self._manifest.id == "coordinator" and isinstance(response, CoordinatorResponse):
            for result in response.specialist_results:
                if result.response is not None:
                    for item in result.response.evidence:
                        merged.setdefault(item.evidence_id, item)
        return tuple(merged.values())

    def _invocation_context(self, request: ResearchRequest) -> InvocationContext:
        tenant_id = self._trusted_tenant_id or request.tenant_id
        project_id = self._trusted_project_id or request.project_id
        if request.tenant_id != tenant_id or request.project_id != project_id:
            raise IsolationError(
                "Request scope does not match the authenticated Hosted Agent scope",
                context={"agent": self._manifest.id},
            )
        scopes: set[str] = set()
        approval_decision_id: str | None = None
        invocation_id: str | None = None
        idempotency_key: str | None = None
        if self._manifest.online:
            scopes.add("research.public.read")
        if isinstance(request, DatasetRequest):
            approval_decision_id = request.approval_decision_id
            invocation_id = request.invocation_id
            idempotency_key = request.idempotency_key
        destination = None
        if self._settings is not None and self._settings.toolbox_endpoint is not None:
            destination = urlsplit(str(self._settings.toolbox_endpoint)).hostname
        timeout_seconds = self._settings.default_timeout_seconds if self._settings is not None else 60
        return InvocationContext(
            tenant_id=tenant_id,
            project_id=project_id,
            principal_id=request.principal_id,
            scopes=frozenset(scopes),
            destination=destination,
            approval_decision_id=approval_decision_id,
            invocation_id=invocation_id,
            idempotency_key=idempotency_key,
            deadline_monotonic=self._monotonic() + timeout_seconds,
        )

    def _emit_audit(
        self,
        event_name: str,
        outcome: str,
        request: ResearchRequest,
        *,
        error_code: str | None = None,
    ) -> None:
        if self._audit_sink is None or self._release_id is None:
            return
        approval_decision_id = (
            request.approval_decision_id
            if isinstance(request, DatasetRequest)
            else None
        )
        idempotency_key = (
            request.idempotency_key
            if isinstance(request, DatasetRequest)
            else None
        )
        self._audit_sink.emit(
            GovernanceAuditEvent(
                event_name=event_name,
                outcome=outcome,
                agent_id=self._manifest.id,
                release_id=self._release_id,
                tenant_digest=telemetry_identity_digest(request.tenant_id),
                principal_digest=telemetry_identity_digest(request.principal_id),
                approval_decision_digest=(
                    telemetry_identity_digest(approval_decision_id)
                    if approval_decision_id is not None
                    else None
                ),
                idempotency_key_digest=(
                    telemetry_identity_digest(idempotency_key)
                    if idempotency_key is not None
                    else None
                ),
                error_code=error_code,
            )
        )

    async def _load_conversation(
        self,
        context: AgentContext,
        request: ResearchRequest,
    ) -> None:
        if not self._persistent_conversation:
            return
        if self._conversation_store is None:
            raise ConfigurationError(
                "Persistent conversation memory is not configured",
                context={"agent": self._manifest.id},
            )
        record = await self._conversation_store.load(
            request.tenant_id,
            request.session_id,
        )
        if record is not None:
            context.session = to_agent_session(record)
        elif context.session is None:
            context.session = to_agent_session(
                ConversationRecord(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                )
            )
        elif context.session.session_id != request.session_id:
            raise ContractError("Invocation session does not match the request envelope")

    async def _save_conversation(
        self,
        context: AgentContext,
        request: ResearchRequest,
    ) -> None:
        if (
            not self._persistent_conversation
            or self._conversation_store is None
            or context.session is None
        ):
            return
        await self._conversation_store.save(
            from_agent_session(request.tenant_id, context.session)
        )


class GovernedFunctionMiddleware(FunctionMiddleware):
    def __init__(
        self,
        capability: CapabilityDescriptor,
        registrations: ToolRegistration | tuple[ToolRegistration, ...],
        *,
        allowed_connector_sources: frozenset[str] = frozenset(),
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
        release_id: str | None = None,
        allow_test_idempotency_store: bool = False,
        allow_test_approval_adapter: bool = False,
        agent_id: str = "unknown-agent",
        audit_sink: GovernanceAuditSink | None = None,
    ) -> None:
        self._capability = capability
        self._agent_id = agent_id
        self._release_id = release_id
        self._audit_sink = audit_sink
        self._allowed_connector_sources = allowed_connector_sources
        normalized_registrations = (registrations,) if isinstance(registrations, ToolRegistration) else registrations
        if not normalized_registrations:
            raise ValueError("Capability middleware requires an attached tool registration")
        registry = CapabilityRegistry()
        registry.add_descriptor(capability)
        for registration in normalized_registrations:
            if registration.binding.descriptor_ref.id != capability.id:
                raise ConfigurationError(
                    "Tool registration does not match its capability descriptor",
                    context={
                        "capability": capability.id,
                        "operation": registration.binding.operation_ref.id,
                    },
                )
            if not registration.runtime_attested:
                raise ConfigurationError(
                    "Tool registration is not continuously provider-attested",
                    context={"capability": capability.id},
                )
            registry.register_tool(registration)
        self._executor = CapabilityExecutor(
            registry,
            idempotency_store=idempotency_store,
            approval_adapter=approval_adapter,
            release_id=release_id,
            allow_test_idempotency_store=allow_test_idempotency_store,
            allow_test_approval_adapter=allow_test_approval_adapter,
        )

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        raw_governance = context.kwargs.get(_GOVERNANCE_CONTEXT_KEY)
        if not isinstance(raw_governance, dict):
            raise AuthorizationError("Tool invocation is missing validated governance context")
        try:
            governance = InvocationContext.model_validate(raw_governance)
        except ValidationError as exc:
            raise AuthorizationError("Tool governance context is invalid") from exc
        if governance.idempotency_key is not None:
            arguments = (
                context.arguments.model_dump(mode="json")
                if isinstance(context.arguments, BaseModel)
                else dict(context.arguments)
            )
            governance = governance.model_copy(
                update={
                    "operation_fingerprint": canonical_digest(
                        {
                            "function": context.function.name,
                            "arguments": arguments,
                        }
                    )
                }
            )
        self._emit_audit("capability.invocation", "started", context.function.name, governance)
        try:
            result = await self._executor.invoke_operation(
                context.function.name,
                {"context": context, "call_next": call_next},
                governance,
            )
        except BaseException as exc:
            self._emit_audit(
                "capability.invocation",
                "failed",
                context.function.name,
                governance,
                error_code=error_from_exception(exc).code,
            )
            raise
        self._emit_audit("capability.invocation", "completed", context.function.name, governance)
        context.result = result["value"]
        collector = context.kwargs.get(_TOOL_EVIDENCE_KEY)
        if not isinstance(collector, list):
            raise ContractError("Tool invocation is missing its trusted evidence collector")
        evidence = self._evidence_from_tool_result(
            context.function.name,
            context.result,
        )
        collector.extend(evidence)
        context.result = self._expose_authorized_evidence(context.result, evidence)

    def _emit_audit(
        self,
        event_name: str,
        outcome: str,
        operation_id: str,
        governance: InvocationContext,
        *,
        error_code: str | None = None,
    ) -> None:
        if self._audit_sink is None or self._release_id is None:
            return
        self._audit_sink.emit(
            GovernanceAuditEvent(
                event_name=event_name,
                outcome=outcome,
                agent_id=self._agent_id,
                release_id=self._release_id,
                tenant_digest=telemetry_identity_digest(governance.tenant_id),
                principal_digest=telemetry_identity_digest(governance.principal_id),
                capability_id=self._capability.id,
                operation_id=operation_id,
                approval_decision_digest=(
                    telemetry_identity_digest(governance.approval_decision_id)
                    if governance.approval_decision_id is not None
                    else None
                ),
                idempotency_key_digest=(
                    telemetry_identity_digest(governance.idempotency_key)
                    if governance.idempotency_key is not None
                    else None
                ),
                error_code=error_code,
            )
        )

    @staticmethod
    async def _invoke_framework_function(payload: dict[str, Any]) -> dict[str, Any]:
        context = payload["context"]
        call_next = payload["call_next"]
        if not isinstance(context, FunctionInvocationContext) or not callable(call_next):
            raise ContractError("Framework function invocation payload is invalid")
        await call_next()
        return {"value": context.result}

    def _evidence_from_tool_result(
        self,
        tool_name: str,
        result: Any,
    ) -> tuple[EvidenceRef, ...]:
        if tool_name == "web_search":
            return self._evidence_from_web_search(result)
        if tool_name in _CONNECTOR_OPERATIONS:
            return self._evidence_from_connector_result(result)
        return ()

    def _evidence_from_web_search(self, result: Any) -> tuple[EvidenceRef, ...]:
        if isinstance(result, Content):
            annotated = self._evidence_from_annotations(result.annotations)
            if result.type == "mcp_server_tool_result":
                return (*annotated, *self._evidence_from_web_search(result.output))
            if result.type == "function_result":
                return (*annotated, *self._evidence_from_web_search(result.result))
            if result.items is not None:
                return (*annotated, *self._evidence_from_web_search(result.items))
            return annotated
        if isinstance(result, (list, tuple)):
            return tuple(evidence for item in result for evidence in self._evidence_from_web_search(item))
        return ()

    @staticmethod
    def _evidence_from_annotations(
        annotations: Sequence[Annotation] | None,
    ) -> tuple[EvidenceRef, ...]:
        if annotations is None:
            return ()
        evidence: list[EvidenceRef] = []
        for annotation in annotations:
            if annotation.get("type") != "citation":
                continue
            source_uri = annotation.get("url")
            if not isinstance(source_uri, str) or not source_uri:
                continue
            title = annotation.get("title")
            evidence.append(
                EvidenceRef(
                    evidence_id=f"web:{canonical_digest({'url': source_uri, 'title': title})}",
                    source_uri=source_uri,
                    title=title[:512] if isinstance(title, str) else None,
                )
            )
        return tuple(evidence)

    def _evidence_from_connector_result(
        self,
        result: Any,
    ) -> tuple[EvidenceRef, ...]:
        payloads = self._dict_payloads(result)
        try:
            connector_result = next(
                _ConnectorToolResult.model_validate(payload)
                for payload in payloads
                if payload.get("source") in self._allowed_connector_sources
            )
        except (StopIteration, ValidationError) as exc:
            raise ContractError("Connector tool output does not match its governed response contract") from exc
        evidence: list[EvidenceRef] = []
        for record in connector_result.records:
            record_uri = record.get("url")
            source_uri = (
                record_uri if isinstance(record_uri, str) and record_uri else str(connector_result.retrieved_from)
            )
            title = record.get("title")
            evidence.append(
                EvidenceRef(
                    evidence_id=(f"connector:{connector_result.source}:{canonical_digest(record)}"),
                    source_uri=source_uri,
                    title=title[:512] if isinstance(title, str) else None,
                )
            )
        return tuple(evidence)

    @classmethod
    def _dict_payloads(cls, result: Any) -> tuple[dict[str, Any], ...]:
        if isinstance(result, Content):
            nested: list[Any] = []
            if result.type == "mcp_server_tool_result":
                nested.append(result.output)
            elif result.type == "function_result":
                nested.append(result.result)
            elif result.items is not None:
                nested.extend(result.items)
            elif result.text is not None:
                nested.append(result.text)
            return tuple(payload for item in nested for payload in cls._dict_payloads(item))
        if isinstance(result, (list, tuple)):
            return tuple(payload for item in result for payload in cls._dict_payloads(item))
        if isinstance(result, str):
            try:
                return cls._dict_payloads(json.loads(result))
            except json.JSONDecodeError:
                return ()
        if isinstance(result, BaseModel):
            return (result.model_dump(mode="json"),)
        if isinstance(result, dict):
            return (result,)
        return ()

    @staticmethod
    def _expose_authorized_evidence(
        result: Any,
        evidence: tuple[EvidenceRef, ...],
    ) -> Any:
        if not evidence:
            return result
        serialized = [item.model_dump(mode="json") for item in evidence]
        if isinstance(result, BaseModel):
            return {
                **result.model_dump(mode="json"),
                "authorized_evidence": serialized,
            }
        if isinstance(result, dict):
            return {**result, "authorized_evidence": serialized}
        governed_content = Content.from_text(
            text=json.dumps(
                {"authorized_evidence": serialized},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if isinstance(result, list):
            return [*result, governed_content]
        if isinstance(result, tuple):
            return (*result, governed_content)
        if isinstance(result, Content):
            return [result, governed_content]
        if isinstance(result, str):
            return f"{result}\n{governed_content.text}"
        return {
            "tool_output": result,
            "authorized_evidence": serialized,
        }


def middleware_for_manifest(
    manifest: AgentManifest,
    settings: HarnessSettings | None,
    capabilities: tuple[CapabilityDescriptor, ...],
    registrations: tuple[ToolRegistration, ...],
    *,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
    release_id: str | None = None,
    allow_test_idempotency_store: bool = False,
    allow_test_approval_adapter: bool = False,
    audit_sink: GovernanceAuditSink | None = None,
    conversation_store: ConversationStore | None = None,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    platform_managed_tools: bool = False,
) -> list[AgentMiddleware | FunctionMiddleware]:
    effective_audit_sink = audit_sink or (
        OpenTelemetryGovernanceAuditSink() if release_id is not None else None
    )
    middleware: list[AgentMiddleware | FunctionMiddleware] = [
        ContractMiddleware(
            manifest,
            settings,
            release_id=release_id,
            audit_sink=effective_audit_sink,
            conversation_store=conversation_store,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        )
    ]
    if (
        not platform_managed_tools
        and tuple(registration.binding for registration in registrations)
        != manifest.capability_bindings
    ):
        raise ConfigurationError(
            "Runtime registrations do not exactly match manifest capability bindings",
            context={"agent": manifest.id},
        )
    if platform_managed_tools:
        return middleware
    attached = {
        capability.id: tuple(
            registration for registration in registrations if registration.binding.descriptor_ref.id == capability.id
        )
        for capability in capabilities
    }
    if capabilities:
        if trusted_tenant_id is None or trusted_project_id is None:
            raise ConfigurationError(
                "Capability middleware requires an authenticated tenant and project scope",
                context={"agent": manifest.id},
            )
        if settings is None:
            raise ConfigurationError(
                "Capability middleware requires resolved runtime settings",
                context={"agent": manifest.id},
            )
        middleware.extend(
            GovernedFunctionMiddleware(
                capability,
                attached[capability.id],
                allowed_connector_sources=frozenset(manifest.connector_sources),
                idempotency_store=idempotency_store,
                approval_adapter=approval_adapter,
                release_id=release_id,
                allow_test_idempotency_store=allow_test_idempotency_store,
                allow_test_approval_adapter=allow_test_approval_adapter,
                agent_id=manifest.id,
                audit_sink=effective_audit_sink,
            )
            for capability in capabilities
        )
    return middleware
