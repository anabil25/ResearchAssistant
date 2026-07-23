from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from research_assistant_connectors.providers import (
    ApprovalPolicy,
    AuthConfig,
    AuthMode,
    GitHubConfig,
    GitHubProvider,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    MCPConfig,
    MCPStreamableHTTPProvider,
    MCPToolPolicy,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OpenAPIProvider,
    Risk,
    WebhookConfig,
    WebhookProvider,
)


class ProtocolHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: Any, **headers: str) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._empty(204 if self.path == "/hook" else 404)

    def do_GET(self) -> None:
        if self.path == "/openapi.json":
            self._json(
                200,
                {
                    "openapi": "3.1.0",
                    "paths": {
                        "/ping": {
                            "get": {
                                "operationId": "ping",
                            }
                        }
                    },
                },
            )
        elif self.path == "/api/ping":
            self._json(200, {"pong": True})
        elif self.path == "/user/repos?per_page=100&type=all":
            self._json(200, [{"full_name": "owner/repository"}])
        elif self.path == "/repos/owner/repository":
            self._json(200, {"full_name": "owner/repository"})
        else:
            self._empty(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path == "/hook":
            payload = json.loads(raw)
            self._json(
                202,
                {
                    "accepted": payload,
                    "idempotency_key": self.headers.get("Idempotency-Key"),
                },
            )
            return
        if self.path != "/mcp":
            self._empty(404)
            return
        payload = json.loads(raw)
        method = payload["method"]
        if method == "initialize":
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-06-18"},
                },
                **{"Mcp-Session-Id": "local-session"},
            )
        elif method == "notifications/initialized":
            self._empty(202)
        elif method == "tools/list":
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                    "required": ["value"],
                                },
                            }
                        ]
                    },
                },
            )
        else:
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"content": payload["params"]["arguments"]},
                },
            )


@contextmanager
def local_protocol_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProtocolHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def invocation_context(client: httpx.Client) -> InvocationContext:
    return InvocationContext(
        "tenant",
        "principal",
        frozenset(),
        None,
        client,
        "correlation",
        "trace",
        lambda _: None,
    )


def test_loopback_mcp_openapi_webhook_and_github_protocols() -> None:
    with local_protocol_server() as endpoint, httpx.Client() as client:
        context = invocation_context(client)

        mcp = MCPStreamableHTTPProvider(
            MCPConfig(
                f"{endpoint}/mcp",
                "tenant",
                tool_policies=(
                    MCPToolPolicy(
                        "echo",
                        Risk.READ,
                        ApprovalPolicy.NEVER,
                        Idempotency.NONE,
                    ),
                ),
            )
        )
        mcp_capability = mcp.discover(context)[0]
        mcp_result = mcp.invoke(
            InvocationRequest(
                mcp_capability.capability_id,
                "mcp.tools.call",
                {"value": "network"},
            ),
            context,
        )

        openapi = OpenAPIProvider(
            OpenAPIConfig(
                f"{endpoint}/api",
                "tenant",
                document_url=f"{endpoint}/openapi.json",
                operation_policies=(
                    OpenAPIOperationPolicy(
                        "ping",
                        Risk.READ,
                        ApprovalPolicy.NEVER,
                    ),
                ),
            )
        )
        openapi_capability = openapi.discover(context)[0]
        openapi_result = openapi.invoke(
            InvocationRequest(openapi_capability.capability_id, "ping", {}),
            context,
        )

        webhook = WebhookProvider(
            WebhookConfig(
                f"{endpoint}/hook",
                "tenant",
                "publish",
                AuthConfig(AuthMode.NONE),
            )
        )
        webhook_capability = webhook.discover(context)[0]
        approved = replace(
            context,
            approved_capability_ids=frozenset({webhook_capability.capability_id}),
        )
        webhook_result = webhook.invoke(
            InvocationRequest(
                webhook_capability.capability_id,
                "publish",
                {"value": "network"},
                "local-event",
            ),
            approved,
        )

        github = GitHubProvider(
            GitHubConfig(
                endpoint,
                "tenant",
                AuthConfig(AuthMode.NONE),
            )
        )
        github_capability = next(
            capability
            for capability in github.discover(context)
            if capability.name.endswith("read")
        )
        github_result = github.invoke(
            InvocationRequest(
                github_capability.capability_id,
                "github.repository.get",
                {},
            ),
            context,
        )

    assert mcp_result.output["content"] == {"value": "network"}
    assert openapi_result.output == {"pong": True}
    assert webhook_result.output["idempotency_key"] == "local-event"
    assert github_result.output["full_name"] == "owner/repository"
