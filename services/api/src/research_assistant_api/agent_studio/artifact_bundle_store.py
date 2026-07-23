"""Content-addressed, immutable release bundle storage.

Release bundles (source/build artifacts for a cut ``AgentVersion``, and the
pre-version source snapshots a Builder proposal captures) are stored by
their sha256 checksum as the blob name, so a bundle can never be silently
overwritten: uploading the same content is idempotent, and uploading
different content always gets a different address. This mirrors
``blob_sources.py`` but is intentionally a separate store (governed object
storage for *release* bundles, distinct from source documents).

Per Phase 2 tenant+project partitioning, every blob path/key is scoped to
``{tenant_id}/{project_id}/{logical_agent_id}/{version_label}/{checksum}``
(``version_label`` is the cut ``AgentVersion.id`` once a version exists, or
the literal ``"unversioned"`` for a Builder proposal's pre-cut source
snapshot) so a read is always authorized against an explicit
``ScopeContext`` rather than a bare tenant id, and no path can collide
across projects even for agents that happen to share a logical agent id
across tenants/projects.

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
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings

#: Path segment used in place of a cut ``AgentVersion.id`` for bundles
#: uploaded before any version exists (e.g. a Builder proposal's source
#: snapshot). Never collides with a real version id, which is always a
#: generated identifier from ``allocate_version``/``uuid4`` and never this
#: literal string.
UNVERSIONED = "unversioned"


class ArtifactBundleStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredBundle:
    uri: str
    checksum: str
    size_bytes: int


def _blob_path(tenant_id: str, project_id: str, logical_agent_id: str, checksum: str, version_label: str) -> str:
    return f"{tenant_id}/{project_id}/{logical_agent_id}/{version_label}/{checksum}"


class ArtifactBundleStore(Protocol):
    def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
        version_label: str = UNVERSIONED,
    ) -> StoredBundle: ...

    def get(
        self,
        *,
        scope: ScopeContext,
        logical_agent_id: str,
        checksum: str,
        version_label: str = UNVERSIONED,
    ) -> bytes | None: ...


class InMemoryArtifactBundleStore:
    """Test-only, content-addressed in-process bundle store."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
        version_label: str = UNVERSIONED,
    ) -> StoredBundle:
        checksum = sha256(content).hexdigest()
        key = _blob_path(tenant_id, project_id, logical_agent_id, checksum, version_label)
        self.items.setdefault(key, content)
        return StoredBundle(uri=f"memory://{key}", checksum=f"sha256:{checksum}", size_bytes=len(content))

    def get(
        self,
        *,
        scope: ScopeContext,
        logical_agent_id: str,
        checksum: str,
        version_label: str = UNVERSIONED,
    ) -> bytes | None:
        key = _blob_path(scope.tenant_id, scope.project_id, logical_agent_id, checksum, version_label)
        return self.items.get(key)


class UnavailableArtifactBundleStore:
    """Explicit cloud-unavailable path: no blob storage endpoint configured."""

    def put(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
        version_label: str = UNVERSIONED,
    ) -> StoredBundle:
        raise ArtifactBundleStoreError(
            "No Azure Storage Blob endpoint is configured; release bundle storage is unavailable."
        )

    def get(
        self,
        *,
        scope: ScopeContext,
        logical_agent_id: str,
        checksum: str,
        version_label: str = UNVERSIONED,
    ) -> bytes | None:
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
        project_id: str,
        logical_agent_id: str,
        content: bytes,
        content_type: str = "application/zip",
        version_label: str = UNVERSIONED,
    ) -> StoredBundle:
        checksum = sha256(content).hexdigest()
        blob_name = _blob_path(tenant_id, project_id, logical_agent_id, checksum, version_label)
        blob = self._container.get_blob_client(blob_name)
        if not blob.exists():
            blob.upload_blob(
                content,
                overwrite=False,
                metadata={
                    "sha256": checksum,
                    "tenant": tenant_id,
                    "project": project_id,
                    "logicalAgentId": logical_agent_id,
                    "versionLabel": version_label,
                },
                content_settings=ContentSettings(content_type=content_type),
            )
        return StoredBundle(uri=blob.url, checksum=f"sha256:{checksum}", size_bytes=len(content))

    def get(
        self,
        *,
        scope: ScopeContext,
        logical_agent_id: str,
        checksum: str,
        version_label: str = UNVERSIONED,
    ) -> bytes | None:
        blob_name = _blob_path(scope.tenant_id, scope.project_id, logical_agent_id, checksum, version_label)
        blob = self._container.get_blob_client(blob_name)
        try:
            downloaded = blob.download_blob()
        except ResourceNotFoundError:
            return None
        content: bytes = downloaded.readall()
        return content


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
