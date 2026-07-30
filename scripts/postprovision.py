from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from weakref import WeakKeyDictionary

from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.search.documents import SearchClient
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
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI
from research_assistant_core.connector_catalog import connector_definitions

from scripts.azd_env import sync_canonical_azd_outputs
from scripts.provider_onboarding import (
    SHARED_TOOLBOX_NAME,
    mcp_endpoint,
    onboard_provider_apis,
    provider_connection_id,
    provider_manifest,
    shared_toolbox_payload,
)

ROOT = Path(__file__).resolve().parents[1]
RETRY_DELAYS = (0, 30, 60, 90, 120)
TOOLBOX_PROJECT_RETRY_DELAYS = (0, 15, 30, 60, 90, 120, 180, 240, 300)
AZ_CLI = "az.cmd" if os.name == "nt" else "az"
AZD_CLI = "azd.exe" if os.name == "nt" else "azd"
FOUNDRY_TOOLBOX_SCOPE = "https://ai.azure.com/.default"
APIM_API_VERSION = "2024-05-01"
APIM_MCP_API_VERSION = "2025-09-01-preview"
FOUNDRY_CONNECTION_API_VERSION = "2025-04-01-preview"
APIM_SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"
CONNECTOR_API_ID = "research-connectors-v1"
CONNECTOR_MCP_TOOLS_PATH = ROOT / "infra" / "connector-mcp-tools.json"
APIM_TOOL_RETRY_DELAYS = (0, 5, 10, 20, 30, 60)
# The memory store API is versioned separately from the agents API.
FOUNDRY_MEMORY_API_VERSION = "2025-11-15-preview"
MEMORY_STORE_NAME = "research_shared_memory"
MEMORY_DEFAULT_TTL_SECONDS = 2592000


