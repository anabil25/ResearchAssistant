from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.artifact_bundle_store as artifact_bundle_store
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from research_assistant_api.agent_studio.artifact_bundle_store import (
    ArtifactBundleStoreError,
    AzureArtifactBundleStore,
    InMemoryArtifactBundleStore,
    UnavailableArtifactBundleStore,
    build_artifact_bundle_store,
    draft_version_label,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


def test_in_memory_store_is_content_addressed_and_idempotent() -> None:
    store = InMemoryArtifactBundleStore()
    first = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"bundle-bytes",
        version_label="version-1",
    )
    second = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"bundle-bytes",
        version_label="version-1",
    )
    assert first == second
    assert first.checksum.startswith("sha256:")
    assert first.size_bytes == len(b"bundle-bytes")
    assert len(store.items) == 1


def test_in_memory_store_distinguishes_different_content() -> None:
    store = InMemoryArtifactBundleStore()
    first = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"content-a",
        version_label="version-1",
    )
    second = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"content-b",
        version_label="version-1",
    )
    assert first.checksum != second.checksum
    assert len(store.items) == 2


def test_in_memory_store_scopes_path_by_project_and_version_label() -> None:
    store = InMemoryArtifactBundleStore()
    proj_a = store.put(
        tenant_id="demo",
        project_id="proj-a",
        logical_agent_id="agent-1",
        content=b"same-bytes",
        version_label="version-123",
    )
    proj_b = store.put(
        tenant_id="demo",
        project_id="proj-b",
        logical_agent_id="agent-1",
        content=b"same-bytes",
        version_label="version-123",
    )
    # Same tenant/agent/content but different project: distinct blob keys (no cross-project collision).
    assert proj_a.uri != proj_b.uri
    assert len(store.items) == 2

    other_version = store.put(
        tenant_id="demo",
        project_id="proj-a",
        logical_agent_id="agent-1",
        content=b"same-bytes",
        version_label="version-456",
    )
    assert other_version.uri != proj_a.uri
    assert len(store.items) == 3


def test_in_memory_store_get_returns_content_within_scope() -> None:
    store = InMemoryArtifactBundleStore()
    stored = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-1",
    )
    checksum = stored.checksum.removeprefix("sha256:")
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert (
        store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum, version_label="version-1") == b"payload"
    )


def test_in_memory_store_get_returns_none_for_wrong_scope_or_missing_content() -> None:
    store = InMemoryArtifactBundleStore()
    stored = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-1",
    )
    checksum = stored.checksum.removeprefix("sha256:")

    other_tenant = ScopeContext(tenant_id="other-tenant", project_id="proj-1")
    result = store.get(scope=other_tenant, logical_agent_id="agent-1", checksum=checksum, version_label="version-1")
    assert result is None

    other_project = ScopeContext(tenant_id="demo", project_id="other-project")
    result = store.get(scope=other_project, logical_agent_id="agent-1", checksum=checksum, version_label="version-1")
    assert result is None

    same_scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    result = store.get(scope=same_scope, logical_agent_id="agent-1", checksum="0" * 64, version_label="version-1")
    assert result is None


def test_in_memory_store_get_fails_closed_when_stored_content_does_not_match_checksum() -> None:
    """The content-addressed path already embeds the checksum, so this is
    unreachable via the store's own ``put``/``get`` API -- but a test
    fixture (or, in production, storage corruption/an out-of-band writer)
    poking the backing storage directly must not be silently trusted back
    out as verified content."""
    store = InMemoryArtifactBundleStore()
    stored = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-1",
    )
    checksum = stored.checksum.removeprefix("sha256:")
    key = f"demo/proj-1/agent-1/version-1/{checksum}"
    store.items[key] = b"tampered-content"

    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    with pytest.raises(ArtifactBundleStoreError, match="does not match"):
        store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum, version_label="version-1")


@pytest.mark.parametrize("bad_label", ["", "   "])
def test_in_memory_store_fails_closed_on_blank_version_label(bad_label: str) -> None:
    store = InMemoryArtifactBundleStore()
    with pytest.raises(ArtifactBundleStoreError, match="non-empty"):
        store.put(
            tenant_id="demo",
            project_id="proj-1",
            logical_agent_id="agent-1",
            content=b"payload",
            version_label=bad_label,
        )
    with pytest.raises(ArtifactBundleStoreError, match="non-empty"):
        store.get(
            scope=ScopeContext(tenant_id="demo", project_id="proj-1"),
            logical_agent_id="agent-1",
            checksum="0" * 64,
            version_label=bad_label,
        )


@pytest.mark.parametrize("bad_etag", ["", "   "])
def test_draft_version_label_fails_closed_on_blank_etag(bad_etag: str) -> None:
    with pytest.raises(ArtifactBundleStoreError, match="non-empty"):
        draft_version_label(bad_etag)


def test_draft_version_label_is_distinct_per_etag() -> None:
    first = draft_version_label("etag-1")
    second = draft_version_label("etag-2")
    assert first != second
    assert first == draft_version_label("etag-1")


