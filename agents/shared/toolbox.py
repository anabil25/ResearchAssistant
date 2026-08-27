"""Shared Foundry toolbox access.

Every agent connects to the same toolbox and may call any tool in it: the public
research connectors, ``web_search``, ``code_interpreter``, and
``tool_search``/``call_tool`` for progressive disclosure. There is no per-agent
allowlist and no separate "online" agent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Collection
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool
from azure.ai.agentserver.core import get_request_context
from azure.identity.aio import get_bearer_token_provider

from .credentials import get_async_credential

TOOLBOX_SCOPE = "https://ai.azure.com/.default"
TOOLBOX_FEATURE_HEADER = {"Foundry-Features": "Toolboxes=V1Preview"}
DEFAULT_TOOLBOX_NAME = "research-shared"


class _BearerRefresh(httpx.Auth):
    """Re-reads the token on every request so a long session cannot expire.

    Only ``async_auth_flow`` is implemented. httpx's default implementation of it
    iterates the *synchronous* ``auth_flow`` generator inline on the event loop,
    so a blocking token fetch there stalls every other task in the process.
    """

    def __init__(self, token_provider: Any) -> None:
        self._token = token_provider

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._token()}"
        for key, value in get_request_context().platform_headers().items():
            request.headers[key] = value
        yield request


def toolbox_url(endpoint: str, name: str, version: str | None = None) -> str:
    # Version numbers restart at 1 in every new project, so an unpinned caller must
    # follow the toolbox's default version rather than a number baked into the image.
    base = f"{endpoint.rstrip('/')}/toolboxes/{name}"
    if version:
        base = f"{base}/versions/{version}"
    return f"{base}/mcp?api-version=v1"


def shared_toolbox(
    *,
    endpoint: str | None = None,
    name: str | None = None,
    version: str | None = None,
    credential: Any | None = None,
    timeout: float = 120.0,
    allowed_tools: Collection[str] | None = None,
) -> MCPStreamableHTTPTool:
    """The project toolbox, optionally limited to an explicit tool set.

    The caller owns the returned tool's lifetime and should ``close()`` it.
    """
    resolved_endpoint = endpoint or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    resolved_name = name or os.environ.get("TOOLBOX_NAME", DEFAULT_TOOLBOX_NAME)
    resolved_version = version or os.environ.get("TOOLBOX_VERSION") or None
    # Managed identity directly where one exists: the full DefaultAzureCredential
    # chain probes sources a hosted container does not have, and blocks.
    cred = credential or get_async_credential()
    http_client = httpx.AsyncClient(
        auth=_BearerRefresh(get_bearer_token_provider(cred, TOOLBOX_SCOPE)),
        headers=dict(TOOLBOX_FEATURE_HEADER),
        timeout=timeout,
    )
    return MCPStreamableHTTPTool(
        name=resolved_name,
        url=toolbox_url(resolved_endpoint, resolved_name, resolved_version),
        http_client=http_client,
        load_prompts=False,
        allowed_tools=allowed_tools,
    )
