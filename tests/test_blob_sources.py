from __future__ import annotations

from typing import Any

import research_assistant_api.blob_sources as blob_sources
from research_assistant_api.config import Settings


class FakeBlobClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.url = f"https://storage.example.test/container/{name}"
        self.uploads: list[dict[str, Any]] = []

    def upload_blob(self, content: bytes, **kwargs: Any) -> None:
        self.uploads.append({"content": content, **kwargs})


class FakeContainerClient:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlobClient] = {}

    def get_blob_client(self, name: str) -> FakeBlobClient:
        blob = FakeBlobClient(name)
        self.blobs[name] = blob
        return blob


class FakeBlobServiceClient:
    def __init__(self, *, account_url: str, credential: object) -> None:
        self.account_url = account_url
        self.credential = credential
        self.container_names: list[str] = []
        self.container_client = FakeContainerClient()

    def get_container_client(self, name: str) -> FakeContainerClient:
        self.container_names.append(name)
        return self.container_client


class RecordingAzureBlobStore:
    def __init__(self, endpoint: str, container_name: str, credential: object) -> None:
        self.endpoint = endpoint
        self.container_name = container_name
        self.credential = credential


def test_safe_filename_and_in_memory_store_put() -> None:
    store = blob_sources.InMemorySourceBlobStore()

    stored = store.put(
        tenant_id="tenant",
        project_id="project",
        source_id="source-1",
        filename="..\\unsafe///report final?.pdf",
        content_type="application/pdf",
        content=b"hello world",
    )

    assert stored.uri.endswith("tenant/project/source-1/report-final-.pdf")
    assert stored.checksum == "sha256:b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert stored.size_bytes == 11
    assert store.items["tenant/project/source-1/report-final-.pdf"] == b"hello world"
    assert blob_sources._safe_filename("***") == "source.bin"
    assert blob_sources._safe_filename("nested/path/" + "a" * 140 + ".txt") == "a" * 120


def test_azure_source_blob_store_put_uploads_metadata(monkeypatch: Any) -> None:
    service_clients: list[FakeBlobServiceClient] = []

    def fake_blob_service_client(*, account_url: str, credential: object) -> FakeBlobServiceClient:
        client = FakeBlobServiceClient(account_url=account_url, credential=credential)
        service_clients.append(client)
        return client

    monkeypatch.setattr(blob_sources, "BlobServiceClient", fake_blob_service_client)

    store = blob_sources.AzureSourceBlobStore(
        "https://storage.example.test",
        "sources",
        credential="cred",  # type: ignore[arg-type]
    )
    stored = store.put(
        tenant_id="tenant",
        project_id="project",
        source_id="source-1",
        filename="quarterly results.csv",
        content_type="text/csv",
        content=b"a,b\n1,2\n",
    )

    assert service_clients[0].account_url == "https://storage.example.test"
    assert service_clients[0].credential == "cred"
    assert service_clients[0].container_names == ["sources"]
    upload = service_clients[0].container_client.blobs[
        "tenant/project/source-1/quarterly-results.csv"
    ].uploads[0]
    assert upload["content"] == b"a,b\n1,2\n"
    assert upload["overwrite"] is False
    assert upload["metadata"] == {
        "sha256": "492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470",
        "tenant": "tenant",
        "project": "project",
        "source": "source-1",
    }
    assert upload["content_settings"].content_type == "text/csv"
    assert stored.uri.endswith("tenant/project/source-1/quarterly-results.csv")
    assert stored.size_bytes == 8
    assert stored.content_type == "text/csv"


def test_build_source_blob_store_selects_backend_and_credentials(monkeypatch: Any) -> None:
    default_credential = object()
    managed_credential = object()
    requested: list[Any] = []

    def _credential(client_id: Any = None) -> object:
        requested.append(client_id)
        if client_id is None:
            return default_credential
        return {"client_id": client_id, "credential": managed_credential}

    monkeypatch.setattr(blob_sources, "azure_credential", _credential)
    monkeypatch.setattr(blob_sources, "AzureSourceBlobStore", RecordingAzureBlobStore)

    in_memory = blob_sources.build_source_blob_store(Settings(storage_blob_endpoint=None))
    default_store = blob_sources.build_source_blob_store(
        Settings(
            storage_blob_endpoint="https://storage.example.test/",
            storage_source_container="ingest",
        )
    )
    managed_store = blob_sources.build_source_blob_store(
        Settings(
            storage_blob_endpoint="https://storage.example.test/",
            storage_source_container="secure",
            managed_identity_client_id="managed-client",
        )
    )

    assert isinstance(in_memory, blob_sources.InMemorySourceBlobStore)
    assert isinstance(default_store, RecordingAzureBlobStore)
    assert default_store.endpoint == "https://storage.example.test"
    assert default_store.container_name == "ingest"
    assert default_store.credential is default_credential
    assert isinstance(managed_store, RecordingAzureBlobStore)
    assert managed_store.container_name == "secure"
    assert managed_store.credential == {
        "client_id": "managed-client",
        "credential": managed_credential,
    }
    assert requested == [None, "managed-client"]
