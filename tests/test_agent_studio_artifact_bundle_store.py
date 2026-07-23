from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.artifact_bundle_store as artifact_bundle_store
from azure.core.exceptions import ResourceNotFoundError
from research_assistant_api.agent_studio.artifact_bundle_store import (
    UNVERSIONED,
    ArtifactBundleStoreError,
    AzureArtifactBundleStore,
    InMemoryArtifactBundleStore,
    UnavailableArtifactBundleStore,
    build_artifact_bundle_store,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


def test_in_memory_store_is_content_addressed_and_idempotent() -> None:
    store = InMemoryArtifactBundleStore()
    first = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"bundle-bytes")
    second = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"bundle-bytes")
    assert first == second
    assert first.checksum.startswith("sha256:")
    assert first.size_bytes == len(b"bundle-bytes")
    assert len(store.items) == 1


def test_in_memory_store_distinguishes_different_content() -> None:
    store = InMemoryArtifactBundleStore()
    first = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"content-a")
    second = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"content-b")
    assert first.checksum != second.checksum
    assert len(store.items) == 2


def test_in_memory_store_scopes_path_by_project_and_version_label() -> None:
    store = InMemoryArtifactBundleStore()
    proj_a = store.put(
        tenant_id="demo", project_id="proj-a", logical_agent_id="agent-1", content=b"same-bytes"
    )
    proj_b = store.put(
        tenant_id="demo", project_id="proj-b", logical_agent_id="agent-1", content=b"same-bytes"
    )
    # Same tenant/agent/content but different project: distinct blob keys (no cross-project collision).
    assert proj_a.uri != proj_b.uri
    assert len(store.items) == 2

    versioned = store.put(
        tenant_id="demo",
        project_id="proj-a",
        logical_agent_id="agent-1",
        content=b"same-bytes",
        version_label="version-123",
    )
    assert versioned.uri != proj_a.uri
    assert len(store.items) == 3


def test_in_memory_store_get_returns_content_within_scope() -> None:
    store = InMemoryArtifactBundleStore()
    stored = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"payload")
    checksum = stored.checksum.removeprefix("sha256:")
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum) == b"payload"


def test_in_memory_store_get_returns_none_for_wrong_scope_or_missing_content() -> None:
    store = InMemoryArtifactBundleStore()
    stored = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"payload")
    checksum = stored.checksum.removeprefix("sha256:")

    other_tenant = ScopeContext(tenant_id="other-tenant", project_id="proj-1")
    assert store.get(scope=other_tenant, logical_agent_id="agent-1", checksum=checksum) is None

    other_project = ScopeContext(tenant_id="demo", project_id="other-project")
    assert store.get(scope=other_project, logical_agent_id="agent-1", checksum=checksum) is None

    same_scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert store.get(scope=same_scope, logical_agent_id="agent-1", checksum="0" * 64) is None


def test_unavailable_store_raises_on_put_and_get() -> None:
    store = UnavailableArtifactBundleStore()
    with pytest.raises(ArtifactBundleStoreError, match="unavailable"):
        store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"bundle-bytes")
    with pytest.raises(ArtifactBundleStoreError, match="unavailable"):
        store.get(
            scope=ScopeContext(tenant_id="demo", project_id="proj-1"),
            logical_agent_id="agent-1",
            checksum="0" * 64,
        )


class FakeBlobClient:
    def __init__(self, registry: dict[str, bytes], name: str) -> None:
        self._registry = registry
        self._name = name
        self.url = f"https://fake.blob.core.windows.net/container/{name}"

    def exists(self) -> bool:
        return self._name in self._registry

    def upload_blob(self, content: bytes, *, overwrite: bool, metadata: dict[str, str], content_settings: Any) -> None:
        assert overwrite is False
        self._registry[self._name] = content

    def download_blob(self) -> Any:
        if self._name not in self._registry:
            raise ResourceNotFoundError("blob not found")
        content = self._registry[self._name]

        class _Downloaded:
            def readall(self) -> bytes:
                return content

        return _Downloaded()


class FakeContainerClient:
    def __init__(self) -> None:
        self.registry: dict[str, bytes] = {}

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
    bundle = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"payload")
    assert bundle.uri.startswith("https://fake.blob.core.windows.net/container/")
    assert "demo/proj-1/agent-1/" + UNVERSIONED in bundle.uri
    assert bundle.checksum.startswith("sha256:")
    fake_container = cast(FakeContainerClient, store._container)
    assert len(fake_container.registry) == 1

    # Uploading the same content again must not re-upload (idempotent put).
    same = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"payload")
    assert same.uri == bundle.uri
    assert len(fake_container.registry) == 1


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
        version_label="version-abc",
    )
    assert "demo/proj-1/agent-1/version-abc/" in bundle.uri


def test_azure_store_get_returns_uploaded_content_within_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    stored = store.put(tenant_id="demo", project_id="proj-1", logical_agent_id="agent-1", content=b"payload")
    checksum = stored.checksum.removeprefix("sha256:")
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert store.get(scope=scope, logical_agent_id="agent-1", checksum=checksum) == b"payload"


def test_azure_store_get_returns_none_when_blob_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_bundle_store, "BlobServiceClient", FakeBlobServiceClient)
    store = AzureArtifactBundleStore(
        "https://storage.example.test", "bundles", credential=cast("TokenCredential", object())
    )
    scope = ScopeContext(tenant_id="demo", project_id="proj-1")
    assert store.get(scope=scope, logical_agent_id="agent-1", checksum="0" * 64) is None


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
    monkeypatch.setattr(
        artifact_bundle_store, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}"
    )
    settings = Settings(
        storage_blob_endpoint="https://storage.example.test",
        managed_identity_client_id="client-123",
    )
    build_artifact_bundle_store(settings)
    assert captured["credential"] == "managed:client-123"
