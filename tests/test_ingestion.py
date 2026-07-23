from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
import research_assistant_worker.ingestion as ingestion


class FakeDownload:
    def __init__(self, content: bytes) -> None:
        self._content = content

    def readall(self) -> bytes:
        return self._content

    def chunks(self) -> list[bytes]:
        return [self._content[index : index + 1024] for index in range(0, len(self._content), 1024)]


class FakeBlob:
    def __init__(self, content: bytes = b"", url: str = "https://blob/manifest") -> None:
        self._content = content
        self.url = url
        self.uploaded: bytes | None = None

    def download_blob(self, **_kwargs: Any) -> FakeDownload:
        return FakeDownload(self._content)

    def upload_blob(self, content: bytes, **_kwargs: Any) -> None:
        self.uploaded = content


class FakeBlobService:
    manifest_blob = FakeBlob(
        url="https://storage.example.test/sources/demo/demo-project/source/extracted-manifest.json"
    )

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def get_blob_client(self, **_kwargs: Any) -> FakeBlob:
        return self.manifest_blob


def settings() -> ingestion.IngestionSettings:
    return ingestion.IngestionSettings(
        storage_endpoint="https://storage.example.test",
        source_container="sources",
        document_intelligence_endpoint="https://di.example.test",
        search_endpoint="https://search.example.test",
        search_index="evidence",
        openai_endpoint="https://foundry.example.test/",
        cosmos_endpoint="https://cosmos.example.test",
        cosmos_database="research",
        embedding_deployment="text-embedding-3-large",
        managed_identity_client_id=None,
    )


def test_text_ingestion_extracts_structural_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b"# Protocol\n\nEligibility is defined before retrieval.\n\n"
        + b"Methods and limitations must be extractable. " * 80
    )
    source_blob = FakeBlob(source, "https://blob/source")
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: source_blob)
    monkeypatch.setattr(ingestion, "BlobServiceClient", FakeBlobService)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)
    FakeBlobService.manifest_blob = FakeBlob(
        url=("https://storage.example.test/sources/demo/demo-project/source-abc123abc123/extracted-manifest.json")
    )

    result = ingestion.extract_source(
        {
            "run_id": "run-ingest-1",
            "source_id": "source-abc123abc123",
            "blob_uri": "https://blob/source",
            "content_type": "text/plain",
            "checksum": f"sha256:{sha256(source).hexdigest()}",
            "title": "Protocol",
            "kind": "Policy",
            "tenant_id": "demo",
            "project_id": "demo-project",
            "access": "internal",
            "year": 2026,
            "provider": "Workspace upload",
            "group_ids": ["researchers"],
            "license": "Project supplied",
        }
    )

    assert result["status"] == "extracted"
    assert result["chunk_count"] >= 2
    assert FakeBlobService.manifest_blob.uploaded is not None
    manifest = json.loads(FakeBlobService.manifest_blob.uploaded)
    assert manifest["checksum"].startswith("sha256:")
    assert manifest["access"] == "internal"
    assert all(len(chunk["content"]) <= 1800 for chunk in manifest["chunks"])


def test_indexing_embeds_and_uploads_every_manifest_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {
        "source_id": "source-abc123abc123",
        "title": "Protocol",
        "kind": "Policy",
        "tenant_id": "demo",
        "project_id": "demo-project",
        "group_ids": ["researchers"],
        "access": "internal",
        "year": 2026,
        "provider": "Workspace upload",
        "license": "Project supplied",
        "version": "1.0",
        "checksum": "sha256:test",
        "chunks": [
            {
                "id": "chunk-1",
                "section": "Page 1",
                "page_start": 1,
                "content": "Eligibility is defined before retrieval.",
            },
            {
                "id": "chunk-2",
                "section": "Page 2",
                "page_start": 2,
                "content": "Methods and limitations must be extractable.",
            },
        ],
    }
    manifest_blob = FakeBlob(json.dumps(manifest).encode())
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: manifest_blob)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())

    class FakeEmbeddings:
        def create(self, **kwargs: Any) -> SimpleNamespace:
            assert kwargs["model"] == "text-embedding-3-large"
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[0.1, 0.2]),
                    SimpleNamespace(embedding=[0.3, 0.4]),
                ]
            )

    class FakeAzureOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.embeddings = FakeEmbeddings()

    uploaded: list[dict[str, Any]] = []
    activated: list[dict[str, Any]] = []

    class FakeSearch:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_documents(
            self,
            *,
            documents: list[dict[str, Any]],
        ) -> list[SimpleNamespace]:
            uploaded.extend(documents)
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

        def merge_documents(
            self,
            *,
            documents: list[dict[str, Any]],
        ) -> list[SimpleNamespace]:
            activated.extend(documents)
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

        def delete_documents(
            self,
            *,
            documents: list[dict[str, Any]],
        ) -> list[SimpleNamespace]:
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

    updates: list[tuple[str, int]] = []
    monkeypatch.setattr(ingestion, "AzureOpenAI", FakeAzureOpenAI)
    monkeypatch.setattr(
        ingestion,
        "get_bearer_token_provider",
        lambda *_args, **_kwargs: lambda: "token",
    )
    monkeypatch.setattr(ingestion, "SearchClient", FakeSearch)
    monkeypatch.setattr(
        ingestion,
        "_update_library",
        lambda _payload, *, status, evidence_count: updates.append((status, evidence_count)),
    )
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    result = ingestion.index_extracted_source(
        {
            "run_id": "run-ingest-1",
            "source_id": "source-abc123abc123",
            "query": "ingest protocol",
            "extracted_manifest_uri": (
                "https://storage.example.test/sources/demo/demo-project/source-abc123abc123/extracted-manifest.json"
            ),
            "tenant_id": "demo",
            "project_id": "demo-project",
        }
    )

    assert result["passage_count"] == 2
    assert len(uploaded) == 2
    assert uploaded[0]["source_kind"] == "policy"
    assert uploaded[0]["project_ids"] == ["demo-project"]
    assert uploaded[0]["group_ids"] == ["researchers"]
    assert uploaded[0]["access"] == "internal"
    assert uploaded[0]["content_vector"] == [0.1, 0.2]
    assert all(document["ingestion_status"] == "staging" for document in uploaded)
    assert all(document["safety_status"] == "safe" for document in uploaded)
    assert activated == [
        {"id": "chunk-1", "ingestion_status": "ready"},
        {"id": "chunk-2", "ingestion_status": "ready"},
    ]
    assert updates == [("ready", 2)]


def test_blob_reference_must_match_configured_account_and_container() -> None:
    with pytest.raises(ValueError, match="configured storage account"):
        ingestion._blob_from_uri("https://attacker.example/source.pdf", settings())

    with pytest.raises(ValueError, match="source container"):
        ingestion._blob_from_uri(
            "https://storage.example.test/other/source.pdf",
            settings(),
        )


def test_blob_download_stops_at_hard_limit() -> None:
    blob = FakeBlob(b"A" * 20)

    with pytest.raises(ValueError, match="10-byte"):
        ingestion._download_bounded(blob, 10)


def test_search_batches_respect_document_count_and_payload_limits() -> None:
    documents = [{"id": f"chunk-{index}", "content": "evidence"} for index in range(1_201)]

    batches = list(ingestion._search_batches(documents))

    assert [len(batch) for batch in batches] == [500, 500, 201]
    assert all(
        len(json.dumps(batch, separators=(",", ":")).encode()) <= ingestion._SEARCH_BATCH_BYTES for batch in batches
    )
