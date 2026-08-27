from __future__ import annotations

import json
import os
import random
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from weakref import WeakKeyDictionary

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchProfile,
)
from research_assistant_core.connector_catalog import connector_definitions

from scripts.azd_env import sync_canonical_azd_outputs
from scripts.provider_onboarding import (
    CONNECTOR_SUBSCRIPTION_ID,
    SHARED_TOOLBOX_NAME,
    reconcile_connector_gateway,
    shared_toolbox_payload,
)

ROOT = Path(__file__).resolve().parents[1]
RETRY_DELAYS = (0, 30, 60, 90, 120)
TOOLBOX_READINESS_RETRY_DELAYS = (0, 10, 20, 40, 60, 60, 60, 60)
AZ_CLI = "az.cmd" if os.name == "nt" else "az"
AZD_CLI = "azd.exe" if os.name == "nt" else "azd"
FOUNDRY_TOOLBOX_SCOPE = "https://ai.azure.com/.default"
APIM_API_VERSION = "2024-05-01"
FOUNDRY_CONNECTION_API_VERSION = "2025-04-01-preview"
APIM_SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"
# The memory store API is versioned separately from the agents API.
FOUNDRY_MEMORY_API_VERSION = "2025-11-15-preview"
MEMORY_STORE_NAME = "research_shared_memory"
MEMORY_DEFAULT_TTL_SECONDS = 2592000