def test_unavailable_store_raises_on_put_and_get() -> None:
    store = UnavailableArtifactBundleStore()
    with pytest.raises(ArtifactBundleStoreError, match="unavailable"):
        store.put(
            tenant_id="demo",
            project_id="proj-1",
            logical_agent_id="agent-1",
            content=b"bundle-bytes",
            version_label="version-1",
        )
    with pytest.raises(ArtifactBundleStoreError, match="unavailable"):
        store.get(
            scope=ScopeContext(tenant_id="demo", project_id="proj-1"),
            logical_agent_id="agent-1",
            checksum="0" * 64,
            version_label="version-1",
        )


class FakeBlobClient:
    def __init__(self, registry: dict[str, dict[str, Any]], name: str) -> None:
        self._registry = registry
        self._name = name
        self.url = f"https://fake.blob.core.windows.net/container/{name}"

    def upload_blob(self, content: bytes, *, overwrite: bool, metadata: dict[str, str], content_settings: Any) -> None:
        assert overwrite is False
        # Mirrors real Azure Blob Storage's conditional-create semantics for
        # ``overwrite=False``: a blob already present at this exact name is
        # rejected with ``ResourceExistsError`` rather than silently
        # overwritten (or silently skipped) here.
        if self._name in self._registry:
            raise ResourceExistsError("blob already exists")
        self._registry[self._name] = {"content": content, "metadata": metadata}

    def download_blob(self) -> Any:
        if self._name not in self._registry:
            raise ResourceNotFoundError("blob not found")
        content = self._registry[self._name]["content"]

        class _Downloaded:
            def readall(self) -> bytes:
                return content

        return _Downloaded()

    def get_blob_properties(self) -> Any:
        if self._name not in self._registry:
            raise ResourceNotFoundError("blob not found")
        entry = self._registry[self._name]

        class _Properties:
            metadata = entry["metadata"]
            size = len(entry["content"])

        return _Properties()


class FakeContainerClient:
    def __init__(self) -> None:
        self.registry: dict[str, dict[str, Any]] = {}

    def get_blob_client(self, blob_name: str) -> FakeBlobClient:
        return FakeBlobClient(self.registry, blob_name)


class FakeBlobServiceClient:
    def __init__(self, *, account_url: str, credential: Any) -> None:
        self.account_url = account_url
        self.credential = credential
        self.container = FakeContainerClient()

    def get_container_client(self, _container_name: str) -> FakeContainerClient:
        return self.container


def test_azure_store_uploads_new_blob_and_skips_reupload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    bundle = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-abc",
    )
    assert bundle.uri.startswith("https://fake.blob.core.windows.net/container/")
    assert "demo/proj-1/agent-1/version-abc/" in bundle.uri
    assert bundle.checksum.startswith("sha256:")
    fake_container = cast(FakeContainerClient, store._container)
    assert len(fake_container.registry) == 1

    # Uploading the same content again is idempotent: the conditional-create
    # attempt is rejected as an already-existing blob, but ``put`` still
    # returns success with the same identity rather than surfacing that as
    # an error.
    same = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-abc",
    )
    assert same.uri == bundle.uri
    assert len(fake_container.registry) == 1


def test_azure_store_put_treats_concurrent_duplicate_upload_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding (independent review of ec4d8f1): exists-then-upload TOCTOU.

    Two concurrent uploads of identical content can both observe the blob
    as absent and both attempt ``upload_blob(overwrite=False)``; the
    "loser" of that race must not surface ``ResourceExistsError`` as a
    failure -- the blob name is content-addressed, so an existing blob at
    this exact path is guaranteed byte-identical, making this a successful
    idempotent no-op rather than a real conflict. This test never calls
    ``exists()`` at all, so it fails if a future regression reintroduces an
    exists-then-upload race window rather than a single atomic
    conditional-create attempt.
    """
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    fake_container = cast(FakeContainerClient, store._container)
    # Simulate a concurrent winner: pre-populate the registry with the
    # exact content (and matching sha256 metadata) this call is about to
    # upload, at the exact path this call will compute, *before* this call
    # ever checks anything.
    checksum = artifact_bundle_store.sha256(b"racy-payload").hexdigest()
    blob_name = f"demo/proj-1/agent-1/version-race/{checksum}"
    fake_container.registry[blob_name] = {"content": b"racy-payload", "metadata": {"sha256": checksum}}

    bundle = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"racy-payload",
        version_label="version-race",
    )

    assert bundle.checksum == f"sha256:{checksum}"
    assert fake_container.registry[blob_name]["content"] == b"racy-payload"


def test_azure_store_put_fails_closed_when_existing_blob_content_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A content-addressed path collision without matching content should
    be unreachable in correct operation, but the store must not blindly
    trust the path and silently report success: if the blob already
    present at this exact checksum-addressed path carries different
    metadata (corruption, a hash-stripping proxy, or a bug elsewhere), the
    conflict must fail closed instead of being treated as an idempotent
    duplicate."""
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    fake_container = cast(FakeContainerClient, store._container)
    checksum = artifact_bundle_store.sha256(b"racy-payload").hexdigest()
    blob_name = f"demo/proj-1/agent-1/version-race/{checksum}"
    # Pre-populate a blob at the exact content-addressed path, but with
    # metadata that does not match the checksum this call will compute
    # (simulating corrupted/mismatched existing content).
    fake_container.registry[blob_name] = {"content": b"different-bytes!", "metadata": {"sha256": "0" * 64}}

    with pytest.raises(ArtifactBundleStoreError, match="does not match"):
        store.put(
            tenant_id="demo",
            project_id="proj-1",
            logical_agent_id="agent-1",
            content=b"racy-payload",
            version_label="version-race",
        )


