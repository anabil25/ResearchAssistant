"""Shared Foundry toolbox access.

Every agent connects to the same toolbox and may call any tool in it: library
retrieval (``file_search``), the public research connectors, ``web_search``,
``code_interpreter``, and ``tool_search``/``call_tool`` for progressive
disclosure. There is no per-agent allowlist and no separate "online" agent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool
from azure.identity import get_bearer_token_provider

from .credentials import get_credential

TOOLBOX_SCOPE = "https://ai.azure.com/.default"
TOOLBOX_FEATURE_HEADER = {"Foundry-Features": "Toolboxes=V1Preview"}
DEFAULT_TOOLBOX_NAME = "research-shared"
DEFAULT_TOOLBOX_VERSION = "2"


class _BearerRefresh(httpx.Auth):
    """Re-reads the token on every request so a long session cannot expire."""

    def __init__(self, token_provider: Any) -> None:
        self._token = token_provider

    def auth_flow(self, request: httpx.Request) -> Any:
        request.headers["Authorization"] = f"Bearer {self._token()}"
        yield request


def toolbox_url(endpoint: str, name: str, version: str) -> str:
    return f"{endpoint.rstrip('/')}/toolboxes/{name}/versions/{version}/mcp?api-version=v1"


def shared_toolbox(
    *,
    endpoint: str | None = None,
    name: str | None = None,
    version: str | None = None,
    credential: Any | None = None,
    timeout: float = 120.0,
) -> MCPStreamableHTTPTool:
    """The project toolbox, unfiltered.

    The caller owns the returned tool's lifetime and should ``close()`` it.
    """
    resolved_endpoint = endpoint or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    resolved_name = name or os.environ.get("TOOLBOX_NAME", DEFAULT_TOOLBOX_NAME)
    resolved_version = version or os.environ.get("TOOLBOX_VERSION", DEFAULT_TOOLBOX_VERSION)
    # Managed identity directly where one exists: the full DefaultAzureCredential
    # chain probes sources a hosted container does not have, and blocks.
    cred = credential or get_credential()
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
    )
