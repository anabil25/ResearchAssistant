from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import DocumentContentFormat
from azure.core.credentials import TokenCredential
from azure.core.exceptions import HttpResponseError
from azure.cosmos import CosmosClient
from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.search.documents import SearchClient
from azure.storage.blob import BlobClient, BlobServiceClient, ContentSettings
from openai import AzureOpenAI
from research_assistant_core.chunking import chunk_text
from research_assistant_core.security import scan_untrusted_content

_MAX_SOURCE_BYTES = 20_000_000
_MAX_MANIFEST_BYTES = 25_000_000
_EMBEDDING_BATCH_SIZE = 128
_SEARCH_BATCH_DOCUMENTS = 500
_SEARCH_BATCH_BYTES = 14_000_000


class BlobDownload(Protocol):
    def chunks(self) -> Iterable[bytes]: ...


class DownloadableBlob(Protocol):
    def download_blob(self, **kwargs: Any) -> BlobDownload: ...


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    storage_endpoint: str
    source_container: str
    document_intelligence_endpoint: str
    search_endpoint: str
    search_index: str
    openai_endpoint: str
    cosmos_endpoint: str
    cosmos_database: str
    embedding_deployment: str
    managed_identity_client_id: str | None


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Runtime ingestion requires {name}")
    return value


