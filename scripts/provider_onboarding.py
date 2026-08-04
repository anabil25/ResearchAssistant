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
from xml.sax.saxutils import escape

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import get_bearer_token_provider
from research_assistant_core.connector_catalog import connector_definitions

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "provider-specs" / "manifest.json"
CONNECTOR_OPENAPI = ROOT / "infra" / "provider-specs" / "authored" / "research_connectors.json"
CONNECTOR_OPERATION_POLICIES = ROOT / "infra" / "connector-operation-policies.json"
CONNECTOR_MCP_CATALOG = ROOT / "infra" / "connector-mcp-catalog.json"
CONNECTOR_MCP_TOOLS = ROOT / "infra" / "connector-mcp-tools.json"
APIM_API_VERSION = "2025-09-01-preview"
APIM_CORE_API_VERSION = "2024-05-01"
ARM = "https://management.azure.com"
SHARED_TOOLBOX_NAME = "research-shared"
CONNECTOR_API_ID = "research-connectors-v1"
# Superseded by tools that reference the facade operations directly.
OBSOLETE_MCP_BACKING_API_ID = "research-connectors-mcp-backing-v1"
CONNECTOR_PRODUCT_ID = "research-agent-tools"
CONNECTOR_SUBSCRIPTION_ID = "foundry-agent-tools"
CONNECTOR_CONTACT_NAME = "research-connector-contact"
UNCONFIGURED_CREDENTIAL = "unset"
APIM_READY_RETRY_DELAYS = (0, 15, 30, 60, 120, 180, 240)
APIM_TOOL_RETRY_DELAYS = APIM_READY_RETRY_DELAYS
APIM_TOOL_VERIFY_DELAYS = (0, 5, 15, 30)
# Tool Search keeps agent context flat once a Toolbox exceeds a handful of tools.
TOOL_SEARCH_THRESHOLD = 5


class ApimRequestError(RuntimeError):
    def __init__(self, method: str, path: str, response: httpx.Response) -> None:
        self.status_code = response.status_code
        retry_after = response.headers.get("Retry-After")
        self.retry_after = int(retry_after) if retry_after and retry_after.isdigit() else None
        request_id = response.headers.get("x-ms-request-id", "unavailable")
        super().__init__(
            f"{method} {path} failed [{response.status_code}] "
            f"request-id={request_id}: {response.text[:300]}"
        )


def provider_manifest() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return entries


def provider_connection_id(connector_id: str) -> str:
    return f"provider-{connector_id.replace('_', '-')}-apim"


def mcp_endpoint(gateway_url: str, mcp_path: str) -> str:
    return f"{gateway_url.rstrip('/')}/{mcp_path}/mcp"


def shared_toolbox_payload(
    connector_targets: dict[str, str],
    guardrail_id: str = "",
) -> dict[str, Any]:
    """Build the governed immutable Toolbox version shared by every agent.

    Tool Search is mandatory rather than optional here: Foundry caps an agent at
    128 registered tools, so the toolbox exposes ``tool_search``/``call_tool``
    instead of flattening every connector operation into agent context.
    """
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
    if len(tools) > TOOL_SEARCH_THRESHOLD:
        tools.append({"type": "toolbox_search"})

    payload: dict[str, Any] = {
        "description": "Governed normalized research connectors shared by every agent.",
        "tools": tools,
    }
    if guardrail_id:
        # Screens tool inputs and outputs independently of the model content filter.
        payload["policies"] = {"rai_config": {"rai_policy_name": guardrail_id}}
    return payload


def expected_shared_server_labels(entries: list[dict[str, Any]]) -> frozenset[str]:
    return frozenset(entry["serverLabel"] for entry in entries)


def _connector_api_policy(
    *,
    tenant_id: str,
    arm_audience: str,
    api_principal_id: str,
    foundry_principal_id: str,
    apim_principal_id: str,
) -> str:
    values = {
        "tenant": escape(tenant_id),
        "audience": escape(arm_audience),
        "api": escape(api_principal_id),
        "foundry": escape(foundry_principal_id),
        "apim": escape(apim_principal_id),
    }
    return (
        '<policies><inbound><base />'
        f'<validate-azure-ad-token tenant-id="{values["tenant"]}" '
        'output-token-variable-name="validated-token"><audiences>'
        f'<audience>{values["audience"]}</audience></audiences><required-claims>'
        '<claim name="oid" match="any">'
        f'<value>{values["api"]}</value><value>{values["foundry"]}</value>'
        f'<value>{values["apim"]}</value></claim></required-claims>'
        '</validate-azure-ad-token>'
        '<validate-parameters specified-parameter-action="prevent" '
        'unspecified-parameter-action="prevent" errors-variable-name="connector-validation-errors">'
        '<headers specified-parameter-action="ignore" unspecified-parameter-action="ignore" />'
        '</validate-parameters></inbound><backend><base /></backend>'
        '<outbound><base /></outbound><on-error><base /></on-error></policies>'
    )


