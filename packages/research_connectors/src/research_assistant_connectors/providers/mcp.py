"""MCP Streamable HTTP provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from threading import Lock, RLock
from typing import Any

import httpx

from ._http import (
    auth_headers,
    binding_safe_endpoint,
    json_object,
    require_endpoint,
    send,
    stable_resource_id,
)
from .config import AuthConfig, MCPConfig, MCPToolPolicy
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityBinding,
    CapabilityInstance,
    CapabilityRecord,
    DiscoveryResult,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationClass,
    OperationDescriptor,
    ProviderDescriptor,
    ProviderError,
    Readiness,
    UnauthorizedError,
    UpstreamError,
    ValidationReport,
    audit_metadata,
    canonical_json_hash,
    capability_instance,
    discovery_result,
    find_operation,
    health_for_target,
    official_provenance,
    operation_allows_retry,
    validation_for_target,
)

PROVIDER_ID = "mcp_streamable_http"
DOCS = ("https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http",)
PROVENANCE = official_provenance(
    DOCS,
    source_version="MCP 2025-06-18",
    last_verified_at="2026-07-23T08:37:02Z",
)


def _safe_input_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("type") != "object":
        return {"type": "object"}
    properties = value.get("properties")
    safe: dict[str, Any] = {}
    if isinstance(properties, Mapping):
        for key, schema in list(properties.items())[:100]:
            if isinstance(key, str) and len(key) <= 128 and isinstance(schema, Mapping):
                declared_type = schema.get("type")
                if declared_type in {"string", "number", "integer", "boolean", "object", "array"}:
                    safe[key] = {"type": declared_type}
    required = value.get("required")
    safe_required = tuple(key for key in required if key in safe) if isinstance(required, list) else ()
    return {"type": "object", "properties": safe, "required": safe_required, "additionalProperties": False}


def _tool_capability(
    name: str,
    schema: dict[str, Any],
    policy: MCPToolPolicy,
    *,
    metadata: Mapping[str, Any],
    destination: str,
    protocol_version: str,
    auth: AuthConfig,
) -> CapabilityRecord:
    safe_destination, destination_digest = binding_safe_endpoint(
        destination,
        invalid_label="invalid:mcp-endpoint",
    )
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=stable_resource_id("mcp.tool", name),
        family="mcp",
        resource_kind="mcp_tool",
        name=name,
        readiness=Readiness.READY,
        auth_modes=(AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
        tenant_boundary="configured tenant",
        data_boundary="configured MCP endpoint",
        resource_id=name,
        operations=(
            OperationDescriptor(
                "mcp.tools.call",
                "1.1.0",
                policy.maturity,
                schema,
                {},
                policy.operation_class,
                policy.approval_policy,
                external_side_effect=policy.operation_class not in {OperationClass.PURE, OperationClass.READ},
                side_effect_destinations=(
                    f"{safe_destination}#url-sha256={destination_digest}",
                ),
                idempotency=policy.idempotency,
                max_retries=1
                if policy.operation_class is not OperationClass.WRITE_IRREVERSIBLE
                and (
                    policy.operation_class in {OperationClass.PURE, OperationClass.READ}
                    or policy.idempotency is not Idempotency.NONE
                )
                else 0,
                docs=DOCS,
            ),
        ),
        provenance=PROVENANCE,
        status_evidence=("Tool returned by a protocol-valid tools/list response.",),
        configuration={
            "tool_name": name,
            "provider_endpoint": safe_destination,
            "provider_endpoint_digest": destination_digest,
            "protocol_version_digest": canonical_json_hash(protocol_version),
            "auth_header_name": auth.header_name,
            "untrusted_tool_metadata_digest": canonical_json_hash(metadata),
        },
        descriptor_version="1.1.0",
        selected_auth_mode=auth.mode,
        connection_id=auth.connection_ref,
        connection_scopes=auth.connection_scopes,
        connection_version=auth.connection_version,
        connection_identity_mode=auth.effective_identity_mode,
        connection_roles=auth.authorized_roles,
    )


class MCPStreamableHTTPProvider:
    def __init__(self, config: MCPConfig) -> None:
        self._config = config
        self._sessions: dict[tuple[str, str, str], str | None] = {}
        self._initialized: set[tuple[str, str, str]] = set()
        self._scope_locks: dict[tuple[str, str, str], RLock] = {}
        self._state_lock = Lock()
        self._next_request_id = 1
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "mcp",
            "MCP Streamable HTTP",
            "Negotiates MCP sessions and invokes discovered tools under trusted local policy.",
            (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
            PROVENANCE,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("MCP endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("MCP tenant boundary is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.endpoint)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    @staticmethod
    def _session_key(context: InvocationContext) -> tuple[str, str, str]:
        return context.tenant_id, context.project_id, context.principal_id

    def _scope_lock(self, session_key: tuple[str, str, str]) -> RLock:
        with self._state_lock:
            return self._scope_locks.setdefault(session_key, RLock())

    def _allocate_request_id(self) -> int:
        with self._state_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    def _headers(self, context: InvocationContext, *, include_session: bool = True) -> dict[str, str]:
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        headers.update(
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": self._config.protocol_version,
            }
        )
        session_key = self._session_key(context)
        with self._scope_lock(session_key):
            session_id = self._sessions.get(session_key) if include_session else None
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _rpc_body(response: Any, request_id: int, provider_id: str) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            candidates = []
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    try:
                        value = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        candidates.append(value)
            payload = next((item for item in candidates if item.get("id") == request_id), None)
            if payload is None:
                raise UpstreamError("MCP stream did not contain the requested response", provider_id=provider_id)
        else:
            payload = json_object(response, provider_id=provider_id)
        if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
            raise UpstreamError("MCP response violated JSON-RPC correlation", provider_id=provider_id)
        if "error" in payload:
            raise UpstreamError("MCP server returned a JSON-RPC error", provider_id=provider_id)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("MCP response did not contain an object result", provider_id=provider_id)
        return result

    def _rpc_once(
        self,
        method: str,
        params: Mapping[str, Any],
        context: InvocationContext,
        *,
        idempotent: bool,
        max_retries: int,
        idempotency_key: str | None = None,
    ) -> tuple[httpx.Response, int, int, str | None]:
        request_id = self._allocate_request_id()
        headers = self._headers(context)
        session_id = headers.get("Mcp-Session-Id")
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method="POST",
            url=require_endpoint(self._config.endpoint),
            headers=headers,
            json_body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            max_retries=max_retries if idempotent else 0,
            idempotent=idempotent,
            passthrough_statuses=frozenset({404}),
        )
        return response, attempts, request_id, session_id

    def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        context: InvocationContext,
        *,
        idempotent: bool,
        max_retries: int,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], int, httpx.Response]:
        response, attempts, request_id, stale_session = self._rpc_once(
            method,
            params,
            context,
            idempotent=idempotent,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
        )
        if response.status_code != 404:
            return self._rpc_body(response, request_id, PROVIDER_ID), attempts, response
        if stale_session is None:
            raise UpstreamError("MCP endpoint returned HTTP 404", provider_id=PROVIDER_ID)

        session_key = self._session_key(context)
        with self._scope_lock(session_key):
            if self._sessions.get(session_key) == stale_session:
                self._sessions.pop(session_key, None)
                self._initialized.discard(session_key)
            if not idempotent:
                raise UpstreamError(
                    "MCP session expired; the non-idempotent request was not replayed",
                    provider_id=PROVIDER_ID,
                )
            if attempts > max_retries:
                raise UpstreamError(
                    "MCP session expired after the retry budget was exhausted",
                    provider_id=PROVIDER_ID,
                )
            self._initialize_with_session(context)

        retry, retry_attempts, retry_id, retry_session = self._rpc_once(
            method,
            params,
            context,
            idempotent=True,
            max_retries=max_retries - attempts,
            idempotency_key=idempotency_key,
        )
        attempts += retry_attempts
        if retry.status_code == 404:
            with self._scope_lock(session_key):
                if self._sessions.get(session_key) == retry_session:
                    self._sessions.pop(session_key, None)
                    self._initialized.discard(session_key)
            raise UpstreamError("MCP session expired after reinitialization", provider_id=PROVIDER_ID)
        return self._rpc_body(retry, retry_id, PROVIDER_ID), attempts, retry

    def _initialize_with_session(self, context: InvocationContext) -> None:
        session_key = self._session_key(context)
        with self._scope_lock(session_key):
            if session_key in self._initialized:
                return
            self._sessions.pop(session_key, None)
            request_id = self._allocate_request_id()
            response, _ = send(
                context,
                provider_id=PROVIDER_ID,
                method="POST",
                url=require_endpoint(self._config.endpoint),
                headers=self._headers(context, include_session=False),
                json_body={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self._config.protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "research-assistant", "version": "0.1.0"},
                    },
                },
                idempotent=True,
            )
            result = self._rpc_body(response, request_id, PROVIDER_ID)
            if result.get("protocolVersion") != self._config.protocol_version:
                raise UpstreamError("MCP server negotiated an unexpected protocol version", provider_id=PROVIDER_ID)
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id is not None:
                if not session_id or any(ord(character) < 0x21 or ord(character) > 0x7E for character in session_id):
                    raise UpstreamError("MCP server returned an invalid session identifier", provider_id=PROVIDER_ID)
                self._sessions[session_key] = session_id
            else:
                self._sessions[session_key] = None
            try:
                notification, _ = send(
                    context,
                    provider_id=PROVIDER_ID,
                    method="POST",
                    url=require_endpoint(self._config.endpoint),
                    headers=self._headers(context),
                    json_body={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    idempotent=True,
                )
                if notification.status_code != 202:
                    raise UpstreamError("MCP initialized notification was not accepted", provider_id=PROVIDER_ID)
            except ProviderError:
                self._sessions.pop(session_key, None)
                raise
            self._initialized.add(session_key)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityRecord, ...]:
        validation = self._validate_configuration(context)
        safe_endpoint, endpoint_digest = binding_safe_endpoint(
            self._config.endpoint,
            invalid_label="invalid:mcp-endpoint",
        )
        if validation.readiness is not Readiness.READY:
            operation = OperationDescriptor(
                "mcp.tools.call",
                "1.0.0",
                Maturity.UNKNOWN,
                {"type": "object"},
                {},
                OperationClass.PRIVILEGED,
                ApprovalPolicy.REQUIRED,
                external_side_effect=True,
                side_effect_destinations=(
                    f"{safe_endpoint}#url-sha256={endpoint_digest}",
                ),
                docs=DOCS,
            )
            return (
                capability_instance(
                    provider_id=PROVIDER_ID,
                    instance_id="mcp.configuration",
                    family="mcp",
                    resource_kind="mcp_endpoint",
                    name="MCP endpoint",
                    readiness=validation.readiness,
                    auth_modes=(self._config.auth.mode,),
                    tenant_boundary="configured tenant",
                    data_boundary="configured MCP endpoint",
                    resource_id="tools",
                    operations=(operation,),
                    provenance=PROVENANCE,
                    status_evidence=("No MCP initialization request was sent.",),
                    unavailable_reason="; ".join(validation.reasons),
                    configuration={
                        "provider_endpoint": safe_endpoint,
                        "provider_endpoint_digest": endpoint_digest,
                        "protocol_version_digest": canonical_json_hash(self._config.protocol_version),
                        "auth_header_name": self._config.auth.header_name,
                    },
                    selected_auth_mode=self._config.auth.mode,
                    connection_id=self._config.auth.connection_ref,
                    connection_scopes=self._config.auth.connection_scopes,
                    connection_version=self._config.auth.connection_version,
                    connection_identity_mode=self._config.auth.effective_identity_mode,
                    connection_roles=self._config.auth.authorized_roles,
                ),
            )
        self._initialize_with_session(context)
        result, _, _ = self._rpc("tools/list", {}, context, idempotent=True, max_retries=1)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise UpstreamError("MCP tools/list result did not contain a tools array", provider_id=PROVIDER_ID)
        policies = {policy.name: policy for policy in self._config.tool_policies}
        capabilities = []
        for tool in tools:
            if not isinstance(tool, Mapping):
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not name or len(name) > 256:
                continue
            policy = policies.get(name, MCPToolPolicy(name))
            capabilities.append(
                _tool_capability(
                    name,
                    _safe_input_schema(tool.get("inputSchema")),
                    policy,
                    metadata={
                        "description": str(tool.get("description", ""))[:1000],
                        "annotations": tool.get("annotations"),
                    },
                    destination=self._config.endpoint or "unconfigured:mcp-endpoint",
                    protocol_version=self._config.protocol_version,
                    auth=self._config.auth,
                )
            )
        return tuple(capabilities)

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        return discovery_result(
            self._discover_instances(context),
            tenant_id=self._config.tenant_id or context.tenant_id,
            project_id=context.project_id,
        )

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return validation_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        return health_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        tool_name = str(instance.configuration["tool_name"])
        result, attempts, response = self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": dict(request.arguments)},
            context,
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
            max_retries=operation.max_retries,
            idempotency_key=request.idempotency_key,
        )
        if result.get("isError") is True:
            raise UpstreamError("MCP tool reported an execution error", provider_id=PROVIDER_ID)
        return InvocationResult(
            PROVIDER_ID,
            instance.instance_id,
            operation.operation_id,
            200,
            result,
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                instance_id=instance.instance_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
