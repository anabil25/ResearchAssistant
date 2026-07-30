"""Verify a deployed APIM MCP server over the Streamable HTTP transport.

Performs the MCP initialize handshake and lists the advertised tools so the
deployed surface can be checked without relying on control-plane metadata.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-06-18"


def _decode(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError("Event stream contained no data frame")
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Full MCP endpoint URL ending in /mcp")
    parser.add_argument("--token", default=None, help="Optional bearer token")
    args = parser.parse_args()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        init = client.post(
            args.endpoint,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "research-assistant-verify", "version": "0.1"},
                },
            },
        )
        if init.status_code != 200:
            print(f"initialize failed [{init.status_code}]: {init.text[:400]}")
            return 1
        payload = _decode(init)
        server_info = payload.get("result", {}).get("serverInfo", {})
        session = init.headers.get("Mcp-Session-Id")
        print(f"initialize OK  server={server_info.get('name')} session={'yes' if session else 'no'}")

        if session:
            headers["Mcp-Session-Id"] = session
        client.post(
            args.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        listed = client.post(
            args.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        if listed.status_code != 200:
            print(f"tools/list failed [{listed.status_code}]: {listed.text[:400]}")
            return 1
        tools = _decode(listed).get("result", {}).get("tools", [])
        print(f"tools/list OK  count={len(tools)}")
        for tool in tools:
            print(f"  - {tool.get('name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
