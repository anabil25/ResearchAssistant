"""Content-addressed, immutable release bundle storage.

Release bundles (source/build artifacts for a cut ``AgentVersion``) are
stored by their sha256 checksum as the blob name, so a bundle can never be
silently overwritten: uploading the same content is idempotent, and
uploading different content always gets a different address. This mirrors
``blob_sources.py`` but is intentionally a separate store (governed object
storage for *release* bundles, distinct from source documents).

Per the cloud-unavailable-paths requirement, when no storage endpoint is
configured this store is explicitly unavailable in non-test code paths
(``UnavailableArtifactBundleStore``); ``InMemoryArtifactBundleStore`` must
only ever be used by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from research_assistant_api.config import Settings


class ArtifactBundleStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredBundle:
    uri: str
    checksum: str
    size_bytes: int


class ArtifactBundleStore(Protocol):
    def put(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
    ) -> StoredBundle: ...


class InMemoryArtifactBundleStore:
    """Test-only, content-addressed in-process bundle store."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def put(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
    ) -> StoredBundle:
        checksum = sha256(content).hexdigest()
        key = f"{tenant_id}/{logical_agent_id}/{checksum}"
        self.items.setdefault(key, content)
        return StoredBundle(uri=f"memory://{key}", checksum=f"sha256:{checksum}", size_bytes=len(content))


class UnavailableArtifactBundleStore:
    """Explicit cloud-unavailable path: no blob storage endpoint configured."""

    def put(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
    ) -> StoredBundle:
        raise ArtifactBundleStoreError(
            "No Azure Storage Blob endpoint is configured; release bundle storage is unavailable."
        )


class AzureArtifactBundleStore:
    def __init__(self, endpoint: str, container_name: str, credential: TokenCredential) -> None:
        client = BlobServiceClient(account_url=endpoint, credential=credential)
        self._container = client.get_container_client(container_name)

    def put(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
    ) -> StoredBundle:
        checksum = sha256(content).hexdigest()
        blob_name = f"{tenant_id}/{logical_agent_id}/{checksum}"
        blob = self._container.get_blob_client(blob_name)
        if not blob.exists():
            blob.upload_blob(
                content,
                overwrite=False,
                metadata={"sha256": checksum, "tenant": tenant_id, "logicalAgentId": logical_agent_id},
                content_settings=ContentSettings(content_type=content_type),
            )
        return StoredBundle(uri=blob.url, checksum=f"sha256:{checksum}", size_bytes=len(content))


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_artifact_bundle_store(settings: Settings) -> ArtifactBundleStore:
    """Production factory: never returns an in-memory store.

    Returns ``UnavailableArtifactBundleStore`` (explicit failure on use) when
    no storage endpoint is configured, rather than silently degrading to
    in-memory persistence.
    """
    if not settings.storage_blob_endpoint:
        return UnavailableArtifactBundleStore()
    return AzureArtifactBundleStore(
        settings.storage_blob_endpoint,
        settings.agent_studio_bundle_container,
        _credential(settings.managed_identity_client_id),
    )