def _connector_mcp_policy(arm_audience: str) -> str:
    return (
        '<policies><inbound><base />'
        f'<authentication-managed-identity resource="{escape(arm_audience)}" />'
        '</inbound><backend><base /></backend><outbound><base /></outbound>'
        '<on-error><base /></on-error></policies>'
    )


class ApimOnboarder:
    """Minimal ARM client for the APIM resources this onboarding owns."""

    def __init__(
        self,
        credential: TokenCredential,
        *,
        subscription_id: str,
        resource_group: str,
        service_name: str,
        resource_manager_endpoint: str = ARM,
        client: httpx.Client | None = None,
    ) -> None:
        self._credential = credential
        self._resource_manager_endpoint = resource_manager_endpoint.rstrip("/")
        self._base = (
            f"{self._resource_manager_endpoint}/subscriptions/{subscription_id}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{service_name}"
        )
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0))
        self._token = get_bearer_token_provider(
            credential,
            f"{self._resource_manager_endpoint}/.default",
        )

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

    def _get(self, path: str, *, api_version: str = APIM_API_VERSION) -> dict[str, Any]:
        response = self._client.get(
            f"{self._base}{path}",
            params={"api-version": api_version},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise ApimRequestError("GET", path, response)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"GET {path} did not return a JSON object")
        return payload

    def _get_when_ready(
        self,
        path: str,
        *,
        label: str,
        api_version: str = APIM_API_VERSION,
    ) -> dict[str, Any]:
        last_error: ApimRequestError | None = None
        for attempt, delay in enumerate(APIM_READY_RETRY_DELAYS, start=1):
            if delay:
                print(
                    f"{label}: waiting {delay}s for APIM readiness "
                    f"({attempt}/{len(APIM_READY_RETRY_DELAYS)})."
                )
                time.sleep(delay)
            try:
                return self._get(path, api_version=api_version)
            except ApimRequestError as exc:
                if exc.status_code != 429 and exc.status_code < 500:
                    raise
                last_error = exc
        raise RuntimeError(f"{label} did not become ready") from last_error

    def _exists(self, path: str, *, api_version: str = APIM_CORE_API_VERSION) -> bool:
        response = self._client.get(
            f"{self._base}{path}",
            params={"api-version": api_version},
            headers=self._headers(),
        )
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise RuntimeError(f"GET {path} failed [{response.status_code}]: {response.text[:300]}")
        return True

    def _put(
        self,
        path: str,
        body: dict[str, Any],
        *,
        api_version: str = APIM_API_VERSION,
    ) -> httpx.Response:
        response = self._client.put(
            f"{self._base}{path}",
            params={"api-version": api_version},
            headers=self._headers(if_match=True),
            content=json.dumps(body),
        )
        if response.status_code >= 400:
            raise ApimRequestError("PUT", path, response)
        return response

    def _delete_api(self, api_id: str) -> None:
        response = self._client.delete(
            f"{self._base}/apis/{api_id}",
            params={
                "api-version": APIM_API_VERSION,
                "deleteRevisions": "true",
            },
            headers=self._headers(if_match=True),
        )
        if response.status_code not in {200, 202, 204, 404}:
            raise ApimRequestError("DELETE", f"/apis/{api_id}", response)
        for attempt in range(1, 25):
            if not self._exists(f"/apis/{api_id}", api_version=APIM_API_VERSION):
                return
            if attempt < 24:
                time.sleep(5)
        raise RuntimeError(f"APIM API '{api_id}' did not finish deleting")

    def _delete_resource(
        self,
        path: str,
        *,
        api_version: str = APIM_API_VERSION,
    ) -> None:
        response = self._client.delete(
            f"{self._base}{path}",
            params={"api-version": api_version},
            headers=self._headers(if_match=True),
        )
        if response.status_code not in {200, 202, 204, 404}:
            raise ApimRequestError("DELETE", path, response)

    def _delete_connector_tools(self, connectors: list[dict[str, Any]]) -> None:
        """Tools must be removed before the operations they reference."""
        for connector in connectors:
            api_id = connector["apiId"]
            if not self._exists(f"/apis/{api_id}", api_version=APIM_API_VERSION):
                continue
            for tool in self._get(f"/apis/{api_id}/tools").get("value", []):
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self._delete_resource(f"/apis/{api_id}/tools/{tool['name']}")

    def _remove_obsolete_backing_api(self, connectors: list[dict[str, Any]]) -> None:
        if not self._exists(
            f"/apis/{OBSOLETE_MCP_BACKING_API_ID}",
            api_version=APIM_CORE_API_VERSION,
        ):
            return
        print("Removing superseded APIM connector MCP backing API.")
        self._delete_connector_tools(connectors)
        self._delete_api(OBSOLETE_MCP_BACKING_API_ID)

    def _repair_obsolete_connector_facade(self, connectors: list[dict[str, Any]]) -> None:
        if not self._exists(
            f"/apis/{CONNECTOR_API_ID}",
            api_version=APIM_CORE_API_VERSION,
        ):
            return
        items = self._get(
            f"/apis/{CONNECTOR_API_ID}/operations",
            api_version=APIM_CORE_API_VERSION,
        ).get("value", [])
        obsolete = any(
            isinstance(item, dict)
            and (
                str(item.get("name", "")).endswith("Http")
                or str(item.get("properties", {}).get("urlTemplate", "")).startswith(
                    "/mcp/"
                )
            )
            for item in items
        )
        if not obsolete:
            return
        print("Recreating obsolete APIM connector facade before import.")
        self._delete_connector_tools(connectors)
        self._delete_api(CONNECTOR_API_ID)

    def _put_with_retry(
        self,
        path: str,
        body: dict[str, Any],
        *,
        label: str,
        api_version: str = APIM_API_VERSION,
    ) -> httpx.Response:
        last_error: ApimRequestError | None = None
        for attempt, configured_delay in enumerate(APIM_TOOL_RETRY_DELAYS, start=1):
            delay = last_error.retry_after if last_error and last_error.retry_after is not None else configured_delay
            if delay:
                print(
                    f"{label}: waiting {delay}s after a transient APIM failure "
                    f"({attempt}/{len(APIM_TOOL_RETRY_DELAYS)})."
                )
                time.sleep(delay)
            try:
                return self._put(path, body, api_version=api_version)
            except ApimRequestError as exc:
                if exc.status_code not in {409, 429} and exc.status_code < 500:
                    raise
                last_error = exc
        raise RuntimeError(f"{label} failed after bounded APIM retries") from last_error

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

    def _await_operations(
        self,
        api_id: str,
        expected: set[str],
        *,
        attempts: int = 20,
        delay: float = 5.0,
    ) -> None:
        missing = expected
        for attempt in range(1, attempts + 1):
            missing = expected - set(self.operation_names(api_id))
            if not missing:
                return
            if attempt < attempts:
                time.sleep(delay)
        raise RuntimeError(
            f"APIM import for '{api_id}' is missing operations: {sorted(missing)}"
        )

    def _reconcile_connector_tools(
        self,
        connectors: list[dict[str, Any]],
    ) -> dict[str, int]:
        tools: list[dict[str, str]] = json.loads(
            CONNECTOR_MCP_TOOLS.read_text(encoding="utf-8")
        )
        expected: dict[str, set[str]] = {
            connector["apiId"]: set() for connector in connectors
        }
        for tool in tools:
            expected[tool["apiId"]].add(tool["name"])

        current: dict[str, set[str]] = {}
        for api_id in expected:
            items = self._get(f"/apis/{api_id}/tools").get("value", [])
            current[api_id] = {
                item["name"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
        if all(expected[api_id] <= current[api_id] for api_id in expected):
            return {api_id: len(names) for api_id, names in expected.items()}

        for tool in sorted(tools, key=lambda item: (item["apiId"], item["name"])):
            if tool["name"] in current[tool["apiId"]]:
                continue
            self._put_with_retry(
                f"/apis/{tool['apiId']}/tools/{tool['name']}",
                {
                    "properties": {
                        "displayName": tool["displayName"],
                        "description": tool["description"],
                        "operationId": (
                            f"{self._base}/apis/{CONNECTOR_API_ID}/operations/"
                            f"{tool['operationId']}"
                        ),
                    }
                },
                label=f"APIM MCP tool {tool['apiId']}/{tool['name']}",
            )

        last_missing: dict[str, list[str]] = {}
        for attempt, delay in enumerate(APIM_TOOL_VERIFY_DELAYS, start=1):
            if delay:
                print(
                    f"Waiting {delay}s for APIM MCP tool inventory "
                    f"({attempt}/{len(APIM_TOOL_VERIFY_DELAYS)})."
                )
                time.sleep(delay)
            missing: dict[str, list[str]] = {}
            for api_id, expected_names in expected.items():
                items = self._get(f"/apis/{api_id}/tools").get("value", [])
                actual_names = {
                    item["name"]
                    for item in items
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                absent = expected_names - actual_names
                if absent:
                    missing[api_id] = sorted(absent)
            if not missing:
                return {api_id: len(names) for api_id, names in expected.items()}
            last_missing = missing
        raise RuntimeError(
            f"APIM MCP tool inventory did not converge: {last_missing}"
        )

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

    def reconcile_connector_gateway(
        self,
        *,
        tenant_id: str,
        api_principal_id: str,
        foundry_principal_id: str,
        apim_principal_id: str,
    ) -> dict[str, Any]:
        service = self._get_when_ready(
            "",
            label="API Management control plane",
            api_version=APIM_CORE_API_VERSION,
        )
        publisher_email = service.get("properties", {}).get("publisherEmail")
        if not isinstance(publisher_email, str) or not publisher_email:
            raise RuntimeError("API Management returned no publisher email")

        self._put(
            f"/products/{CONNECTOR_PRODUCT_ID}",
            {
                "properties": {
                    "displayName": "Research agent tools",
                    "description": "Governed connector MCP servers consumed by Microsoft Foundry.",
                    "subscriptionRequired": True,
                    "approvalRequired": False,
                    "state": "published",
                }
            },
            api_version=APIM_CORE_API_VERSION,
        )
        self._put(
            f"/subscriptions/{CONNECTOR_SUBSCRIPTION_ID}",
            {
                "properties": {
                    "displayName": "Microsoft Foundry connector MCP access",
                    "scope": f"/products/{CONNECTOR_PRODUCT_ID}",
                    "state": "active",
                    "allowTracing": False,
                }
            },
            api_version=APIM_CORE_API_VERSION,
        )
        self._put(
            f"/namedValues/{CONNECTOR_CONTACT_NAME}",
            {
                "properties": {
                    "displayName": CONNECTOR_CONTACT_NAME,
                    "value": publisher_email,
                    "secret": False,
                    "tags": ["connector"],
                }
            },
            api_version=APIM_CORE_API_VERSION,
        )

        connectors: list[dict[str, Any]] = json.loads(
            CONNECTOR_MCP_CATALOG.read_text(encoding="utf-8")
        )
        for connector in connectors:
            named_value = connector.get("credentialNamedValue")
            if not isinstance(named_value, str) or not named_value:
                continue
            if self._exists(f"/namedValues/{named_value}"):
                continue
            self._put(
                f"/namedValues/{named_value}",
                {
                    "properties": {
                        "displayName": named_value,
                        "value": UNCONFIGURED_CREDENTIAL,
                        "secret": True,
                        "tags": ["connector"],
                    }
                },
                api_version=APIM_CORE_API_VERSION,
            )

        self._repair_obsolete_connector_facade(connectors)
        response = self._put(
            f"/apis/{CONNECTOR_API_ID}",
            {
                "properties": {
                    "apiRevision": "1",
                    "description": "Narrow normalized public research metadata operations.",
                    "displayName": "Research connector facade",
                    "format": "openapi+json",
                    "path": "research-connectors",
                    "protocols": ["https"],
                    "serviceUrl": "https://normalized-connectors.invalid",
                    "subscriptionRequired": False,
                    "type": "http",
                    "value": CONNECTOR_OPENAPI.read_text(encoding="utf-8"),
                }
            },
            api_version=APIM_CORE_API_VERSION,
        )
        self._await_async_operation(response, f"import {CONNECTOR_API_ID}")
        self._await_api(CONNECTOR_API_ID)

        arm_audience = f"{self._resource_manager_endpoint}/"
        self._put(
            f"/apis/{CONNECTOR_API_ID}/policies/policy",
            {
                "properties": {
                    "format": "rawxml",
                    "value": _connector_api_policy(
                        tenant_id=tenant_id,
                        arm_audience=arm_audience,
                        api_principal_id=api_principal_id,
                        foundry_principal_id=foundry_principal_id,
                        apim_principal_id=apim_principal_id,
                    ),
                }
            },
            api_version=APIM_CORE_API_VERSION,
        )
        operation_policies: list[dict[str, str]] = json.loads(
            CONNECTOR_OPERATION_POLICIES.read_text(encoding="utf-8")
        )
        self._await_operations(
            CONNECTOR_API_ID,
            {policy["operationId"] for policy in operation_policies},
        )
        for policy in operation_policies:
            self._put(
                f"/apis/{CONNECTOR_API_ID}/operations/{policy['operationId']}/policies/policy",
                {"properties": {"format": "rawxml", "value": policy["value"]}},
                api_version=APIM_CORE_API_VERSION,
            )

        self._remove_obsolete_backing_api(connectors)

        mcp_policy = _connector_mcp_policy(arm_audience)
        for connector in connectors:
            api_id = connector["apiId"]
            response = self._put(
                f"/apis/{api_id}",
                {
                    "properties": {
                        "type": "mcp",
                        "path": connector["path"],
                        "displayName": connector["displayName"],
                        "description": connector["description"],
                        "protocols": ["https"],
                        "subscriptionRequired": False,
                    }
                },
            )
            self._await_async_operation(response, f"create MCP API {api_id}")
            self._await_api(api_id)

        self._reconcile_connector_tools(connectors)

        for connector in connectors:
            api_id = connector["apiId"]
            self._put(
                f"/apis/{api_id}/policies/policy",
                {"properties": {"format": "rawxml", "value": mcp_policy}},
            )
            self._put(
                f"/products/{CONNECTOR_PRODUCT_ID}/apis/{api_id}",
                {},
                api_version=APIM_CORE_API_VERSION,
            )

        gateway_url = service.get("properties", {}).get("gatewayUrl")
        if not isinstance(gateway_url, str) or not gateway_url:
            raise RuntimeError("API Management returned no gateway URL")
        return {
            "subscriptionId": CONNECTOR_SUBSCRIPTION_ID,
            "mcpUrls": [
                {
                    "id": connector["id"],
                    "endpoint": f"{gateway_url.rstrip('/')}/{connector['path']}/mcp",
                }
                for connector in connectors
            ],
        }

    def operation_names(self, api_id: str) -> list[str]:
        listed = self._get(f"/apis/{api_id}/operations").get("value", [])
        return [item["name"] for item in listed]

    def create_mcp_server(self, entry: dict[str, Any]) -> None:
        response = self._put(
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
        self._await_async_operation(response, f"create MCP API {entry['mcpApiId']}")
        self._await_api(entry["mcpApiId"])

    def attach_tools(self, entry: dict[str, Any], api_id: str, operations: list[str]) -> None:
        for operation in operations:
            self._put_with_retry(
                f"/apis/{entry['mcpApiId']}/tools/{operation}",
                {
                    "properties": {
                        "displayName": operation,
                        "description": f"{entry['displayName']} operation {operation}.",
                        "operationId": f"{self._base}/apis/{api_id}/operations/{operation}",
                    }
                },
                label=f"APIM MCP tool {entry['mcpApiId']}/{operation}",
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
    resource_manager_endpoint: str = ARM,
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
        resource_manager_endpoint=resource_manager_endpoint,
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


def reconcile_connector_gateway(
    credential: TokenCredential,
    *,
    subscription_id: str,
    resource_group: str,
    service_name: str,
    tenant_id: str,
    api_principal_id: str,
    foundry_principal_id: str,
    apim_principal_id: str,
    resource_manager_endpoint: str = ARM,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    onboarder = ApimOnboarder(
        credential,
        subscription_id=subscription_id,
        resource_group=resource_group,
        service_name=service_name,
        resource_manager_endpoint=resource_manager_endpoint,
        client=client,
    )
    try:
        return onboarder.reconcile_connector_gateway(
            tenant_id=tenant_id,
            api_principal_id=api_principal_id,
            foundry_principal_id=foundry_principal_id,
            apim_principal_id=apim_principal_id,
        )
    finally:
        onboarder.close()