def test_azure_store_put_fails_closed_when_existing_blob_vanishes_before_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the blob that caused ``ResourceExistsError`` cannot be re-read at
    all (e.g. deleted between the failed create and the verification read),
    the create conflict must fail closed rather than silently succeeding
    without ever having verified identity."""
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )

    class _VanishingBlobClient:
        url = "https://fake.blob.core.windows.net/container/vanished"

        def upload_blob(self, *args: Any, **kwargs: Any) -> None:
            raise ResourceExistsError("blob already exists")

        def get_blob_properties(self) -> Any:
            raise ResourceNotFoundError("blob not found")

    monkeypatch.setattr(store._container, "get_blob_client", lambda _name: _VanishingBlobClient())

    with pytest.raises(ArtifactBundleStoreError, match="could not be re-read"):
        store.put(
            tenant_id="demo",
            project_id="proj-1",
            logical_agent_id="agent-1",
            content=b"racy-payload",
            version_label="version-race",
        )


def test_azure_store_put_accepts_explicit_version_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    bundle = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-xyz",
    )
    assert "demo/proj-1/agent-1/version-xyz/" in bundle.uri


def test_azure_store_fails_closed_on_blank_version_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    with pytest.raises(ArtifactBundleStoreError, match="non-empty"):
        store.put(
            tenant_id="demo",
            project_id="proj-1",
            logical_agent_id="agent-1",
            content=b"payload",
            version_label="",
        )
    with pytest.raises(ArtifactBundleStoreError, match="non-empty"):
        store.get(
            scope=ScopeContext(tenant_id="demo", project_id="proj-1"),
            logical_agent_id="agent-1",
            checksum="0" * 64,
            version_label="",
        )


def test_azure_store_get_returns_uploaded_content_within_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    stored = store.put(
        tenant_id="demo",
        project_id="proj-1",
        logical_agent_id="agent-1",
        content=b"payload",
        version_label="version-1",
    )
    checksum = stored.checksum.removeprefix("sha256:")
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert (
        store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum, version_label="version-1") == b"payload"
    )


def test_azure_store_get_returns_none_when_blob_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert store.get(scope=scope, logical_agent_id="agent-1", checksum="0" * 64, version_label="version-1") is None


def test_azure_store_get_fails_closed_when_blob_content_does_not_match_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob present at a content-addressed path should be unreachable
    with content that doesn't match its own path's checksum -- but as with
    the write-side create-race verification, this is defense in depth, not
    an assumption: corruption, a prior bug, or a privileged/out-of-band
    writer placing different bytes at that exact name must fail closed on
    read rather than being silently trusted and returned."""
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    checksum = "0" * 64
    blob_name = f"demo/proj-1/agent-1/version-1/{checksum}"
    store._container.registry[blob_name] = {"content": b"tampered-content", "metadata": {"sha256": checksum}}

    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    with pytest.raises(ArtifactBundleStoreError, match="does not match"):
        store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum, version_label="version-1")


def test_build_artifact_bundle_store_returns_unavailable_when_not_configured() -> None:
    settings = Settings(storage_blob_endpoint=None)
    store = build_artifact_bundle_store(settings)
    assert isinstance(store, UnavailableArtifactBundleStore)


def test_build_artifact_bundle_store_returns_azure_store_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    monkeypatch.setattr(artifact_bundle_store, "DefaultAzureCredential", lambda: "default-credential")
    settings = Settings(storage_blob_endpoint="https://storage.example.test")
    store = build_artifact_bundle_store(settings)
    assert isinstance(store, AzureArtifactBundleStore)


def test_build_artifact_bundle_store_uses_managed_identity_when_client_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeBlobServiceClient:
        def __init__(self, *, account_url: str, credential: Any) -> None:
            captured["credential"] = credential

        def get_container_client(self, _name: str) -> FakeContainerClient:
            return FakeContainerClient()

    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", _FakeBlobServiceClient)
    monkeypatch.setattr(artifact_bundle_store, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}")
    settings = Settings(
        storage_blob_endpoint="https://storage.example.test",
        managed_identity_client_id="client-123",
    )
    build_artifact_bundle_store(settings)
    assert captured["credential"] == "managed:client-123"
