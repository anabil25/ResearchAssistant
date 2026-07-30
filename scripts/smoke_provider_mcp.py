"""Smoke-test a provider MCP server exposed by API Management.

Usage::

    python -m scripts.smoke_provider_mcp --gateway https://<apim>.azure-api.net \
        --server provider-arxiv-mcp --tool arxivOaiRequest --argument verb=Identify

The script performs the MCP streamable-HTTP handshake (``initialize`` then
``tools/call``) and prints a short preview of the upstream payload so an
operator can confirm the gateway really reaches the documented provider host.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from typing import Any

_ACCEPT = "application/json, text/event-stream"
_TIMEOUT = 60


def _parse_sse(body: str) -> dict[str, Any]:
    """Return the first JSON-RPC payload from an SSE or plain JSON response."""
    for line in body.splitlines():
        if line.startswith("data:"):
            event: dict[str, Any] = json.loads(line[5:].strip())
            return event
    payload: dict[str, Any] = json.loads(body)
    return payload


def _post(url: str, payload: dict[str, Any], session_id: str | None) -> tuple[dict[str, Any], str | None]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": _ACCEPT},
        method="POST",
    )
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        body = response.read().decode("utf-8")
        return _parse_sse(body), response.headers.get("Mcp-Session-Id") or session_id


def call_tool(gateway: str, server: str, tool: str, arguments: dict[str, str]) -> str:
    """Call one MCP tool and return the textual content the gateway returned."""
    url = f"{gateway.rstrip('/')}/{server}/mcp"
    _, session_id = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "research-assistant-smoke", "version": "1.0"},
            },
        },
        None,
    )
    result, _ = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        session_id,
    )
    if "error" in result:
        raise RuntimeError(f"{tool} failed: {result['error']}")
    content = result.get("result", {}).get("content", [])
    return "".join(part.get("text", "") for part in content)


def main() -> None:
    """Parse arguments and print a preview of the tool response."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--argument", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--preview", type=int, default=400)
    args = parser.parse_args()

    arguments = dict(item.split("=", 1) for item in args.argument)
    text = call_tool(args.gateway, args.server, args.tool, arguments)
    print(f"{args.server}/{args.tool} -> {len(text)} bytes")
    print(text[: args.preview])


if __name__ == "__main__":
    main()
