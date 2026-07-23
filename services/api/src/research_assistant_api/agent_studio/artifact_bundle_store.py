"""Content-addressed, immutable release bundle storage.

Release bundles (source/build artifacts for a cut ``AgentVersion``, and the
pre-version source snapshots a Builder proposal captures) are stored by
their sha256 checksum as the blob name, so a bundle can never be silently
overwritten: uploading the same content is idempotent, and uploading
different content always gets a different address. This mirrors
``blob_sources.py`` but is intentionally a separate store (governed object
storage for *release* bundles, distinct from source documents).

Per Phase 2 tenant+project partitioning, every blob path/key is scoped to
``{tenant_id}/{project_id}/{logical_agent_id}/{version_label}/{checksum}``.
``version_label`` has no default and no "unversioned" sentinel: callers must
always pass an explicit, non-empty label so a read/write is always
traceable to an exact draft revision or an exact immutable cut version, and
so a future release-path caller can never *silently* fall back to a shared
generic bucket by omission. Two label shapes are expected:

- Draft (pre-cut-version) source snapshots use ``draft_version_label``,
  which is scoped to the draft's current ``etag`` -- distinct draft
  revisions of the same agent never collide.
- Cut-version/release bundles use the exact immutable ``AgentVersion.id``
  (or an equivalent content hash) directly as the label.

so a read is always authorized against an explicit ``ScopeContext`` rather
than a bare tenant id, and no path can collide across projects even for
agents that happen to share a logical agent id across tenants/projects.

Per the cloud-unavailable-paths requirement, when no storage endpoint is
configured this store is explicitly unavailable in non-test code paths
(``UnavailableArtifactBundleStore``); ``InMemoryArtifactBundleStore`` must
only ever be used by tests.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings


class ArtifactBundleStoreError(RuntimeError):
    pass


def draft_version_label(etag: str) -> str:
    """Path segment for a not-yet-cut draft's pre-version bundle uploads.

    Scoped to the draft's current ``etag`` (rather than a fixed literal)
    so source snapshots captured for different Builder proposals against
    the same agent's draft never share a bucket, and the label always
    traces back to the exact draft revision a proposal was generated
    against. ``logical_agent_id`` is already a separate path segment in
    ``_blob_path``, so this only needs etag-uniqueness within that agent's
    draft lineage.
    """
    if not etag or not etag.strip():
        raise ArtifactBundleStoreError(
            "A draft bundle version label requires a non-empty draft etag; there is no default 'unversioned' fallback."
        )
    return f"draft:{etag}"


@dataclass(frozen=True, slots=True)
class StoredBundle:
    uri: str
    checksum: str
    size_bytes: int


def _blob_path(tenant_id: str, project_id: str, logical_agent_id: str, checksum: str, version_label: str) -> str:
    if not version_label or not version_label.strip():
        raise ArtifactBundleStoreError(
            "version_label must be a non-empty, explicit draft or release identifier; "
            "there is no default 'unversioned' bucket for released artifacts."
        )
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
        version_label: str,
    ) -> StoredBundle: ...

    def get(
        self,
        *,
        scope: ScopeContext,
        logical_agent_id: str,
        checksum: str,
        version_label: str,
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
        version_label: str,
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
        version_label: str,
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
        version_label: str,
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
        version_label: str,
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
        version_label: str,
    ) -> StoredBundle:
        checksum = sha256(content).hexdigest()
        blob_name = _blob_path(tenant_id, project_id, logical_agent_id, checksum, version_label)
        blob = self._container.get_blob_client(blob_name)
        # No ``exists()`` pre-check: an exists-then-upload sequence is a
        # classic TOCTOU race. Two concurrent uploads of identical content
        # can both observe "absent" and both attempt
        # ``upload_blob(overwrite=False)``; the loser must not surface that
        # as a failure. Instead, attempt the conditional create directly
        # (atomic server-side) and treat ``ResourceExistsError`` as success:
        # the blob name is content-addressed (``.../{checksum}``), so a
        # blob already present at this exact path is guaranteed
        # byte-identical to ``content`` -- there is no real conflict to
        # surface, only a redundant, idempotent write that lost the race.
        with contextlib.suppress(ResourceExistsError):
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
        version_label: str,
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
