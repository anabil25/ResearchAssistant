from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePath
from typing import Protocol

from azure.core.credentials import TokenCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.config import Settings


@dataclass(frozen=True, slots=True)
class StoredSource:
    uri: str
    checksum: str
    size_bytes: int
    content_type: str


class SourceBlobStore(Protocol):
    def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> StoredSource: ...


class AzureSourceBlobStore:
    def __init__(
        self,
        endpoint: str,
        container_name: str,
        credential: TokenCredential,
    ) -> None:
        client = BlobServiceClient(account_url=endpoint, credential=credential)
        self._container = client.get_container_client(container_name)

    def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_id: str,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> StoredSource:
        checksum = sha256(content).hexdigest()
        blob_name = f"{tenant_id}/{project_id}/{source_id}/{_safe_filename(filename)}"
        blob = self._container.get_blob_client(blob_name)
        blob.upload_blob(
            content,
            overwrite=False,
            metadata={
                "sha256": checksum,
                "tenant": tenant_id,
                "project": project_id,
                "source": source_id,
            },
            content_settings=ContentSettings(content_type=content_type),
        )
        return StoredSource(
            uri=blob.url,
            checksum=f"sha256:{checksum}",
            size_bytes=len(content),
            content_type=content_type,
        )


def _safe_filename(value: str) -> str:
    basename = PurePath(value).name
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip(".-")
    return sanitized[:120] or "source.bin"


def build_source_blob_store(settings: Settings) -> SourceBlobStore:
    return AzureSourceBlobStore(
        settings.storage_blob_endpoint,
        settings.storage_source_container,
        azure_credential(settings.managed_identity_client_id),
    )