def ingestion_settings() -> IngestionSettings:
    return IngestionSettings(
        storage_endpoint=_required("AZURE_STORAGE_BLOB_ENDPOINT"),
        source_container=os.getenv("AZURE_STORAGE_SOURCE_CONTAINER", "sources"),
        document_intelligence_endpoint=_required("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
        search_endpoint=_required("AZURE_SEARCH_ENDPOINT"),
        search_index=_required("AZURE_SEARCH_INDEX_NAME"),
        openai_endpoint=_required("AZURE_OPENAI_ENDPOINT"),
        cosmos_endpoint=_required("AZURE_COSMOS_ENDPOINT"),
        cosmos_database=os.getenv("AZURE_COSMOS_DATABASE", "research"),
        embedding_deployment=os.getenv(
            "AZURE_AI_EMBEDDING_DEPLOYMENT_NAME",
            "text-embedding-3-large",
        ),
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID"),
    )


@lru_cache(maxsize=1)
def credential() -> TokenCredential:
    client_id = os.getenv("AZURE_CLIENT_ID")
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def _update_run(
    payload: dict[str, Any],
    *,
    status: str,
    progress: int,
    current_stage: str,
    completed: bool = False,
) -> None:
    endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
    if not endpoint:
        return
    database_name = os.getenv("AZURE_COSMOS_DATABASE", "research")
    client = CosmosClient(endpoint, credential=credential())
    container = client.get_database_client(database_name).get_container_client("runs")
    run_id = str(payload["run_id"])
    partition = f"{payload.get('tenant_id', 'demo')}|{run_id}"
    document = container.read_item(item=run_id, partition_key=partition)
    run = document["payload"]
    run["status"] = status
    run["progress"] = progress
    run["current_stage"] = current_stage
    stages = run.get("stages")
    if isinstance(stages, list):
        if completed and status == "completed":
            completed_at = datetime.now(UTC).isoformat()
            for stage in stages:
                if isinstance(stage, dict):
                    stage["status"] = "completed"
                    stage["started_at"] = stage.get("started_at") or run.get("started_at")
                    stage["completed_at"] = stage.get("completed_at") or completed_at
        else:
            current_index = next(
                (
                    index
                    for index, stage in enumerate(stages)
                    if isinstance(stage, dict) and stage.get("label") == current_stage
                ),
                None,
            )
            if current_index is not None:
                for index, stage in enumerate(stages):
                    if not isinstance(stage, dict):
                        continue
                    if index < current_index:
                        stage["status"] = "completed"
                    elif index == current_index:
                        stage["status"] = (
                            "waiting_for_approval"
                            if status == "waiting_for_approval"
                            else "failed"
                            if status in {"failed", "blocked"}
                            else "running"
                        )
                    else:
                        stage["status"] = "planned"
            elif completed and status in {"failed", "blocked"}:
                active = next(
                    (
                        stage
                        for stage in stages
                        if isinstance(stage, dict)
                        and stage.get("status")
                        in {"running", "waiting_for_approval", "planned"}
                    ),
                    None,
                )
                if active is not None:
                    active["status"] = "failed"
    if completed:
        run["completed_at"] = datetime.now(UTC).isoformat()
    container.upsert_item(document)


def _update_library(
    payload: dict[str, Any],
    *,
    status: str,
    evidence_count: int,
) -> None:
    endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
    if not endpoint:
        return
    database_name = os.getenv("AZURE_COSMOS_DATABASE", "research")
    client = CosmosClient(endpoint, credential=credential())
    container = client.get_database_client(database_name).get_container_client("sources")
    source_id = str(payload["source_id"])
    partition = f"{payload.get('tenant_id', 'demo')}|{payload.get('project_id', 'demo-project')}"
    document = container.read_item(item=source_id, partition_key=partition)
    item = document["payload"]
    item["status"] = status
    item["evidence_count"] = evidence_count
    item["version"] = "1.0"
    container.upsert_item(document)


def _extract_pages(
    content: bytes,
    content_type: str,
    settings: IngestionSettings,
) -> list[tuple[int, str]]:
    if content_type in {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    }:
        return [(1, content.decode("utf-8-sig"))]

    client = DocumentIntelligenceClient(
        endpoint=settings.document_intelligence_endpoint,
        credential=credential(),
    )
    result = client.begin_analyze_document(
        "prebuilt-layout",
        BytesIO(content),
        output_content_format=DocumentContentFormat.MARKDOWN,
    ).result()
    extracted = result.content or ""
    pages: list[tuple[int, str]] = []
    for page in result.pages or []:
        page_content = "".join(extracted[span.offset : span.offset + span.length] for span in page.spans or []).strip()
        if page_content:
            pages.append((page.page_number, page_content))
    return pages or [(1, extracted)]


def _blob_from_uri(uri: str, settings: IngestionSettings) -> BlobClient:
    configured = urlparse(settings.storage_endpoint)
    candidate = urlparse(uri)
    if candidate.scheme != "https" or candidate.netloc.casefold() != configured.netloc.casefold():
        raise ValueError("Blob reference is outside the configured storage account")
    path = unquote(candidate.path).lstrip("/")
    container, separator, blob_name = path.partition("/")
    if not separator or container != settings.source_container or not blob_name or ".." in blob_name.split("/"):
        raise ValueError("Blob reference is outside the configured source container")
    service = BlobServiceClient(
        account_url=settings.storage_endpoint,
        credential=credential(),
    )
    return service.get_blob_client(
        container=settings.source_container,
        blob=blob_name,
    )


def _download_bounded(blob: DownloadableBlob, max_bytes: int) -> bytes:
    content = bytearray()
    for block in blob.download_blob(max_concurrency=1).chunks():
        if len(content) + len(block) > max_bytes:
            raise ValueError(f"Blob exceeds the {max_bytes:,}-byte processing limit")
        content.extend(block)
    return bytes(content)


def extract_source(payload: dict[str, Any]) -> dict[str, Any]:
    _update_run(
        payload,
        status="running",
        progress=20,
        current_stage="Extract structure",
    )
    blob_uri = payload.get("blob_uri")
    if not blob_uri:
        return {
            "source_id": str(payload["source_id"]),
            "blob_uri": f"blob://sources/{payload['source_id']}",
            "status": "verified",
        }

    settings = ingestion_settings()
    source_blob = _blob_from_uri(str(blob_uri), settings)
    content = _download_bounded(source_blob, _MAX_SOURCE_BYTES)
    if not content:
        raise ValueError("Source content must be between 1 byte and 20 MB")
    checksum = f"sha256:{sha256(content).hexdigest()}"
    expected_checksum = payload.get("checksum")
    if expected_checksum and checksum != expected_checksum:
        raise ValueError("Source checksum does not match the upload record")

    pages = _extract_pages(
        content,
        str(payload.get("content_type") or "application/octet-stream"),
        settings,
    )
    manifest_chunks: list[dict[str, Any]] = []
    for page_number, page_content in pages:
        for chunk in chunk_text(page_content):
            manifest_chunks.append(
                {
                    "id": f"{payload['source_id']}-chunk-{len(manifest_chunks):04d}",
                    "page_start": page_number,
                    "section": f"Page {page_number}",
                    "content": chunk.content,
                }
            )
    if not manifest_chunks:
        raise ValueError("Document Intelligence did not extract any source text")

    manifest = {
        "source_id": payload["source_id"],
        "title": payload.get("title", payload["source_id"]),
        "kind": payload.get("kind", "Document"),
        "tenant_id": payload.get("tenant_id", "demo"),
        "project_id": payload.get("project_id", "demo-project"),
        "access": payload.get("access", "internal"),
        "group_ids": payload.get("group_ids", []),
        "license": payload.get("license", "Project supplied"),
        "provider": payload.get("provider", "Workspace upload"),
        "year": payload.get("year"),
        "version": "1.0",
        "checksum": checksum,
        "blob_uri": blob_uri,
        "chunks": manifest_chunks,
    }
    service = BlobServiceClient(
        account_url=settings.storage_endpoint,
        credential=credential(),
    )
    manifest_blob = service.get_blob_client(
        container=settings.source_container,
        blob=(f"{manifest['tenant_id']}/{manifest['project_id']}/{manifest['source_id']}/extracted-manifest.json"),
    )
    manifest_blob.upload_blob(
        json.dumps(manifest, ensure_ascii=True).encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )
    return {
        "source_id": str(payload["source_id"]),
        "blob_uri": str(blob_uri),
        "extracted_manifest_uri": manifest_blob.url,
        "chunk_count": len(manifest_chunks),
        "status": "extracted",
    }


def _source_kind(value: str) -> str:
    return {
        "paper": "paper",
        "policy": "policy",
        "funding notice": "grant",
        "dataset": "dataset",
        "person": "person",
        "facility": "facility",
        "template": "template",
    }.get(value.casefold(), "template")


def _search_batches(
    documents: list[dict[str, Any]],
) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    batch_bytes = 2
    for document in documents:
        document_bytes = len(json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        if document_bytes + 2 > _SEARCH_BATCH_BYTES:
            raise ValueError("A Search document exceeds the batch byte limit")
        if batch and (len(batch) >= _SEARCH_BATCH_DOCUMENTS or batch_bytes + document_bytes + 1 > _SEARCH_BATCH_BYTES):
            yield batch
            batch = []
            batch_bytes = 2
        batch.append(document)
        batch_bytes += document_bytes + 1
    if batch:
        yield batch


def index_extracted_source(payload: dict[str, Any]) -> dict[str, Any]:
    manifest_uri = payload.get("extracted_manifest_uri")
    if not manifest_uri:
        _update_run(
            payload,
            status="running",
            progress=50,
            current_stage="Retrieve authorized evidence",
        )
        if payload.get("workflow_kind") == "library_ingestion":
            _update_library(
                payload,
                status="needs_review",
                evidence_count=0,
            )
        return {
            "query": payload["query"],
            "evidence_manifest_uri": f"blob://evidence/{payload['run_id']}.json",
            "passage_count": (0 if payload.get("workflow_kind") == "library_ingestion" else 3),
        }

    _update_run(
        payload,
        status="running",
        progress=58,
        current_stage="Chunk, embed & index",
    )
    settings = ingestion_settings()
    manifest_blob = _blob_from_uri(str(manifest_uri), settings)
    manifest = json.loads(_download_bounded(manifest_blob, _MAX_MANIFEST_BYTES))
    chunks = manifest["chunks"]
    token_provider = get_bearer_token_provider(
        credential(),
        "https://cognitiveservices.azure.com/.default",
    )
    openai_client = AzureOpenAI(
        azure_endpoint=settings.openai_endpoint,
        azure_ad_token_provider=token_provider,
        api_version="2024-10-21",
    )
    search = SearchClient(
        endpoint=settings.search_endpoint,
        index_name=settings.search_index,
        credential=credential(),
    )
    indexed_count = 0
    staged_ids: list[str] = []
    try:
        for start in range(0, len(chunks), _EMBEDDING_BATCH_SIZE):
            chunk_batch = chunks[start : start + _EMBEDDING_BATCH_SIZE]
            embeddings = openai_client.embeddings.create(
                model=settings.embedding_deployment,
                input=[chunk["content"] for chunk in chunk_batch],
            )
            documents = [
                {
                    "id": chunk["id"],
                    "source_id": manifest["source_id"],
                    "source_kind": _source_kind(str(manifest["kind"])),
                    "tenant_ids": [manifest["tenant_id"]],
                    "project_ids": [manifest["project_id"]],
                    "group_ids": manifest["group_ids"],
                    "access": manifest["access"],
                    "year": manifest["year"],
                    "provider": manifest["provider"],
                    "ingestion_status": "staging",
                    "generation_id": str(payload["run_id"]),
                    "safety_status": ("quarantined" if scan_untrusted_content(str(chunk["content"])) else "safe"),
                    "title": manifest["title"],
                    "section": chunk["section"],
                    "page_start": chunk["page_start"],
                    "content": chunk["content"],
                    "checksum": manifest["checksum"],
                    "license": manifest["license"],
                    "version": manifest["version"],
                    "content_vector": embedding.embedding,
                }
                for chunk, embedding in zip(
                    chunk_batch,
                    embeddings.data,
                    strict=True,
                )
            ]
            for search_batch in _search_batches(documents):
                outcomes = search.upload_documents(documents=search_batch)
                failures = [outcome.key for outcome in outcomes if not outcome.succeeded]
                staged_ids.extend(str(outcome.key) for outcome in outcomes if outcome.succeeded)
                if failures:
                    raise RuntimeError(f"Search staging failed for chunks: {failures}")

        activation = [{"id": chunk_id, "ingestion_status": "ready"} for chunk_id in staged_ids]
        for activation_batch in _search_batches(activation):
            outcomes = search.merge_documents(documents=activation_batch)
            failures = [outcome.key for outcome in outcomes if not outcome.succeeded]
            if failures:
                raise RuntimeError(f"Search activation failed for chunks: {failures}")
        indexed_count = sum(1 for chunk in chunks if not scan_untrusted_content(str(chunk["content"])))
        quarantined_count = len(chunks) - indexed_count
        _update_library(
            payload,
            status="needs_review" if quarantined_count else "ready",
            evidence_count=indexed_count,
        )
        _update_run(
            payload,
            status="completed",
            progress=100,
            current_stage="Indexed and ready",
            completed=True,
        )
    except Exception:
        cleanup = [{"id": chunk_id} for chunk_id in staged_ids]
        try:
            for cleanup_batch in _search_batches(cleanup):
                search.delete_documents(documents=cleanup_batch)
        except HttpResponseError as cleanup_exc:
            raise RuntimeError("Search generation cleanup failed after ingestion failure") from cleanup_exc
        raise
    return {
        "query": payload["query"],
        "evidence_manifest_uri": str(manifest_uri),
        "passage_count": indexed_count,
    }


def set_run_state(
    payload: dict[str, Any],
    *,
    status: str,
    progress: int,
    current_stage: str,
    completed: bool = False,
) -> None:
    _update_run(
        payload,
        status=status,
        progress=progress,
        current_stage=current_stage,
        completed=completed,
    )


def set_library_state(
    payload: dict[str, Any],
    *,
    status: str,
    evidence_count: int = 0,
) -> None:
    _update_library(
        payload,
        status=status,
        evidence_count=evidence_count,
    )