class ToolboxProjectUnavailable(RuntimeError):
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
) -> dict[str, Any]:
    token = _bearer_token(credential, f"{resource_manager_endpoint}/.default")
    request = Request(
        url,
        data=(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else b""
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"ARM request failed with HTTP {exc.code}: {method} {url}") from exc
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


def connector_mcp_tool_catalog() -> tuple[dict[str, str], ...]:
    payload = json.loads(CONNECTOR_MCP_TOOLS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("Connector MCP tool catalog must be a JSON array")
    required = {"apiId", "name", "displayName", "description", "operationId"}
    tools: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for item in payload:
        if not isinstance(item, dict) or set(item) != required:
            raise RuntimeError("Connector MCP tool catalog contains an invalid entry")
        if not all(isinstance(item[field], str) and item[field] for field in required):
            raise RuntimeError("Connector MCP tool catalog fields must be non-empty strings")
        tool = {field: item[field] for field in required}
        identity = (tool["apiId"], tool["name"])
        if identity in identities:
            raise RuntimeError(f"Connector MCP tool catalog duplicates {identity[0]}/{identity[1]}")
        identities.add(identity)
        tools.append(tool)
    expected = {
        (connector.apim_mcp_api_id, operation.mcp_tool_name, operation.id)
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    }
    actual = {(tool["apiId"], tool["name"], tool["operationId"]) for tool in tools}
    if actual != expected:
        raise RuntimeError("Connector MCP tool catalog does not match the governed connector catalog")
    return tuple(tools)


def configure_connector_mcp_tools(
    credential: TokenCredential | None = None,
) -> dict[str, int]:
    """Upsert preview APIM tool children sequentially and verify inventory."""
    effective_credential = credential or DefaultAzureCredential()
    resource_manager_endpoint = _resource_manager_endpoint()
    service_base = (
        f"{resource_manager_endpoint}/subscriptions/{required_env('AZURE_SUBSCRIPTION_ID')}"
        f"/resourceGroups/{required_env('AZURE_RESOURCE_GROUP')}"
        "/providers/Microsoft.ApiManagement/service/"
        f"{required_env('AZURE_API_MANAGEMENT_NAME')}"
    )
    tools = connector_mcp_tool_catalog()
    expected: dict[str, set[str]] = {}
    for tool in tools:
        expected.setdefault(tool["apiId"], set()).add(tool["name"])
        _arm_json_request(
            effective_credential,
            method="PUT",
            url=(
                f"{service_base}/apis/{tool['apiId']}/tools/{tool['name']}"
                f"?api-version={APIM_MCP_API_VERSION}"
            ),
            resource_manager_endpoint=resource_manager_endpoint,
            payload={
                "properties": {
                    "displayName": tool["displayName"],
                    "description": tool["description"],
                    "operationId": (
                        f"{service_base}/apis/{CONNECTOR_API_ID}/operations/"
                        f"{tool['operationId']}"
                    ),
                }
            },
        )

    last_missing: dict[str, list[str]] = {}
    for attempt, delay in enumerate(APIM_TOOL_RETRY_DELAYS, start=1):
        if delay:
            print(
                f"Waiting {delay}s for APIM MCP tool inventory "
                f"({attempt}/{len(APIM_TOOL_RETRY_DELAYS)})."
            )
            time.sleep(delay)
        missing: dict[str, list[str]] = {}
        for api_id, expected_names in expected.items():
            try:
                response = _arm_json_request(
                    effective_credential,
                    method="GET",
                    url=f"{service_base}/apis/{api_id}/tools?api-version={APIM_MCP_API_VERSION}",
                    resource_manager_endpoint=resource_manager_endpoint,
                )
            except RuntimeError:
                missing[api_id] = sorted(expected_names)
                continue
            items = response.get("value")
            actual_names = {
                item["name"]
                for item in items
                if isinstance(items, list)
                and isinstance(item, dict)
                and isinstance(item.get("name"), str)
            } if isinstance(items, list) else set()
            absent = expected_names - actual_names
            if absent:
                missing[api_id] = sorted(absent)
        if not missing:
            return {api_id: len(names) for api_id, names in expected.items()}
        last_missing = missing
    raise RuntimeError(f"APIM MCP tool inventory did not converge: {last_missing}")


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


def load_documents(
    *,
    tenant_id: str = "demo",
    project_id: str = "demo-project",
) -> list[dict[str, Any]]:
    path = ROOT / "sample_data" / "evidence.json"
    documents = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("sample_data/evidence.json must contain a non-empty JSON array")
    for document in documents:
        source_kind = str(document.get("source_kind", ""))
        is_public = source_kind in {"paper", "grant"}
        document["tenant_ids"] = [tenant_id]
        document["project_ids"] = [project_id]
        document.setdefault("group_ids", [] if is_public else ["researchers"])
        document.setdefault("access", "public" if is_public else "internal")
        document.setdefault(
            "year",
            2025
            if document.get("source_id") == "paper-rag"
            else 2024
            if document.get("source_id") == "paper-workflow"
            else 2026,
        )
        document.setdefault(
            "provider",
            "PubMed" if source_kind == "paper" else "Grants.gov" if source_kind == "grant" else "Institutional Library",
        )
        document.setdefault("ingestion_status", "ready")
        document.setdefault("safety_status", "safe")
        document.setdefault("generation_id", "seed-v1")
    return documents


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


def embed_documents(
    documents: list[dict[str, Any]],
    credential: TokenCredential,
) -> None:
    token_provider = get_bearer_token_provider(
        credential,
        "https://cognitiveservices.azure.com/.default",
    )
    client = AzureOpenAI(
        azure_endpoint=required_env("AZURE_OPENAI_ENDPOINT"),
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    )
    response = client.embeddings.create(
        model=required_env("AZURE_AI_EMBEDDING_DEPLOYMENT_NAME"),
        input=[document["content"] for document in documents],
    )
    for document, embedding in zip(documents, response.data, strict=True):
        document["content_vector"] = embedding.embedding


def upload_search_documents(
    endpoint: str,
    index_name: str,
    documents: list[dict[str, Any]],
    credential: TokenCredential,
) -> None:
    client = SearchClient(endpoint=endpoint, index_name=index_name, credential=credential)
    result = with_rbac_retry(
        "Upload Search documents",
        lambda: client.upload_documents(documents=documents),
    )
    failures = [item for item in result if not item.succeeded]
    if failures:
        raise RuntimeError(f"Search upload failed for {[item.key for item in failures]}")


def storage_public_network_access() -> str:
    completed = subprocess.run(
        [
            AZ_CLI,
            "storage",
            "account",
            "show",
            "--resource-group",
            required_env("AZURE_RESOURCE_GROUP"),
            "--name",
            required_env("AZURE_STORAGE_ACCOUNT_NAME"),
            "--query",
            "publicNetworkAccess",
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def upload_source_artifacts(credential: TokenCredential) -> bool:
    if storage_public_network_access().lower() == "disabled":
        print(
            "Blob archival skipped: Azure Policy disables Storage public network access. "
            "The synthetic evidence remains indexed in Azure AI Search."
        )
        return False

    endpoint = required_env("AZURE_STORAGE_BLOB_ENDPOINT")
    container = required_env("AZURE_STORAGE_SOURCE_CONTAINER")
    service = BlobServiceClient(account_url=endpoint, credential=credential)
    container_client = service.get_container_client(container)

    def upload() -> None:
        for path in sorted((ROOT / "sample_data").iterdir()):
            if path.is_file():
                container_client.upload_blob(
                    name=f"sample/{path.name}",
                    data=path.read_bytes(),
                    overwrite=True,
                    metadata={"fixture": "true", "license": "CC0-synthetic"},
                )

    with_rbac_retry("Upload source artifacts", upload)
    return True


def wait_for_acr_pull_roles() -> None:
    resource_group = required_env("AZURE_RESOURCE_GROUP")
    acr_id = required_env("AZURE_CONTAINER_REGISTRY_RESOURCE_ID")
    registry = required_env("AZURE_CONTAINER_REGISTRY_ENDPOINT")
    targets = [
        required_env("SERVICE_WEB_NAME"),
        required_env("SERVICE_API_NAME"),
    ]
    for app_name in targets:
        registry_identity = subprocess.run(
            [
                AZ_CLI,
                "containerapp",
                "show",
                "--resource-group",
                resource_group,
                "--name",
                app_name,
                "--query",
                (
                    "properties.configuration.registries"
                    f"[?server=='{registry}'].identity | [0]"
                ),
                "--output",
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        if not registry_identity:
            raise RuntimeError(f"Container App {app_name} has no ACR identity")

        if registry_identity == "system":
            principal = subprocess.run(
                [
                    AZ_CLI,
                    "containerapp",
                    "identity",
                    "show",
                    "--resource-group",
                    resource_group,
                    "--name",
                    app_name,
                    "--query",
                    "principalId",
                    "--output",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
        else:
            principal = subprocess.run(
                [
                    AZ_CLI,
                    "identity",
                    "show",
                    "--ids",
                    registry_identity,
                    "--query",
                    "principalId",
                    "--output",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
        if not principal:
            raise RuntimeError(
                f"Container App {app_name} ACR identity has no principal"
            )

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
                print(f"AcrPull confirmed for {app_name}.")
                break
            if attempt == 5:
                raise RuntimeError(
                    f"AcrPull was not visible for {app_name} after five minutes"
                )
            print(
                f"Waiting 60s for {app_name} AcrPull propagation "
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


def provider_connection_payload(*, target: str) -> dict[str, Any]:
    """Anonymous connection for the public provider APIs fronted by APIM.

    Foundry rejects the ARM audience for non-ARM hosts, and these gateway MCP
    servers expose read-only public research APIs with no subscription key.
    """
    return {
        "properties": {
            "authType": "None",
            "category": "RemoteTool",
            "target": target,
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
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method=method,
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
        if exc.code == 404:
            raise ToolboxProjectUnavailable("Foundry project or Toolbox is not ready") from exc
        detail = exc.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"Foundry Toolbox request failed with HTTP {exc.code}: {detail}") from exc
    except (URLError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("Foundry Toolbox request failed") from exc


def _assert_mcp_success(payload: dict[str, Any], operation: str) -> dict[str, Any]:
    if "error" in payload:
        raise RuntimeError(f"Foundry Toolbox MCP {operation} failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Foundry Toolbox MCP {operation} returned no result")
    return result


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
    created, _ = _toolbox_json_request(
        credential,
        method="POST",
        url=f"{base_url}/versions?api-version=v1",
        payload=toolbox_version_payload(toolbox_name, mcp_targets),
    )
    version = created.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"Foundry Toolbox {toolbox_name} version creation returned no version")
    _validate_toolbox_version(
        credential,
        project_endpoint=project_endpoint,
        toolbox_name=toolbox_name,
        version=version,
    )
    _toolbox_json_request(
        credential,
        method="PATCH",
        url=f"{base_url}?api-version=v1",
        payload={"default_version": version},
    )
    return f"{base_url}/mcp?api-version=v1"


def with_toolbox_project_retry[T](
    toolbox_name: str,
    operation: Callable[[], T],
) -> T:
    last_error: ToolboxProjectUnavailable | None = None
    for attempt, delay in enumerate(TOOLBOX_PROJECT_RETRY_DELAYS, start=1):
        if delay:
            print(
                f"Waiting {delay}s for Foundry project Toolbox readiness "
                f"({attempt}/{len(TOOLBOX_PROJECT_RETRY_DELAYS)})."
            )
            time.sleep(delay)
        try:
            return operation()
        except ToolboxProjectUnavailable as exc:
            last_error = exc
    raise RuntimeError(
        f"Foundry project did not become ready for Toolbox {toolbox_name} "
        f"after {sum(TOOLBOX_PROJECT_RETRY_DELAYS)}s"
    ) from last_error


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
    version: str,
) -> frozenset[str]:
    endpoint = (
        f"{_toolbox_base_url(project_endpoint, SHARED_TOOLBOX_NAME)}"
        f"/versions/{version}/mcp?api-version=v1"
    )
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
    if not isinstance(tools, list) or not tools:
        raise RuntimeError("Shared Toolbox version advertised no tools")
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
    gateway_url: str,
    entries: list[dict[str, Any]],
    connector_targets: dict[str, str],
    guardrail_id: str = "",
) -> str:
    base_url = _toolbox_base_url(project_endpoint, SHARED_TOOLBOX_NAME)
    created, _ = _toolbox_json_request(
        credential,
        method="POST",
        url=f"{base_url}/versions?api-version=v1",
        payload=shared_toolbox_payload(
            gateway_url,
            entries,
            connector_targets,
            guardrail_id,
        ),
    )
    version = created.get("version")
    if not isinstance(version, str) or not version:
        raise RuntimeError("Shared Toolbox version creation returned no version")
    names = _shared_toolbox_tool_names(
        credential,
        project_endpoint=project_endpoint,
        version=version,
    )
    missing = expected_shared_tool_names() - names
    if missing:
        raise RuntimeError(
            f"Shared Toolbox version {version} is missing pinned tools: {sorted(missing)}"
        )
    print(f"Shared Toolbox version {version} advertises {len(names)} tool(s).")
    _toolbox_json_request(
        credential,
        method="PATCH",
        url=f"{base_url}?api-version=v1",
        payload={"default_version": version},
    )
    return f"{base_url}/mcp?api-version=v1"


def configure_provider_apis(
    credential: TokenCredential | None = None,
    *,
    connector_targets: dict[str, str] | None = None,
) -> str:
    """Import provider specs into APIM, expose them as MCP, and share one Toolbox."""
    entries = provider_manifest()
    effective_credential = credential or DefaultAzureCredential()
    gateway_url = required_env("AZURE_API_MANAGEMENT_GATEWAY_URL")
    project_id = required_env("AZURE_AI_PROJECT_ID")
    project_endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT")
    governed_targets = connector_targets or connector_mcp_targets(
        required_env("AZURE_CONNECTOR_MCP_URLS")
    )

    onboard_provider_apis(
        effective_credential,
        subscription_id=required_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=required_env("AZURE_RESOURCE_GROUP"),
        service_name=required_env("AZURE_API_MANAGEMENT_NAME"),
        entries=entries,
    )

    for entry in entries:
        connection_url = (
            f"{project_id}/connections/{provider_connection_id(entry['connectorId'])}"
            "?api-version=2025-04-01-preview"
        )
        subprocess.run(
            [
                AZ_CLI,
                "rest",
                "--method",
                "put",
                "--url",
                connection_url,
                "--body",
                json.dumps(
                    provider_connection_payload(
                        target=mcp_endpoint(gateway_url, entry["mcpPath"]),
                    ),
                    separators=(",", ":"),
                ),
                "--output",
                "none",
            ],
            check=True,
        )

    endpoint = with_toolbox_project_retry(
        SHARED_TOOLBOX_NAME,
        lambda: _reconcile_shared_toolbox(
            effective_credential,
            project_endpoint=project_endpoint,
            gateway_url=gateway_url,
            entries=entries,
            connector_targets=governed_targets,
            guardrail_id=optional_env("AZURE_AGENTIC_GUARDRAIL_ID"),
        ),
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
    subprocess.run([AZD_CLI, "env", "set", "MEMORY_STORE_NAME", MEMORY_STORE_NAME], check=True)
    print(f"Memory store {MEMORY_STORE_NAME} is configured.")
    return MEMORY_STORE_NAME


LOCAL_ENV_BEGIN = "# >>> azd postprovision (managed) >>>"
LOCAL_ENV_END = "# <<< azd postprovision (managed) <<<"
#: Endpoints a local API process needs to read back what this deployment
#: created. Deliberately excludes stores whose absence keeps local runs on the
#: offline sandbox the README promises.
LOCAL_ENV_KEYS = ("FOUNDRY_PROJECT_ENDPOINT",)


def write_local_env() -> None:
    """Refresh the managed block of the repo-root ``.env`` from azd outputs.

    Without this, a developer running the API locally after ``azd up`` sees an
    empty Agent Studio and no way to discover why: the values exist only in
    ``.azure/<env>/.env``, which the app never reads. Rewriting the block on
    every provision also repoints a checkout that still references a torn-down
    environment. Lines outside the markers are preserved.
    """
    managed = [f'{key}="{os.environ[key]}"' for key in LOCAL_ENV_KEYS if os.environ.get(key)]
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


def main() -> None:
    sync_canonical_azd_outputs()
    write_local_env()
    credential = DefaultAzureCredential()
    endpoint = required_env("AZURE_SEARCH_ENDPOINT")
    index_name = required_env("AZURE_SEARCH_INDEX_NAME")
    documents = load_documents(
        tenant_id=required_env("AZURE_TENANT_ID"),
        project_id=required_env("AZURE_AI_PROJECT_NAME"),
    )

    create_index(endpoint, index_name, credential)
    embed_documents(documents, credential)
    upload_search_documents(endpoint, index_name, documents, credential)
    upload_source_artifacts(credential)
    configure_connector_mcp_tools(credential)
    connector_targets = configure_connector_connections(credential)
    configure_provider_apis(credential, connector_targets=connector_targets)
    configure_agent_memory(credential)
    wait_for_acr_pull_roles()
    print(f"Provisioned {len(documents)} evidence records into {index_name}.")


if __name__ == "__main__":
    main()
