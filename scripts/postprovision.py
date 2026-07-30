from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

from scripts.azd_env import sync_canonical_azd_outputs

ROOT = Path(__file__).resolve().parents[1]
RETRY_DELAYS = (0, 30, 60, 90, 120)
TOOLBOX_PROJECT_RETRY_DELAYS = (0, 15, 30, 60, 90, 120, 180, 240, 300)
AZ_CLI = "az.cmd" if os.name == "nt" else "az"
AZD_CLI = "azd.exe" if os.name == "nt" else "azd"


class ToolboxProjectUnavailable(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required azd environment value is missing: {name}")
    return value


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
        required_env("SERVICE_WORKER_NAME"),
        required_env("SERVICE_CONNECTOR_ADAPTER_NAME"),
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
    audience: str,
) -> dict[str, Any]:
    return {
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": target,
            "audience": audience.rstrip("/"),
            "metadata": {
                "type": "generic_mcp",
                "audience": audience.rstrip("/"),
            },
        }
    }


def _toolbox_command_result(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _toolbox_error_text(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        value.strip()
        for value in (
            getattr(result, "stdout", ""),
            getattr(result, "stderr", ""),
        )
        if isinstance(value, str) and value.strip()
    )


def _raise_for_toolbox_failure(
    result: subprocess.CompletedProcess[str],
    *,
    toolbox_name: str,
) -> None:
    details = _toolbox_error_text(result)
    if "project not found" in details.lower():
        raise ToolboxProjectUnavailable(
            f"Foundry project is not ready for Toolbox {toolbox_name}"
        )
    raise RuntimeError(
        f"Toolbox {toolbox_name} command failed: {details or 'no diagnostic output'}"
    )


def _toolbox_endpoint(
    *,
    toolbox_name: str,
    definition: Path,
    project_endpoint: str,
) -> str:
    show_command = [
        AZD_CLI,
        "ai",
        "toolbox",
        "show",
        toolbox_name,
        "--project-endpoint",
        project_endpoint,
        "--output",
        "json",
    ]
    shown = _toolbox_command_result(show_command)
    if shown.returncode != 0:
        if "project not found" in _toolbox_error_text(shown).lower():
            _raise_for_toolbox_failure(shown, toolbox_name=toolbox_name)
        created = _toolbox_command_result(
            [
                AZD_CLI,
                "ai",
                "toolbox",
                "create",
                toolbox_name,
                "--from-file",
                str(definition),
                "--project-endpoint",
                project_endpoint,
            ]
        )
        if created.returncode != 0:
            _raise_for_toolbox_failure(created, toolbox_name=toolbox_name)
        shown = _toolbox_command_result(show_command)
        if shown.returncode != 0:
            _raise_for_toolbox_failure(shown, toolbox_name=toolbox_name)
    payload = json.loads(shown.stdout)
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise RuntimeError(f"Toolbox {toolbox_name} did not return an HTTPS endpoint")
    return endpoint


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


def probe_toolbox_project_readiness(project_endpoint: str) -> None:
    """Start data-plane discovery while independent provisioning work runs."""
    result = _toolbox_command_result(
        [
            AZD_CLI,
            "ai",
            "toolbox",
            "list",
            "--project-endpoint",
            project_endpoint,
            "--output",
            "json",
        ]
    )
    if result.returncode == 0:
        print("Foundry project Toolbox endpoint is ready.")
        return
    if "project not found" in _toolbox_error_text(result).lower():
        print(
            "Foundry project Toolbox endpoint is still propagating; "
            "continuing independent provisioning work before bounded retries."
        )
        return
    _raise_for_toolbox_failure(result, toolbox_name="project-readiness")


def configure_connector_toolboxes() -> dict[str, str]:
    project_id = required_env("AZURE_AI_PROJECT_ID")
    project_endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT")
    mcp_url = required_env("AZURE_CONNECTOR_MCP_URL")
    cloud = subprocess.run(
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
    if not cloud:
        raise RuntimeError("The active Azure cloud has no ARM audience endpoint")
    connection_url = (
        f"{project_id}/connections/research-connectors-apim"
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
                connector_connection_payload(
                    target=mcp_url,
                    audience=cloud,
                ),
                separators=(",", ":"),
            ),
            "--output",
            "none",
        ],
        check=True,
    )

    definitions = {
        "research-literature": (
            ROOT / "infra" / "toolboxes" / "literature-toolbox.yaml",
            "TOOLBOX_LITERATURE_MCP_ENDPOINT",
        ),
        "research-grant": (
            ROOT / "infra" / "toolboxes" / "grant-toolbox.yaml",
            "TOOLBOX_GRANT_MCP_ENDPOINT",
        ),
        "research-matching": (
            ROOT / "infra" / "toolboxes" / "matching-toolbox.yaml",
            "TOOLBOX_MATCHING_MCP_ENDPOINT",
        ),
        "research-dataset": (
            ROOT / "infra" / "toolboxes" / "dataset-toolbox.yaml",
            "TOOLBOX_DATASET_MCP_ENDPOINT",
        ),
    }
    endpoints: dict[str, str] = {}
    for toolbox_name, (definition, environment_name) in definitions.items():
        endpoint = with_toolbox_project_retry(
            toolbox_name,
            lambda toolbox_name=toolbox_name, definition=definition: _toolbox_endpoint(
                toolbox_name=toolbox_name,
                definition=definition,
                project_endpoint=project_endpoint,
            ),
        )
        subprocess.run(
            [AZD_CLI, "env", "set", environment_name, endpoint],
            check=True,
        )
        endpoints[toolbox_name] = endpoint
    return endpoints


def configure_connector_adapter_identity() -> None:
    subprocess.run(
        [
            AZ_CLI,
            "containerapp",
            "update",
            "--resource-group",
            required_env("AZURE_RESOURCE_GROUP"),
            "--name",
            required_env("SERVICE_CONNECTOR_ADAPTER_NAME"),
            "--set-env-vars",
            (
                "RESEARCH_APIM_PRINCIPAL_ID="
                f"{required_env('AZURE_API_MANAGEMENT_PRINCIPAL_ID')}"
            ),
            (
                "RESEARCH_WORKSPACE_TENANT_ID="
                f"{required_env('AZURE_TENANT_ID')}"
            ),
            "--output",
            "none",
        ],
        check=True,
    )


def main() -> None:
    sync_canonical_azd_outputs()
    probe_toolbox_project_readiness(required_env("FOUNDRY_PROJECT_ENDPOINT"))
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
    configure_connector_adapter_identity()
    configure_connector_toolboxes()
    wait_for_acr_pull_roles()
    print(f"Provisioned {len(documents)} evidence records into {index_name}.")


if __name__ == "__main__":
    main()