class FoundryProjectUnavailable(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ToolboxProjectUnavailable(FoundryProjectUnavailable):
    pass


class AmbiguousToolboxCreate(RuntimeError):
    pass


_TOKEN_PROVIDERS: WeakKeyDictionary[object, dict[str, Callable[[], str]]] = WeakKeyDictionary()


def _bearer_token(credential: TokenCredential, scope: str) -> str:
    """Reuse one caching token provider per credential and scope.

    ``AzureCliCredential`` shells out to ``az`` on every ``get_token`` call and
    documents that it does not cache, so calling it per request exhausts the CLI.
    ``get_bearer_token_provider`` wraps ``BearerTokenCredentialPolicy``, which is
    the same caching path Azure SDK clients use.
    """
    by_scope = _TOKEN_PROVIDERS.setdefault(credential, {})
    provider = by_scope.get(scope)
    if provider is None:
        provider = get_bearer_token_provider(credential, scope)
        by_scope[scope] = provider
    return provider()

def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required azd environment value is missing: {name}")
    return value


def optional_env(name: str) -> str:
    """Read a value that brownfield environments may not have provisioned yet."""
    return os.getenv(name) or ""


def _resource_manager_endpoint() -> str:
    endpoint = subprocess.run(
        [
            AZ_CLI,
            "cloud",
            "show",
            "--query",
            "endpoints.resourceManager",
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    if not endpoint:
        raise RuntimeError("The active Azure cloud has no ARM endpoint")
    return endpoint.rstrip("/")


def _arm_json_request(
    credential: TokenCredential,
    *,
    method: str,
    url: str,
    resource_manager_endpoint: str,
    payload: dict[str, Any] | None = None,
    if_match: bool = False,
) -> dict[str, Any]:
    token = _bearer_token(credential, f"{resource_manager_endpoint}/.default")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if if_match:
        headers["If-Match"] = "*"
    request = Request(
        url,
        data=(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        ),
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw_body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(
            f"ARM request failed with HTTP {exc.code}: {method} {url}: {detail}"
        ) from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"ARM request failed: {method} {url}") from exc
    if not raw_body:
        return {}
    body = json.loads(raw_body)
    if not isinstance(body, dict):
        raise RuntimeError("ARM response must be a JSON object")
    return body


def apim_mcp_subscription_key(
    credential: TokenCredential,
    *,
    resource_manager_endpoint: str,
) -> str:
    subscription_resource = (
        f"{resource_manager_endpoint}/subscriptions/{required_env('AZURE_SUBSCRIPTION_ID')}"
        f"/resourceGroups/{required_env('AZURE_RESOURCE_GROUP')}"
        "/providers/Microsoft.ApiManagement/service/"
        f"{required_env('AZURE_API_MANAGEMENT_NAME')}/subscriptions/"
        f"{required_env('AZURE_API_MANAGEMENT_MCP_SUBSCRIPTION_ID')}"
    )
    response = _arm_json_request(
        credential,
        method="POST",
        url=f"{subscription_resource}/listSecrets?api-version={APIM_API_VERSION}",
        resource_manager_endpoint=resource_manager_endpoint,
    )
    key = response.get("primaryKey")
    if not isinstance(key, str) or not key:
        raise RuntimeError("APIM MCP subscription returned no primary key")
    return key


def configure_connector_gateway(
    credential: TokenCredential | None = None,
) -> dict[str, Any]:
    """Reconcile mutable APIM connector configuration and publish its outputs."""
    effective_credential = credential or DefaultAzureCredential()
    resource_manager_endpoint = _resource_manager_endpoint()
    result = reconcile_connector_gateway(
        effective_credential,
        subscription_id=required_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=required_env("AZURE_RESOURCE_GROUP"),
        service_name=required_env("AZURE_API_MANAGEMENT_NAME"),
        tenant_id=required_env("AZURE_TENANT_ID"),
        api_principal_id=required_env("AZURE_API_MANAGED_IDENTITY_PRINCIPAL_ID"),
        foundry_principal_id=required_env("AZURE_FOUNDRY_PROJECT_PRINCIPAL_ID"),
        apim_principal_id=required_env("AZURE_API_MANAGEMENT_PRINCIPAL_ID"),
        resource_manager_endpoint=resource_manager_endpoint,
    )
    mcp_urls = json.dumps(result["mcpUrls"], separators=(",", ":"))
    outputs = {
        "AZURE_CONNECTOR_MCP_URLS": mcp_urls,
        "AZURE_API_MANAGEMENT_MCP_SUBSCRIPTION_ID": CONNECTOR_SUBSCRIPTION_ID,
    }
    for key, value in outputs.items():
        os.environ[key] = value
        subprocess.run([AZD_CLI, "env", "set", key, value], check=True)
    return result


def with_rbac_retry[T](label: str, operation: Callable[[], T]) -> T:
    last_error: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            print(f"{label}: waiting {delay}s for RBAC propagation ({attempt}/{len(RETRY_DELAYS)})")
            time.sleep(delay)
        try:
            return operation()
        except HttpResponseError as exc:
            last_error = exc
            if exc.status_code not in (401, 403):
                raise
    raise RuntimeError(f"{label} failed after bounded RBAC retries") from last_error


def create_index(
    endpoint: str,
    index_name: str,
    credential: TokenCredential,
) -> None:
    fields = [
        SearchField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchField(name="source_id", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="source_kind", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchField(
            name="tenant_ids",
            type=SearchFieldDataType.Collection(  # type: ignore[operator]
                SearchFieldDataType.String
            ),
            filterable=True,
        ),
        SearchField(
            name="project_ids",
            type=SearchFieldDataType.Collection(  # type: ignore[operator]
                SearchFieldDataType.String
            ),
            filterable=True,
        ),
        SearchField(
            name="group_ids",
            type=SearchFieldDataType.Collection(  # type: ignore[operator]
                SearchFieldDataType.String
            ),
            filterable=True,
        ),
        SearchField(
            name="access",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SearchField(
            name="year",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SearchField(
            name="provider",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SearchField(
            name="ingestion_status",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(
            name="safety_status",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(
            name="generation_id",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchField(name="title", type=SearchFieldDataType.String, searchable=True),
        SearchField(name="section", type=SearchFieldDataType.String, searchable=True),
        SearchField(name="page_start", type=SearchFieldDataType.Int32, filterable=True),
        SearchField(name="content", type=SearchFieldDataType.String, searchable=True),
        SearchField(name="checksum", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="license", type=SearchFieldDataType.String, filterable=True),
        SearchField(name="version", type=SearchFieldDataType.String, filterable=True),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(  # type: ignore[operator]
                SearchFieldDataType.Single
            ),
            searchable=True,
            vector_search_dimensions=3072,
            vector_search_profile_name="research-vector-profile",
        ),
    ]
    index = SearchIndex(
        name=index_name,
        fields=fields,
        vector_search=VectorSearch(
            algorithms=[HnswAlgorithmConfiguration(name="research-hnsw")],
            profiles=[
                VectorSearchProfile(
                    name="research-vector-profile",
                    algorithm_configuration_name="research-hnsw",
                )
            ],
        ),
        semantic_search=SemanticSearch(
            configurations=[
                SemanticConfiguration(
                    name="research-semantic",
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="title"),
                        content_fields=[SemanticField(field_name="content")],
                        keywords_fields=[SemanticField(field_name="section")],
                    ),
                )
            ],
            default_configuration_name="research-semantic",
        ),
    )
    client = SearchIndexClient(endpoint=endpoint, credential=credential)
    with_rbac_retry("Create Search index", lambda: client.create_or_update_index(index))


def wait_for_acr_pull_roles() -> None:
    acr_id = required_env("AZURE_CONTAINER_REGISTRY_RESOURCE_ID")
    principal = required_env("AZURE_MANAGED_IDENTITY_PRINCIPAL_ID")
    for attempt in range(1, 6):
        role = subprocess.run(
            [
                AZ_CLI,
                "role",
                "assignment",
                "list",
                "--scope",
                acr_id,
                "--assignee-object-id",
                principal,
                "--query",
                "[?roleDefinitionName=='AcrPull'].roleDefinitionName",
                "--output",
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        if role == "AcrPull":
            print("AcrPull confirmed for the Container Apps workload identity.")
            return
        if attempt == 5:
            raise RuntimeError(
                "AcrPull was not visible for the Container Apps workload identity "
                "after five minutes"
            )
        print(
            "Waiting 60s for Container Apps workload identity AcrPull propagation "
            f"({attempt}/5)."
        )
        time.sleep(60)


def connector_connection_payload(
    *,
    target: str,
    subscription_key: str,
) -> dict[str, Any]:
    return {
        "properties": {
            "authType": "CustomKeys",
            "category": "RemoteTool",
            "target": target,
            "credentials": {
                "keys": {APIM_SUBSCRIPTION_HEADER: subscription_key},
            },
            "metadata": {"type": "generic_mcp"},
        }
    }


def connector_mcp_targets(serialized: str) -> dict[str, str]:
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AZURE_CONNECTOR_MCP_URLS must contain a JSON array") from exc
    if not isinstance(payload, list):
        raise RuntimeError("AZURE_CONNECTOR_MCP_URLS must contain a JSON array")

    expected_ids = {connector.id for connector in connector_definitions()}
    targets: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("Connector MCP endpoint entries must be JSON objects")
        connector_id = item.get("id")
        endpoint = item.get("endpoint")
        if not isinstance(connector_id, str) or not isinstance(endpoint, str):
            raise RuntimeError("Connector MCP endpoint entries require string id and endpoint values")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise RuntimeError(
                f"Connector MCP endpoint for '{connector_id}' must be an HTTPS URL without query or fragment"
            )
        if connector_id in targets:
            raise RuntimeError(f"Connector MCP endpoint inventory contains duplicate '{connector_id}' entries")
        targets[connector_id] = endpoint
    if set(targets) != expected_ids:
        missing = sorted(expected_ids - set(targets))
        unexpected = sorted(set(targets) - expected_ids)
        raise RuntimeError(
            "Connector MCP endpoint inventory does not match the governed catalog "
            f"(missing={missing}, unexpected={unexpected})"
        )
    return targets


_TOOLBOX_AGENT_IDS = {
    "research-literature": "literature",
    "research-grant": "grant",
    "research-matching": "matching",
}


def toolbox_version_payload(
    toolbox_name: str,
    mcp_targets: dict[str, str],
) -> dict[str, Any]:
    """Build one complete, immutable Toolbox version from governed metadata."""
    if toolbox_name == "research-dataset":
        return {
            "description": "Sandboxed Foundry data analysis tools",
            "tools": [
                {
                    "type": "code_interpreter",
                    "name": "code_interpreter",
                    "require_approval": "never",
                }
            ],
        }
    try:
        agent_id = _TOOLBOX_AGENT_IDS[toolbox_name]
    except KeyError as exc:
        raise ValueError(f"Unknown governed Toolbox '{toolbox_name}'") from exc
    tools: list[dict[str, str]] = [
        {
            "type": "web_search",
            "name": "web_search",
            "require_approval": "never",
        }
    ]
    for connector in connector_definitions():
        if agent_id not in connector.assigned_agents:
            continue
        tools.append(
            {
                "type": "mcp",
                "server_label": connector.id,
                "server_url": mcp_targets[connector.id],
                "project_connection_id": connector.toolbox_connection_id,
                "require_approval": "never",
            }
        )
    return {
        "description": f"Governed public {agent_id} discovery tools",
        "tools": tools,
    }


def expected_toolbox_tool_names(toolbox_name: str) -> frozenset[str]:
    if toolbox_name == "research-dataset":
        return frozenset({"code_interpreter"})
    try:
        agent_id = _TOOLBOX_AGENT_IDS[toolbox_name]
    except KeyError as exc:
        raise ValueError(f"Unknown governed Toolbox '{toolbox_name}'") from exc
    return frozenset(
        {
            "web_search",
            *{
                f"{connector.id}___{operation.mcp_tool_name}"
                for connector in connector_definitions()
                if agent_id in connector.assigned_agents
                for operation in connector.operations
                if operation.operation_class != "delete"
            },
        }
    )


def _toolbox_base_url(project_endpoint: str, toolbox_name: str) -> str:
    return f"{project_endpoint.rstrip('/')}/toolboxes/{toolbox_name}"


def _toolbox_json_request(
    credential: TokenCredential,
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
    session_id: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    token = _bearer_token(credential, FOUNDRY_TOOLBOX_SCOPE)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = Request(
        url,
        data=(
            None
            if method == "GET"
            else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ),
        headers=headers,
        method=method,
    )
    toolbox_request = "/toolboxes" in url
    toolbox_collection_request = method == "GET" and "/toolboxes?" in url
    toolbox_create_request = method == "POST" and url.endswith("/versions?api-version=v1")
    readiness_request = (
        toolbox_request
        and payload.get("method") in {"initialize", "notifications/initialized", "tools/list"}
    )
    promotion_request = (
        toolbox_request
        and method == "PATCH"
        and "/versions/" not in url
        and set(payload) == {"default_version"}
    )
    memory_store_request = method == "POST" and "/memory_stores" in url
    transient_request = (
        toolbox_collection_request
        or readiness_request
        or promotion_request
        or memory_store_request
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw_body = response.read()
            body = json.loads(raw_body) if raw_body else {}
            if not isinstance(body, dict):
                raise RuntimeError("Foundry Toolbox response must be a JSON object")
            response_session_id = response.headers.get("Mcp-Session-Id")
            return body, response_session_id if isinstance(response_session_id, str) else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        if toolbox_create_request and exc.code in {404, 429, 500, 502, 503, 504}:
            raise AmbiguousToolboxCreate(
                f"Foundry Toolbox version create returned HTTP {exc.code}: {detail}"
            ) from exc
        if transient_request and exc.code in {404, 429, 500, 502, 503, 504}:
            error_type = ToolboxProjectUnavailable if not memory_store_request else FoundryProjectUnavailable
            raise error_type(
                f"Foundry data-plane endpoint returned transient HTTP {exc.code}",
                retry_after_seconds=_parse_retry_after(exc.headers.get("Retry-After")),
            ) from exc
        raise RuntimeError(f"Foundry Toolbox request failed with HTTP {exc.code}: {detail}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Foundry Toolbox request failed") from exc
    except (URLError, OSError) as exc:
        if transient_request:
            error_type = ToolboxProjectUnavailable if not memory_store_request else FoundryProjectUnavailable
            raise error_type("Foundry data-plane endpoint is temporarily unreachable") from exc
        raise RuntimeError("Foundry Toolbox request failed") from exc


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _assert_mcp_success(payload: dict[str, Any], operation: str) -> dict[str, Any]:
    if "error" in payload:
        if _is_transient_builtin_tool_source_error(payload["error"], operation):
            raise ToolboxProjectUnavailable(
                "Foundry built-in Toolbox tool sources are not ready"
            )
        raise RuntimeError(f"Foundry Toolbox MCP {operation} failed: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Foundry Toolbox MCP {operation} returned no result")
    return result


def _is_transient_builtin_tool_source_error(error: object, operation: str) -> bool:
    if operation != "tools/list" or not isinstance(error, dict) or error.get("code") != -32007:
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    details_start = message.find('{"errors"')
    if details_start < 0:
        return False
    try:
        details = json.loads(message[details_start:])
    except json.JSONDecodeError:
        return False
    failures = details.get("errors") if isinstance(details, dict) else None
    if not isinstance(failures, list) or not failures:
        return False
    built_in_sources = {"web_search", "code_interpreter"}
    for failure in failures:
        if not isinstance(failure, dict) or failure.get("name") not in built_in_sources:
            return False
        source_error = failure.get("error")
        if not isinstance(source_error, dict) or source_error.get("code") != "NOT_FOUND":
            return False
        source_message = source_error.get("message")
        if not isinstance(source_message, str) or "RAPI MCP endpoint returned HTTP 404" not in source_message:
            return False
    return True


def _validate_toolbox_version(
    credential: TokenCredential,
    *,
    project_endpoint: str,
    toolbox_name: str,
    version: str,
) -> None:
    endpoint = f"{_toolbox_base_url(project_endpoint, toolbox_name)}/versions/{version}/mcp?api-version=v1"
    initialize, session_id = _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "research-assistant-provisioning", "version": "1.0"},
            },
        },
    )
    _assert_mcp_success(initialize, "initialize")
    _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    listed, _ = _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    result = _assert_mcp_success(listed, "tools/list")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("Foundry Toolbox MCP tools/list returned no tools array")
    if not tools:
        raise ToolboxProjectUnavailable(
            f"Foundry Toolbox {toolbox_name} version {version} advertised no tools yet"
        )
    names: set[str] = set()
    for tool in tools:
        if isinstance(tool, dict):
            name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
    expected = expected_toolbox_tool_names(toolbox_name)
    if names != expected:
        raise RuntimeError(
            f"Foundry Toolbox {toolbox_name} version {version} inventory does not match "
            f"the governed catalog (missing={sorted(expected - names)}, unexpected={sorted(names - expected)})"
        )


def _reconcile_toolbox(
    credential: TokenCredential,
    *,
    toolbox_name: str,
    project_endpoint: str,
    mcp_targets: dict[str, str],
) -> str:
    base_url = _toolbox_base_url(project_endpoint, toolbox_name)
    version = _create_or_recover_toolbox_version(
        credential,
        toolbox_name=toolbox_name,
        project_endpoint=project_endpoint,
        desired=toolbox_version_payload(toolbox_name, mcp_targets),
    )
    with_toolbox_readiness_retry(
        toolbox_name,
        lambda: _validate_toolbox_version(
            credential,
            project_endpoint=project_endpoint,
            toolbox_name=toolbox_name,
            version=version,
        ),
    )
    _promote_toolbox_version(
        credential,
        toolbox_name=toolbox_name,
        project_endpoint=project_endpoint,
        version=version,
    )
    return f"{base_url}/mcp?api-version=v1"


def with_toolbox_readiness_retry[T](
    toolbox_name: str,
    operation: Callable[[], T],
    *,
    phase: str = "version readiness",
    delays: tuple[float, ...] = TOOLBOX_READINESS_RETRY_DELAYS,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
) -> T:
    return with_foundry_readiness_retry(
        f"Toolbox {toolbox_name}",
        operation,
        phase=phase,
        delays=delays,
        sleep=sleep,
        jitter=jitter,
    )


def with_foundry_readiness_retry[T](
    resource_label: str,
    operation: Callable[[], T],
    *,
    phase: str,
    delays: tuple[float, ...] = TOOLBOX_READINESS_RETRY_DELAYS,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
) -> T:
    effective_sleep = sleep or time.sleep
    effective_jitter = jitter or random.uniform
    last_error: FoundryProjectUnavailable | None = None
    started = time.monotonic()
    for attempt, base_delay in enumerate(delays, start=1):
        if base_delay:
            retry_after = last_error.retry_after_seconds if last_error else None
            delay = max(base_delay, retry_after or 0.0)
            delay += effective_jitter(0.0, min(delay * 0.2, 5.0))
            print(
                f"Waiting {delay:.1f}s for Foundry {resource_label} {phase} "
                f"({attempt}/{len(delays)})."
            )
            effective_sleep(delay)
        try:
            result = operation()
            if attempt > 1:
                print(
                    f"Foundry {resource_label} {phase} succeeded after {attempt} attempts "
                    f"over {time.monotonic() - started:.1f}s."
                )
            return result
        except FoundryProjectUnavailable as exc:
            last_error = exc
    raise RuntimeError(
        f"Foundry {resource_label} {phase} failed after {len(delays)} attempts "
        f"over {time.monotonic() - started:.1f}s"
    ) from last_error


def _promote_toolbox_version(
    credential: TokenCredential,
    *,
    toolbox_name: str,
    project_endpoint: str,
    version: str,
) -> None:
    base_url = _toolbox_base_url(project_endpoint, toolbox_name)
    with_toolbox_readiness_retry(
        toolbox_name,
        lambda: _toolbox_json_request(
            credential,
            method="PATCH",
            url=f"{base_url}?api-version=v1",
            payload={"default_version": version},
        ),
        phase="default-version promotion",
    )


def _wait_for_toolbox_service_ready(
    credential: TokenCredential,
    *,
    project_endpoint: str,
) -> None:
    collection_url = f"{project_endpoint.rstrip('/')}/toolboxes?api-version=v1"
    with_toolbox_readiness_retry(
        "service",
        lambda: _toolbox_json_request(
            credential,
            method="GET",
            url=collection_url,
            payload={},
        ),
        phase="control-plane routing",
    )


def _matching_toolbox_version(
    credential: TokenCredential,
    *,
    toolbox_name: str,
    project_endpoint: str,
    desired: dict[str, Any],
) -> str | None:
    base_url = _toolbox_base_url(project_endpoint, toolbox_name)
    try:
        payload, _ = _toolbox_json_request(
            credential,
            method="GET",
            url=f"{base_url}/versions?api-version=v1",
            payload={},
        )
    except RuntimeError as exc:
        if "HTTP 404" in str(exc):
            return None
        raise
    versions = payload.get("data")
    if not isinstance(versions, list):
        raise RuntimeError(f"Foundry Toolbox {toolbox_name} version list returned no data array")
    matches: list[tuple[int, str]] = []
    for candidate in versions:
        if not isinstance(candidate, dict):
            continue
        version = candidate.get("version")
        if not isinstance(version, str) or not version:
            continue
        if any(candidate.get(key) != value for key, value in desired.items()):
            continue
        created_at = candidate.get("created_at")
        matches.append((created_at if isinstance(created_at, int) else 0, version))
    return max(matches)[1] if matches else None


def _create_or_recover_toolbox_version(
    credential: TokenCredential,
    *,
    toolbox_name: str,
    project_endpoint: str,
    desired: dict[str, Any],
) -> str:
    existing = _matching_toolbox_version(
        credential,
        toolbox_name=toolbox_name,
        project_endpoint=project_endpoint,
        desired=desired,
    )
    if existing:
        print(f"Reusing exact Foundry Toolbox {toolbox_name} version {existing}.")
        return existing

    _wait_for_toolbox_service_ready(credential, project_endpoint=project_endpoint)
    base_url = _toolbox_base_url(project_endpoint, toolbox_name)
    try:
        created, _ = _toolbox_json_request(
            credential,
            method="POST",
            url=f"{base_url}/versions?api-version=v1",
            payload=desired,
        )
    except AmbiguousToolboxCreate as exc:
        def recover() -> str:
            version = _matching_toolbox_version(
                credential,
                toolbox_name=toolbox_name,
                project_endpoint=project_endpoint,
                desired=desired,
            )
            if not version:
                raise ToolboxProjectUnavailable(
                    f"Foundry Toolbox {toolbox_name} create outcome is not visible yet"
                )
            return version

        try:
            recovered = with_toolbox_readiness_retry(
                toolbox_name,
                recover,
                phase="ambiguous create reconciliation",
            )
        except RuntimeError:
            raise RuntimeError(
                f"Could not reconcile the ambiguous Foundry Toolbox {toolbox_name} create"
            ) from exc
        print(f"Recovered Foundry Toolbox {toolbox_name} version {recovered} after an ambiguous create.")
        return recovered

    version = created.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"Foundry Toolbox {toolbox_name} version creation returned no version")
    return version


def configure_connector_connections(
    credential: TokenCredential | None = None,
) -> dict[str, str]:
    project_id = required_env("AZURE_AI_PROJECT_ID")
    mcp_targets = connector_mcp_targets(required_env("AZURE_CONNECTOR_MCP_URLS"))
    effective_credential = credential or DefaultAzureCredential()
    resource_manager_endpoint = _resource_manager_endpoint()
    subscription_key = apim_mcp_subscription_key(
        effective_credential,
        resource_manager_endpoint=resource_manager_endpoint,
    )
    for connector in connector_definitions():
        connection_url = (
            f"{resource_manager_endpoint}{project_id}/connections/{connector.toolbox_connection_id}"
            f"?api-version={FOUNDRY_CONNECTION_API_VERSION}"
        )
        _arm_json_request(
            effective_credential,
            method="PUT",
            url=connection_url,
            resource_manager_endpoint=resource_manager_endpoint,
            payload=connector_connection_payload(
                target=mcp_targets[connector.id],
                subscription_key=subscription_key,
            ),
        )
    return mcp_targets


def _shared_toolbox_tool_names(
    credential: TokenCredential,
    *,
    project_endpoint: str,
    version: str | None,
) -> frozenset[str]:
    version_path = f"/versions/{version}" if version else ""
    endpoint = f"{_toolbox_base_url(project_endpoint, SHARED_TOOLBOX_NAME)}{version_path}/mcp?api-version=v1"
    initialize, session_id = _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "research-assistant-provisioning", "version": "1.0"},
            },
        },
    )
    _assert_mcp_success(initialize, "initialize")
    _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    listed, _ = _toolbox_json_request(
        credential,
        method="POST",
        url=endpoint,
        session_id=session_id,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    result = _assert_mcp_success(listed, "tools/list")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("Shared Toolbox version advertised no tools")
    if not tools:
        raise ToolboxProjectUnavailable(
            f"Shared Toolbox version {version} advertised no tools yet"
        )
    return frozenset(
        tool["name"] for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    )


def expected_shared_tool_names() -> frozenset[str]:
    return frozenset(
        {
            "tool_search",
            "call_tool",
            "web_search",
            "code_interpreter",
            *{
                f"{connector.id}___{operation.mcp_tool_name}"
                for connector in connector_definitions()
                for operation in connector.operations
                if operation.operation_class != "delete"
            },
        }
    )


def _reconcile_shared_toolbox(
    credential: TokenCredential,
    *,
    project_endpoint: str,
    connector_targets: dict[str, str],
    guardrail_id: str = "",
) -> str:
    base_url = _toolbox_base_url(project_endpoint, SHARED_TOOLBOX_NAME)
    version = _create_or_recover_toolbox_version(
        credential,
        toolbox_name=SHARED_TOOLBOX_NAME,
        project_endpoint=project_endpoint,
        desired=shared_toolbox_payload(
            connector_targets,
            guardrail_id,
        ),
    )
    names = with_toolbox_readiness_retry(
        SHARED_TOOLBOX_NAME,
        lambda: _shared_toolbox_tool_names(
            credential,
            project_endpoint=project_endpoint,
            version=version,
        ),
    )
    expected = expected_shared_tool_names()
    if names != expected:
        raise RuntimeError(
            f"Shared Toolbox version {version} inventory does not match the governed catalog "
            f"(missing={sorted(expected - names)}, unexpected={sorted(names - expected)})"
        )
    print(f"Shared Toolbox version {version} advertises {len(names)} tool(s).")
    _promote_toolbox_version(
        credential,
        toolbox_name=SHARED_TOOLBOX_NAME,
        project_endpoint=project_endpoint,
        version=version,
    )
    consumer_names = with_toolbox_readiness_retry(
        SHARED_TOOLBOX_NAME,
        lambda: _shared_toolbox_tool_names(
            credential,
            project_endpoint=project_endpoint,
            version=None,
        ),
        phase="consumer endpoint activation",
    )
    if consumer_names != expected:
        raise RuntimeError(
            "Shared Toolbox consumer inventory does not match the promoted version "
            f"(missing={sorted(expected - consumer_names)}, "
            f"unexpected={sorted(consumer_names - expected)})"
        )
    print(f"Shared Toolbox consumer endpoint advertises {len(consumer_names)} tool(s).")
    return f"{base_url}/mcp?api-version=v1"


def configure_shared_toolbox(
    credential: TokenCredential | None = None,
    *,
    connector_targets: dict[str, str] | None = None,
) -> str:
    """Publish the bounded connector MCP surface through one shared Toolbox."""
    effective_credential = credential or DefaultAzureCredential()
    project_endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT")
    governed_targets = connector_targets or connector_mcp_targets(
        required_env("AZURE_CONNECTOR_MCP_URLS")
    )

    endpoint = _reconcile_shared_toolbox(
        effective_credential,
        project_endpoint=project_endpoint,
        connector_targets=governed_targets,
        guardrail_id=optional_env("AZURE_AGENTIC_GUARDRAIL_ID"),
    )
    subprocess.run([AZD_CLI, "env", "set", "TOOLBOX_SHARED_MCP_ENDPOINT", endpoint], check=True)
    return endpoint


def configure_agent_memory(credential: TokenCredential | None = None) -> str:
    """Provision the shared Foundry memory store and publish its name to azd.

    Hosted agents reach memory through the memory store REST API rather than the
    ``memory_search_preview`` tool, which Foundry documents for prompt agents only.
    """
    chat_deployment = optional_env("AZURE_AI_CHAT_DEPLOYMENT_NAME")
    embedding_deployment = optional_env("AZURE_AI_EMBEDDING_DEPLOYMENT_NAME")
    if not chat_deployment or not embedding_deployment:
        print("Skipping memory store: chat or embedding deployment name is unavailable.")
        return ""

    project_endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT")
    effective_credential = credential or DefaultAzureCredential()
    url = (
        f"{project_endpoint.rstrip('/')}/memory_stores"
        f"?api-version={FOUNDRY_MEMORY_API_VERSION}"
    )
    def ensure_memory_store() -> None:
        try:
            _toolbox_json_request(
                effective_credential,
                method="POST",
                url=url,
                payload={
                    "name": MEMORY_STORE_NAME,
                    "description": "Shared research memory for user profile, chat summary, and procedural recall.",
                    "definition": {
                        "kind": "default",
                        "chat_model": chat_deployment,
                        "embedding_model": embedding_deployment,
                        "options": {
                            "chat_summary_enabled": True,
                            "user_profile_enabled": True,
                            "procedural_memory_enabled": True,
                            "default_ttl_seconds": MEMORY_DEFAULT_TTL_SECONDS,
                            "user_profile_details": (
                                "Store research interests and workflow preferences only. "
                                "Never store credentials, precise location, financial, or health data."
                            ),
                        },
                    },
                },
            )
        except RuntimeError as exc:
            expected = f"Memory Store with Name {MEMORY_STORE_NAME} already exists"
            if expected not in str(exc):
                raise

    with_foundry_readiness_retry(
        f"memory store {MEMORY_STORE_NAME}",
        ensure_memory_store,
        phase="upsert",
    )
    subprocess.run([AZD_CLI, "env", "set", "MEMORY_STORE_NAME", MEMORY_STORE_NAME], check=True)
    print(f"Memory store {MEMORY_STORE_NAME} is configured.")
    return MEMORY_STORE_NAME


LOCAL_ENV_BEGIN = "# >>> azd postprovision (managed) >>>"
LOCAL_ENV_END = "# <<< azd postprovision (managed) <<<"
LOCAL_WEB_ENV_BEGIN = "# >>> azd live backend (managed) >>>"
LOCAL_WEB_ENV_END = "# <<< azd live backend (managed) <<<"
LOCAL_ENV_BINDINGS = {
    "FOUNDRY_PROJECT_ENDPOINT": "FOUNDRY_PROJECT_ENDPOINT",
    "RESEARCH_WORKSPACE_TENANT_ID": "AZURE_TENANT_ID",
    "RESEARCH_WORKSPACE_PROJECT_ID": "AZURE_AI_PROJECT_NAME",
    "AZURE_COSMOS_ENDPOINT": "AZURE_COSMOS_ENDPOINT",
    "AZURE_SEARCH_ENDPOINT": "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_INDEX_NAME": "AZURE_SEARCH_INDEX_NAME",
    "AZURE_STORAGE_BLOB_ENDPOINT": "AZURE_STORAGE_BLOB_ENDPOINT",
}


def write_local_env() -> None:
    """Refresh the managed block of the repo-root ``.env`` from azd outputs.

    Without this, a developer running the API locally after ``azd up`` sees an
    empty Agent Studio and no way to discover why: the values exist only in
    ``.azure/<env>/.env``, which the app never reads. Rewriting the block on
    every provision also repoints a checkout that still references a torn-down
    environment. Lines outside the markers are preserved.
    """
    managed = [
        f'{target}="{os.environ[source]}"'
        for target, source in LOCAL_ENV_BINDINGS.items()
        if os.environ.get(source)
    ]
    if not managed:
        return

    env_path = ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    preserved: list[str] = []
    inside = False
    for line in existing:
        if line.strip() == LOCAL_ENV_BEGIN:
            inside = True
        elif line.strip() == LOCAL_ENV_END:
            inside = False
        elif not inside:
            preserved.append(line)

    while preserved and not preserved[-1].strip():
        preserved.pop()
    block = [LOCAL_ENV_BEGIN, *managed, LOCAL_ENV_END]
    content = "\n".join([*preserved, *([""] if preserved else []), *block])
    env_path.write_text(content + "\n", encoding="utf-8")
    print(f"Wrote {len(managed)} local development value(s) to {env_path}.")


def write_local_web_env() -> None:
    web_url = os.environ.get("WEB_URL") or os.environ.get("SERVICE_WEB_URI")
    if not web_url:
        return
    env_path = ROOT / "apps" / "web" / ".env.local"
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    preserved: list[str] = []
    inside = False
    for line in existing:
        if line.strip() == LOCAL_WEB_ENV_BEGIN:
            inside = True
        elif line.strip() == LOCAL_WEB_ENV_END:
            inside = False
        elif not inside:
            preserved.append(line)
    while preserved and not preserved[-1].strip():
        preserved.pop()
    backend_url = f"{web_url.rstrip('/')}/api/backend"
    block = [
        LOCAL_WEB_ENV_BEGIN,
        f'INTERNAL_API_URL="{backend_url}"',
        LOCAL_WEB_ENV_END,
    ]
    content = "\n".join([*preserved, *([""] if preserved else []), *block])
    env_path.write_text(content + "\n", encoding="utf-8")
    print(f"Configured the local web app to use the deployed live backend via {env_path}.")


def main() -> None:
    sync_canonical_azd_outputs()
    write_local_env()
    write_local_web_env()
    credential = DefaultAzureCredential()
    endpoint = required_env("AZURE_SEARCH_ENDPOINT")
    index_name = required_env("AZURE_SEARCH_INDEX_NAME")

    create_index(endpoint, index_name, credential)
    configure_connector_gateway(credential)
    connector_targets = configure_connector_connections(credential)
    configure_shared_toolbox(credential, connector_targets=connector_targets)
    configure_agent_memory(credential)
    wait_for_acr_pull_roles()
    print(f"Configured the empty evidence index {index_name}; ingestion is explicit.")


if __name__ == "__main__":
    main()
