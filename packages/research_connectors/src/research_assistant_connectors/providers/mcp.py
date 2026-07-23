"""MCP Streamable HTTP provider."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from ._http import auth_headers, json_object, require_endpoint, send, stable_resource_id
from .config import MCPConfig, MCPToolPolicy
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityDescriptor,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationDescriptor,
    ProviderDescriptor,
    Readiness,
    Risk,
    UnauthorizedError,
    UpstreamError,
    ValidationReport,
    audit_metadata,
    find_operation,
)

PROVIDER_ID = "mcp_streamable_http"
DOCS = ("https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http",)


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
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        PROVIDER_ID,
        stable_resource_id("mcp.tool", name),
        "mcp",
        "mcp_tool",
        name,
        Readiness.READY,
        True,
        (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
        "configured tenant",
        "configured MCP endpoint",
        (
            OperationDescriptor(
                "mcp.tools.call",
                Maturity.GA,
                schema,
                {},
                policy.risk,
                policy.approval_policy,
                idempotency=policy.idempotency,
                docs=DOCS,
            ),
        ),
        DOCS,
        ("Tool returned by a protocol-valid tools/list response.",),
        metadata={"tool_name": name, "untrusted_tool_metadata": metadata},
    )


class MCPStreamableHTTPProvider:
    def __init__(self, config: MCPConfig) -> None:
        self._config = config
        self._sessions: dict[tuple[str, str], str | None] = {}
        self._initialized: set[tuple[str, str]] = set()
        self._next_id = 1
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "mcp",
            "MCP Streamable HTTP",
            "Negotiates MCP sessions and invokes discovered tools under trusted local policy.",
            (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
            DOCS,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
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

    def _headers(self, context: InvocationContext) -> dict[str, str]:
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        headers.update(
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": self._config.protocol_version,
            }
        )
        session_id = self._sessions.get((context.tenant_id, context.principal_id))
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

    def _rpc(
        self,
        method: str,
        params: Mapping[str, Any],
        context: InvocationContext,
        *,
        idempotent: bool,
    ) -> tuple[dict[str, Any], int, httpx.Response]:
        request_id = self._next_id
        self._next_id += 1
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method="POST",
            url=require_endpoint(self._config.endpoint),
            headers=self._headers(context),
            json_body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            idempotent=idempotent,
        )
        return self._rpc_body(response, request_id, PROVIDER_ID), attempts, response

    def _initialize_with_session(self, context: InvocationContext) -> None:
        session_key = (context.tenant_id, context.principal_id)
        if session_key in self._initialized:
            return
        request_id = self._next_id
        self._next_id += 1
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="POST",
            url=require_endpoint(self._config.endpoint),
            headers=self._headers(context),
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
        self._initialized.add(session_key)

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY:
            operation = OperationDescriptor(
                "mcp.tools.call",
                Maturity.GA,
                {"type": "object"},
                {},
                Risk.EXTERNAL_SIDE_EFFECT,
                ApprovalPolicy.REQUIRED,
                docs=DOCS,
            )
            return (
                CapabilityDescriptor(
                    PROVIDER_ID,
                    "mcp.configuration",
                    "mcp",
                    "mcp_endpoint",
                    "MCP endpoint",
                    validation.readiness,
                    False,
                    (self._config.auth.mode,),
                    "configured tenant",
                    "configured MCP endpoint",
                    (operation,),
                    DOCS,
                    ("No MCP initialization request was sent.",),
                    unavailable_reason="; ".join(validation.reasons),
                ),
            )
        self._initialize_with_session(context)
        result, _, _ = self._rpc("tools/list", {}, context, idempotent=True)
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
                )
            )
        return tuple(capabilities)

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        ready = all(item.readiness is Readiness.READY for item in capabilities)
        return HealthReport(
            Readiness.READY if ready else capabilities[0].readiness,
            (f"Protocol-valid tools/list returned {len(capabilities)} tool(s).",),
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        capability, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        tool_name = str(capability.metadata["tool_name"])
        result, attempts, response = self._rpc(
            "tools/call",
            {"name": tool_name, "arguments": dict(request.arguments)},
            context,
            idempotent=operation.idempotency is Idempotency.INHERENT,
        )
        if result.get("isError") is True:
            raise UpstreamError("MCP tool reported an execution error", provider_id=PROVIDER_ID)
        return InvocationResult(
            PROVIDER_ID,
            capability.capability_id,
            operation.operation_id,
            200,
            result,
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                capability_id=capability.capability_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
