"""Onboard upstream provider APIs into APIM and publish them to one shared Toolbox.

Each provider specification in ``infra/provider-specs`` is imported as an APIM
API whose backend is the upstream service, converted to an APIM-native MCP
server, and then surfaced through a single Foundry Toolbox shared by every
agent. All writes are idempotent upserts scoped to ``provider-*`` resources.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import get_bearer_token_provider
from research_assistant_core.connector_catalog import connector_definitions

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "provider-specs" / "manifest.json"
APIM_API_VERSION = "2025-09-01-preview"
ARM = "https://management.azure.com"
SHARED_TOOLBOX_NAME = "research-shared"
# Tool Search keeps agent context flat once a Toolbox exceeds a handful of tools.
TOOL_SEARCH_THRESHOLD = 5


def provider_manifest() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return entries


def provider_connection_id(connector_id: str) -> str:
    return f"provider-{connector_id.replace('_', '-')}-apim"


def mcp_endpoint(gateway_url: str, mcp_path: str) -> str:
    return f"{gateway_url.rstrip('/')}/{mcp_path}/mcp"


def shared_toolbox_payload(
    gateway_url: str,
    entries: list[dict[str, Any]],
    connector_targets: dict[str, str],
    guardrail_id: str = "",
) -> dict[str, Any]:
    """Build the single immutable Toolbox version shared by every agent.

    Tool Search is mandatory rather than optional here: Foundry caps an agent at
    128 registered tools and the provider surface is larger than that, so the
    toolbox must expose ``tool_search``/``call_tool`` instead of every tool.
    """
    if not entries:
        raise ValueError("The shared Toolbox requires at least one provider MCP server")
    connectors = connector_definitions()
    expected_connector_ids = {connector.id for connector in connectors}
    if set(connector_targets) != expected_connector_ids:
        raise ValueError("The shared Toolbox requires the complete governed connector catalog")

    tools: list[dict[str, Any]] = [
        {
            "type": "web_search",
            "name": "web_search",
            "description": "Search the public web for current information and citations.",
            "tool_configs": {"*": {"pin": True}},
        },
        {
            "type": "code_interpreter",
            "name": "code_interpreter",
            "description": "Run Python to analyse, chart, and summarise research data.",
            "container": {"type": "auto"},
            "tool_configs": {"*": {"pin": True}},
        },
        {
            "type": "file_search",
            "name": "file_search",
            "description": "Search uploaded research documents supplied at run time.",
        },
    ]
    for connector in connectors:
        tools.append(
            {
                "type": "mcp",
                "server_label": connector.id,
                "server_url": connector_targets[connector.id],
                "project_connection_id": connector.toolbox_connection_id,
                "description": connector.description,
                "require_approval": "never",
                "tool_configs": {
                    "*": {
                        "pin": True,
                        "additional_search_text": " ".join(connector.capabilities),
                    }
                },
            }
        )
    for entry in sorted(entries, key=lambda item: item["connectorId"]):
        tools.append(
            {
                "type": "mcp",
                "server_label": f"provider_{entry['serverLabel']}",
                "server_url": mcp_endpoint(gateway_url, entry["mcpPath"]),
                "project_connection_id": provider_connection_id(entry["connectorId"]),
                "description": f"Raw {entry['displayName']} operations exposed through API Management.",
                "require_approval": "never",
            }
        )
    if len(tools) > TOOL_SEARCH_THRESHOLD:
        tools.append({"type": "toolbox_search"})

    payload: dict[str, Any] = {
        "description": "Governed upstream research provider APIs shared by every agent.",
        "tools": tools,
    }
    if guardrail_id:
        # Screens tool inputs and outputs independently of the model content filter.
        payload["policies"] = {"rai_config": {"rai_policy_name": guardrail_id}}
    return payload


def expected_shared_server_labels(entries: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(entry["serverLabel"] for entry in entries)


class ApimOnboarder:
    """Minimal ARM client for the APIM resources this onboarding owns."""

    def __init__(
        self,
        credential: TokenCredential,
        *,
        subscription_id: str,
        resource_group: str,
        service_name: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._credential = credential
        self._base = (
            f"{ARM}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{service_name}"
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0))
        self._token = get_bearer_token_provider(credential, f"{ARM}/.default")

    @property
    def base(self) -> str:
        return self._base

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self, *, if_match: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        if if_match:
            headers["If-Match"] = "*"
        return headers

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(
            f"{self._base}{path}",
            params={"api-version": APIM_API_VERSION},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} failed [{response.status_code}]: {response.text[:300]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"GET {path} did not return a JSON object")
        return payload

    def _put(self, path: str, body: dict[str, Any]) -> httpx.Response:
        response = self._client.put(
            f"{self._base}{path}",
            params={"api-version": APIM_API_VERSION},
            headers=self._headers(if_match=True),
            content=json.dumps(body),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"PUT {path} failed [{response.status_code}]: {response.text[:300]}")
        return response

    def _await_async_operation(self, response: httpx.Response, label: str) -> None:
        """Surface the real failure detail from an APIM async operation."""
        location = response.headers.get("Azure-AsyncOperation") or response.headers.get("Location")
        if not location:
            return
        for _ in range(60):
            polled = self._client.get(location, headers=self._headers())
            if polled.status_code >= 400:
                raise RuntimeError(f"{label}: async poll failed [{polled.status_code}]: {polled.text[:400]}")
            body = polled.json() if polled.content else {}
            status = body.get("status") if isinstance(body, dict) else None
            if status in {"Succeeded", None}:
                return
            if status in {"Failed", "Canceled"}:
                raise RuntimeError(f"{label}: {json.dumps(body)[:600]}")
            time.sleep(10.0)
        raise RuntimeError(f"{label}: async operation did not complete")

    def _await_api(self, api_id: str, *, attempts: int = 40, delay: float = 15.0) -> None:
        for attempt in range(1, attempts + 1):
            state = self._get(f"/apis/{api_id}").get("properties", {}).get("provisioningState")
            if state in {"Succeeded", None}:
                return
            if state == "Failed":
                raise RuntimeError(f"APIM import for '{api_id}' failed")
            if attempt == attempts:
                raise RuntimeError(f"APIM import for '{api_id}' did not settle (state={state})")
            time.sleep(delay)

    def import_api(self, entry: dict[str, Any], api: dict[str, Any]) -> None:
        response = self._put(
            f"/apis/{api['apiId']}",
            {
                "properties": {
                    "displayName": api.get("displayName", entry["displayName"]),
                    "description": f"Upstream provider API. Documentation: {entry['documentationUrl']}",
                    "path": api["apiPath"],
                    "protocols": ["https"],
                    "serviceUrl": api["serverUrl"],
                    "subscriptionRequired": False,
                    "format": api["apimFormat"],
                    "value": (ROOT / api["specFile"]).read_text(encoding="utf-8"),
                }
            },
        )
        self._await_async_operation(response, f"import {api['apiId']}")
        self._await_api(api["apiId"])

    def operation_names(self, api_id: str) -> list[str]:
        listed = self._get(f"/apis/{api_id}/operations").get("value", [])
        return [item["name"] for item in listed]

    def create_mcp_server(self, entry: dict[str, Any]) -> None:
        self._put(
            f"/apis/{entry['mcpApiId']}",
            {
                "properties": {
                    "type": "mcp",
                    "displayName": f"{entry['displayName']} MCP",
                    "description": f"APIM-native MCP surface for {entry['displayName']}.",
                    "path": entry["mcpPath"],
                    "protocols": ["https"],
                    "subscriptionRequired": False,
                }
            },
        )

    def attach_tools(self, entry: dict[str, Any], api_id: str, operations: list[str]) -> None:
        for operation in operations:
            self._put(
                f"/apis/{entry['mcpApiId']}/tools/{operation}",
                {
                    "properties": {
                        "displayName": operation,
                        "description": f"{entry['displayName']} operation {operation}.",
                        "operationId": f"{self._base}/apis/{api_id}/operations/{operation}",
                    }
                },
            )

    def onboard(self, entry: dict[str, Any]) -> int:
        apis = entry.get("apis") or [
            {
                "apiId": entry["apiId"],
                "apiPath": entry["apiPath"],
                "displayName": entry["displayName"],
                "specFile": entry["specFile"],
                "apimFormat": entry["apimFormat"],
                "serverUrl": entry["serverUrl"],
            }
        ]
        for api in apis:
            self.import_api(entry, api)
        self.create_mcp_server(entry)

        attached = 0
        for api in apis:
            operations = self.operation_names(api["apiId"])
            if not operations:
                raise RuntimeError(f"APIM reported no operations for '{api['apiId']}'")
            self.attach_tools(entry, api["apiId"], operations)
            attached += len(operations)
        return attached


def onboard_provider_apis(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    service_name: str,
    entries: list[dict[str, Any]] | None = None,
    client: httpx.Client | None = None,
    strict: bool = True,
) -> dict[str, int]:
    """Import every provider specification and expose it as an APIM MCP server.

    One provider whose published specification APIM rejects must not silently
    remove the rest of the surface, so failures are collected and re-raised
    together once every other provider has been onboarded.
    """
    selected = entries if entries is not None else provider_manifest()
    onboarder = ApimOnboarder(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        service_name=service_name,
        client=client,
    )
    results: dict[str, int] = {}
    failures: dict[str, str] = {}
    try:
        for entry in selected:
            connector_id = entry["connectorId"]
            try:
                results[connector_id] = onboarder.onboard(entry)
            except (RuntimeError, httpx.HTTPError) as exc:
                failures[connector_id] = str(exc)[:300]
                print(f"APIM: {connector_id} FAILED -> {failures[connector_id]}")
                continue
            print(f"APIM: {connector_id} -> {results[connector_id]} MCP tools")
    finally:
        onboarder.close()

    if failures and strict:
        detail = "; ".join(f"{name}: {reason}" for name, reason in sorted(failures.items()))
        raise RuntimeError(f"Provider onboarding failed for {sorted(failures)}: {detail}")
    return results
