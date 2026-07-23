from __future__ import annotations

import json
from collections.abc import Iterator
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
import research_assistant_worker.ingestion as ingestion
from azure.core.exceptions import HttpResponseError


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
        self.upload_kwargs: dict[str, Any] | None = None

    def download_blob(self, **_kwargs: Any) -> FakeDownload:
        return FakeDownload(self._content)

    def upload_blob(self, content: bytes, **kwargs: Any) -> None:
        self.uploaded = content
        self.upload_kwargs = kwargs


class FakeBlobService:
    manifest_blob = FakeBlob(
        url="https://storage.example.test/sources/demo/demo-project/source/extracted-manifest.json"
    )

    def __init__(self, **_kwargs: Any) -> None:
        pass

    def get_blob_client(self, **_kwargs: Any) -> FakeBlob:
        return self.manifest_blob


@pytest.fixture(autouse=True)
def clear_credential_cache() -> Iterator[None]:
    ingestion.credential.cache_clear()
    yield
    ingestion.credential.cache_clear()


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


def _manifest(
    *,
    kind: str = "Policy",
    chunks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "source_id": "source-abc123abc123",
        "title": "Protocol",
        "kind": kind,
        "tenant_id": "demo",
        "project_id": "demo-project",
        "group_ids": ["researchers"],
        "access": "internal",
        "year": 2026,
        "provider": "Workspace upload",
        "license": "Project supplied",
        "version": "1.0",
        "checksum": "sha256:test",
        "chunks": chunks
        or [
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


def _payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "run_id": "run-ingest-1",
        "source_id": "source-abc123abc123",
        "query": "ingest protocol",
        "tenant_id": "demo",
        "project_id": "demo-project",
    }
    payload.update(overrides)
    return payload


def test_required_setting_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_SETTING", raising=False)

    with pytest.raises(RuntimeError, match="requires MISSING_SETTING"):
        ingestion._required("MISSING_SETTING")


def test_ingestion_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_STORAGE_BLOB_ENDPOINT", "https://storage.example.test")
    monkeypatch.setenv("AZURE_STORAGE_SOURCE_CONTAINER", "custom-sources")
    monkeypatch.setenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "https://di.example.test")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.example.test")
    monkeypatch.setenv("AZURE_SEARCH_INDEX_NAME", "evidence")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://openai.example.test")
    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setenv("AZURE_COSMOS_DATABASE", "custom-research")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT_NAME", "embeddings-small")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-123")

    resolved = ingestion.ingestion_settings()

    assert resolved == ingestion.IngestionSettings(
        storage_endpoint="https://storage.example.test",
        source_container="custom-sources",
        document_intelligence_endpoint="https://di.example.test",
        search_endpoint="https://search.example.test",
        search_index="evidence",
        openai_endpoint="https://openai.example.test",
        cosmos_endpoint="https://cosmos.example.test",
        cosmos_database="custom-research",
        embedding_deployment="embeddings-small",
        managed_identity_client_id="client-123",
    )


def test_credential_prefers_managed_identity_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeManagedIdentityCredential:
        def __init__(self, *, client_id: str) -> None:
            created.append(client_id)
            self.client_id = client_id

    monkeypatch.setenv("AZURE_CLIENT_ID", "client-123")
    monkeypatch.setattr(ingestion, "ManagedIdentityCredential", FakeManagedIdentityCredential)
    monkeypatch.setattr(ingestion, "DefaultAzureCredential", lambda: pytest.fail("default credential not expected"))

    first = ingestion.credential()
    second = ingestion.credential()

    assert first is second
    assert first.client_id == "client-123"  # type: ignore[attr-defined]
    assert created == ["client-123"]


def test_credential_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeDefaultAzureCredential:
        def __init__(self) -> None:
            created.append("default")

    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.setattr(
        ingestion,
        "ManagedIdentityCredential",
        lambda **_kwargs: pytest.fail("managed identity not expected"),
    )
    monkeypatch.setattr(ingestion, "DefaultAzureCredential", FakeDefaultAzureCredential)

    credential = ingestion.credential()

    assert isinstance(credential, FakeDefaultAzureCredential)
    assert created == ["default"]


def test_run_and_library_updates_are_noops_without_cosmos_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_COSMOS_ENDPOINT", raising=False)
    monkeypatch.setattr(
        ingestion,
        "CosmosClient",
        lambda *_args, **_kwargs: pytest.fail("CosmosClient should not be constructed"),
    )

    ingestion._update_run(
        _payload(),
        status="running",
        progress=10,
        current_stage="Extract structure",
    )
    ingestion._update_library(_payload(), status="ready", evidence_count=3)


def test_update_run_marks_all_stages_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {
        "payload": {
            "started_at": "2026-01-01T00:00:00+00:00",
            "stages": [
                {"label": "Extract structure", "status": "running"},
                "skip-me",
                {
                    "label": "Chunk, embed & index",
                    "status": "planned",
                    "started_at": "existing-start",
                    "completed_at": "existing-end",
                },
            ],
        }
    }
    reads: list[tuple[str, str]] = []
    upserts: list[dict[str, Any]] = []

    class FakeContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
            reads.append((item, partition_key))
            return document

        def upsert_item(self, item: dict[str, Any]) -> None:
            upserts.append(json.loads(json.dumps(item)))

    class FakeCosmosClient:
        def __init__(self, endpoint: str, credential: object) -> None:
            assert endpoint == "https://cosmos.example.test"
            assert credential == "credential"

        def get_database_client(self, database_name: str) -> SimpleNamespace:
            assert database_name == "research"
            return SimpleNamespace(get_container_client=lambda name: FakeContainer())

    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setenv("AZURE_COSMOS_DATABASE", "research")
    monkeypatch.setattr(ingestion, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    ingestion._update_run(
        _payload(run_id="run-complete"),
        status="completed",
        progress=100,
        current_stage="Indexed and ready",
        completed=True,
    )

    assert reads == [("run-complete", "demo|run-complete")]
    saved = upserts[0]["payload"]
    assert saved["status"] == "completed"
    assert saved["progress"] == 100
    assert saved["current_stage"] == "Indexed and ready"
    assert saved["completed_at"]
    assert saved["stages"][0]["status"] == "completed"
    assert saved["stages"][0]["started_at"] == "2026-01-01T00:00:00+00:00"
    assert saved["stages"][0]["completed_at"]
    assert saved["stages"][1] == "skip-me"
    assert saved["stages"][2]["status"] == "completed"
    assert saved["stages"][2]["started_at"] == "existing-start"
    assert saved["stages"][2]["completed_at"] == "existing-end"


@pytest.mark.parametrize(
    ("status", "expected_current"),
    [
        ("running", "running"),
        ("waiting_for_approval", "waiting_for_approval"),
        ("blocked", "failed"),
    ],
)
def test_update_run_tracks_current_stage_progression(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_current: str,
) -> None:
    document = {
        "payload": {
            "stages": [
                {"label": "Extract structure", "status": "running"},
                "skip-me",
                {"label": "Human review", "status": "planned"},
                {"label": "Complete", "status": "planned"},
            ]
        }
    }
    upserts: list[dict[str, Any]] = []

    class FakeContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
            assert item == "run-stage"
            assert partition_key == "demo|run-stage"
            return document

        def upsert_item(self, item: dict[str, Any]) -> None:
            upserts.append(json.loads(json.dumps(item)))

    class FakeCosmosClient:
        def __init__(self, endpoint: str, credential: object) -> None:
            assert endpoint == "https://cosmos.example.test"
            assert credential == "credential"

        def get_database_client(self, database_name: str) -> SimpleNamespace:
            assert database_name == "research"
            return SimpleNamespace(get_container_client=lambda name: FakeContainer())

    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setattr(ingestion, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    ingestion._update_run(
        _payload(run_id="run-stage"),
        status=status,
        progress=80,
        current_stage="Human review",
    )

    stages = upserts[0]["payload"]["stages"]
    assert stages[0]["status"] == "completed"
    assert stages[1] == "skip-me"
    assert stages[2]["status"] == expected_current
    assert stages[3]["status"] == "planned"


def test_update_run_marks_first_active_stage_failed_when_label_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "payload": {
            "stages": [
                {"label": "Extract structure", "status": "completed"},
                {"label": "Human review", "status": "waiting_for_approval"},
                {"label": "Complete", "status": "planned"},
            ]
        }
    }
    upserts: list[dict[str, Any]] = []

    class FakeContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
            assert item == "run-missing-stage"
            assert partition_key == "demo|run-missing-stage"
            return document

        def upsert_item(self, item: dict[str, Any]) -> None:
            upserts.append(json.loads(json.dumps(item)))

    class FakeCosmosClient:
        def __init__(self, endpoint: str, credential: object) -> None:
            assert endpoint == "https://cosmos.example.test"
            assert credential == "credential"

        def get_database_client(self, database_name: str) -> SimpleNamespace:
            assert database_name == "research"
            return SimpleNamespace(get_container_client=lambda name: FakeContainer())

    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setattr(ingestion, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    ingestion._update_run(
        _payload(run_id="run-missing-stage"),
        status="failed",
        progress=100,
        current_stage="Missing stage",
        completed=True,
    )

    saved = upserts[0]["payload"]
    assert saved["completed_at"]
    assert saved["stages"][1]["status"] == "failed"
    assert saved["stages"][2]["status"] == "planned"


def test_update_run_handles_missing_or_inactive_stage_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents: list[dict[str, Any]] = [
        {"payload": {"status": "planned", "stages": None}},
        {
            "payload": {
                "stages": [
                    {"label": "Extract structure", "status": "completed"},
                    {"label": "Human review", "status": "failed"},
                ]
            }
        },
        {
            "payload": {
                "stages": [
                    {"label": "Extract structure", "status": "completed"},
                    {"label": "Complete", "status": "completed"},
                ]
            }
        },
    ]
    upserts: list[dict[str, Any]] = []

    class FakeContainer:
        def __init__(self) -> None:
            self._documents = iter(documents)

        def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
            assert item == "run-misc"
            assert partition_key == "demo|run-misc"
            return next(self._documents)

        def upsert_item(self, item: dict[str, Any]) -> None:
            upserts.append(json.loads(json.dumps(item)))

    container = FakeContainer()

    class FakeCosmosClient:
        def __init__(self, endpoint: str, credential: object) -> None:
            assert endpoint == "https://cosmos.example.test"
            assert credential == "credential"

        def get_database_client(self, database_name: str) -> SimpleNamespace:
            assert database_name == "research"
            return SimpleNamespace(get_container_client=lambda name: container)

    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setattr(ingestion, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    ingestion._update_run(
        _payload(run_id="run-misc"),
        status="running",
        progress=10,
        current_stage="Missing stage",
    )
    ingestion._update_run(
        _payload(run_id="run-misc"),
        status="running",
        progress=20,
        current_stage="Still missing",
    )
    ingestion._update_run(
        _payload(run_id="run-misc"),
        status="failed",
        progress=100,
        current_stage="Still missing",
        completed=True,
    )

    assert upserts[0]["payload"]["status"] == "running"
    assert upserts[0]["payload"]["stages"] is None
    assert upserts[1]["payload"]["stages"][0]["status"] == "completed"
    assert upserts[1]["payload"]["stages"][1]["status"] == "failed"
    assert upserts[2]["payload"]["stages"][0]["status"] == "completed"
    assert upserts[2]["payload"]["stages"][1]["status"] == "completed"


def test_update_library_mutates_source_document(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"payload": {"status": "planned", "evidence_count": 0}}
    reads: list[tuple[str, str]] = []
    upserts: list[dict[str, Any]] = []

    class FakeContainer:
        def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
            reads.append((item, partition_key))
            return document

        def upsert_item(self, item: dict[str, Any]) -> None:
            upserts.append(json.loads(json.dumps(item)))

    class FakeCosmosClient:
        def __init__(self, endpoint: str, credential: object) -> None:
            assert endpoint == "https://cosmos.example.test"
            assert credential == "credential"

        def get_database_client(self, database_name: str) -> SimpleNamespace:
            assert database_name == "research"
            return SimpleNamespace(get_container_client=lambda name: FakeContainer())

    monkeypatch.setenv("AZURE_COSMOS_ENDPOINT", "https://cosmos.example.test")
    monkeypatch.setattr(ingestion, "CosmosClient", FakeCosmosClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    ingestion._update_library(_payload(source_id="source-42"), status="ready", evidence_count=7)

    assert reads == [("source-42", "demo|demo-project")]
    assert upserts[0]["payload"] == {
        "status": "ready",
        "evidence_count": 7,
        "version": "1.0",
    }


def test_extract_pages_returns_plain_text_without_document_intelligence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ingestion,
        "DocumentIntelligenceClient",
        lambda **_kwargs: pytest.fail("Document Intelligence should not be used for plain text"),
    )

    pages = ingestion._extract_pages(b"\xef\xbb\xbfHello world", "text/markdown", settings())

    assert pages == [(1, "Hello world")]


def test_extract_pages_uses_document_intelligence_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    result = SimpleNamespace(
        content="Alpha Beta  Gamma",
        pages=[
            SimpleNamespace(
                page_number=1,
                spans=[
                    SimpleNamespace(offset=0, length=6),
                    SimpleNamespace(offset=6, length=4),
                ],
            ),
            SimpleNamespace(page_number=2, spans=[SimpleNamespace(offset=10, length=2)]),
            SimpleNamespace(page_number=3, spans=[SimpleNamespace(offset=12, length=5)]),
        ],
    )

    class FakeDocumentIntelligenceClient:
        def __init__(self, *, endpoint: str, credential: object) -> None:
            calls.append({"endpoint": endpoint, "credential": credential})

        def begin_analyze_document(self, model: str, stream: Any, **kwargs: Any) -> SimpleNamespace:
            calls.append({"model": model, "stream": stream.read(), "kwargs": kwargs})
            return SimpleNamespace(result=lambda: result)

    monkeypatch.setattr(ingestion, "DocumentIntelligenceClient", FakeDocumentIntelligenceClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    pages = ingestion._extract_pages(b"%PDF-1.7", "application/pdf", settings())

    assert pages == [(1, "Alpha Beta"), (3, "Gamma")]
    assert calls[0] == {
        "endpoint": "https://di.example.test",
        "credential": "credential",
    }
    assert calls[1]["model"] == "prebuilt-layout"
    assert calls[1]["stream"] == b"%PDF-1.7"


def test_extract_pages_falls_back_to_full_content_when_page_spans_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(content="Recovered full text", pages=[SimpleNamespace(page_number=1, spans=[])])

    class FakeDocumentIntelligenceClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def begin_analyze_document(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(result=lambda: result)

    monkeypatch.setattr(ingestion, "DocumentIntelligenceClient", FakeDocumentIntelligenceClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    pages = ingestion._extract_pages(b"%PDF-1.7", "application/pdf", settings())

    assert pages == [(1, "Recovered full text")]


def test_blob_reference_must_match_configured_account_and_container() -> None:
    with pytest.raises(ValueError, match="configured storage account"):
        ingestion._blob_from_uri("https://attacker.example/source.pdf", settings())

    with pytest.raises(ValueError, match="source container"):
        ingestion._blob_from_uri(
            "https://storage.example.test/other/source.pdf",
            settings(),
        )


def test_blob_reference_returns_blob_client_for_valid_source_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, str]] = []
    blob_client = object()

    class FakeBlobServiceClient:
        def __init__(self, *, account_url: str, credential: object) -> None:
            assert account_url == "https://storage.example.test"
            assert credential == "credential"

        def get_blob_client(self, *, container: str, blob: str) -> object:
            seen.append({"container": container, "blob": blob})
            return blob_client

    monkeypatch.setattr(ingestion, "BlobServiceClient", FakeBlobServiceClient)
    monkeypatch.setattr(ingestion, "credential", lambda: "credential")

    resolved = ingestion._blob_from_uri(
        "https://storage.example.test/sources/demo%20folder/source%20name.pdf",
        settings(),
    )

    assert resolved is blob_client
    assert seen == [{"container": "sources", "blob": "demo folder/source name.pdf"}]


def test_blob_download_stops_at_hard_limit() -> None:
    blob = FakeBlob(b"A" * 20)

    with pytest.raises(ValueError, match="10-byte"):
        ingestion._download_bounded(blob, 10)


def test_extract_source_short_circuits_without_blob_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        ingestion,
        "_update_run",
        lambda _payload, **state: updates.append(state),
    )

    result = ingestion.extract_source(_payload(blob_uri=None))

    assert result == {
        "source_id": "source-abc123abc123",
        "blob_uri": "blob://sources/source-abc123abc123",
        "status": "verified",
    }
    assert updates == [
        {
            "status": "running",
            "progress": 20,
            "current_stage": "Extract structure",
        }
    ]


def test_extract_source_rejects_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: FakeBlob(b""))
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="between 1 byte and 20 MB"):
        ingestion.extract_source(_payload(blob_uri="https://storage.example.test/sources/source.txt"))


def test_extract_source_rejects_checksum_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"source content"
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: FakeBlob(content))
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="checksum does not match"):
        ingestion.extract_source(
            _payload(
                blob_uri="https://storage.example.test/sources/source.txt",
                checksum="sha256:not-the-right-digest",
            )
        )


def test_extract_source_rejects_empty_manifest_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"document"
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: FakeBlob(content))
    monkeypatch.setattr(ingestion, "_extract_pages", lambda *_args: [(1, "page text")])
    monkeypatch.setattr(ingestion, "chunk_text", lambda _text: [])
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="did not extract any source text"):
        ingestion.extract_source(
            _payload(
                blob_uri="https://storage.example.test/sources/source.txt",
                checksum=f"sha256:{sha256(content).hexdigest()}",
            )
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
        url="https://storage.example.test/sources/demo/demo-project/source-abc123abc123/extracted-manifest.json"
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

    assert result == {
        "source_id": "source-abc123abc123",
        "blob_uri": "https://blob/source",
        "extracted_manifest_uri": FakeBlobService.manifest_blob.url,
        "chunk_count": result["chunk_count"],
        "status": "extracted",
    }
    assert result["chunk_count"] >= 2
    assert FakeBlobService.manifest_blob.uploaded is not None
    manifest = json.loads(FakeBlobService.manifest_blob.uploaded)
    ids = [chunk["id"] for chunk in manifest["chunks"]]
    assert manifest["checksum"].startswith("sha256:")
    assert manifest["access"] == "internal"
    assert ids[0] == "source-abc123abc123-chunk-0000"
    assert ids == [f"source-abc123abc123-chunk-{index:04d}" for index in range(len(ids))]
    assert all(len(chunk["content"]) <= 1800 for chunk in manifest["chunks"])


def test_search_batches_respect_document_count_and_payload_limits() -> None:
    documents = [{"id": f"chunk-{index}", "content": "evidence"} for index in range(1_201)]

    batches = list(ingestion._search_batches(documents))

    assert [len(batch) for batch in batches] == [500, 500, 201]
    assert all(
        len(json.dumps(batch, separators=(",", ":")).encode()) <= ingestion._SEARCH_BATCH_BYTES for batch in batches
    )


def test_search_batches_return_no_batches_for_empty_input() -> None:
    assert list(ingestion._search_batches([])) == []


def test_search_batches_reject_single_document_that_exceeds_byte_limit() -> None:
    oversize = [{"id": "chunk-1", "content": "x" * ingestion._SEARCH_BATCH_BYTES}]

    with pytest.raises(ValueError, match="exceeds the batch byte limit"):
        list(ingestion._search_batches(oversize))


def test_indexing_without_manifest_updates_library_only_for_library_workflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_updates: list[dict[str, Any]] = []
    library_updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        ingestion,
        "_update_run",
        lambda _payload, **state: run_updates.append(state),
    )
    monkeypatch.setattr(
        ingestion,
        "_update_library",
        lambda _payload, *, status, evidence_count: library_updates.append(
            {"status": status, "evidence_count": evidence_count}
        ),
    )

    retrieval = ingestion.index_extracted_source(_payload(extracted_manifest_uri=None))
    library = ingestion.index_extracted_source(
        _payload(extracted_manifest_uri=None, workflow_kind="library_ingestion")
    )

    assert retrieval == {
        "query": "ingest protocol",
        "evidence_manifest_uri": "blob://evidence/run-ingest-1.json",
        "passage_count": 3,
    }
    assert library == {
        "query": "ingest protocol",
        "evidence_manifest_uri": "blob://evidence/run-ingest-1.json",
        "passage_count": 0,
    }
    assert run_updates == [
        {
            "status": "running",
            "progress": 50,
            "current_stage": "Retrieve authorized evidence",
        },
        {
            "status": "running",
            "progress": 50,
            "current_stage": "Retrieve authorized evidence",
        },
    ]
    assert library_updates == [{"status": "needs_review", "evidence_count": 0}]


def test_indexing_embeds_and_uploads_every_manifest_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_blob = FakeBlob(json.dumps(_manifest()).encode())
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
        _payload(
            extracted_manifest_uri=(
                "https://storage.example.test/sources/demo/demo-project/"
                "source-abc123abc123/extracted-manifest.json"
            )
        )
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


def test_indexing_marks_quarantined_chunks_for_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_blob = FakeBlob(json.dumps(_manifest()).encode())
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: manifest_blob)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())
    monkeypatch.setattr(
        ingestion,
        "scan_untrusted_content",
        lambda content: content.startswith("Methods"),
    )

    class FakeEmbeddings:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
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
    run_updates: list[dict[str, Any]] = []
    library_updates: list[dict[str, Any]] = []

    class FakeSearch:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            uploaded.extend(documents)
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

        def merge_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

        def delete_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

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
        lambda _payload, *, status, evidence_count: library_updates.append(
            {"status": status, "evidence_count": evidence_count}
        ),
    )
    monkeypatch.setattr(
        ingestion,
        "_update_run",
        lambda _payload, **state: run_updates.append(state),
    )

    result = ingestion.index_extracted_source(
        _payload(
            extracted_manifest_uri=(
                "https://storage.example.test/sources/demo/demo-project/"
                "source-abc123abc123/extracted-manifest.json"
            )
        )
    )

    assert result["passage_count"] == 1
    assert [document["safety_status"] for document in uploaded] == ["safe", "quarantined"]
    assert library_updates == [{"status": "needs_review", "evidence_count": 1}]
    assert run_updates[-1] == {
        "status": "completed",
        "progress": 100,
        "current_stage": "Indexed and ready",
        "completed": True,
    }


def test_indexing_rejects_oversized_search_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge_manifest = _manifest(
        chunks=[
            {
                "id": "chunk-1",
                "section": "Page 1",
                "page_start": 1,
                "content": "x" * ingestion._SEARCH_BATCH_BYTES,
            }
        ]
    )
    manifest_blob = FakeBlob(json.dumps(huge_manifest).encode())
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: manifest_blob)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())

    class FakeEmbeddings:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])

    class FakeAzureOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.embeddings = FakeEmbeddings()

    class FakeSearch:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_documents(self, **_kwargs: Any) -> list[SimpleNamespace]:
            raise AssertionError("upload should not run for oversized documents")

        def merge_documents(self, **_kwargs: Any) -> list[SimpleNamespace]:
            raise AssertionError("merge should not run for oversized documents")

        def delete_documents(self, **_kwargs: Any) -> list[SimpleNamespace]:
            raise AssertionError("cleanup should not run without staged ids")

    monkeypatch.setattr(ingestion, "AzureOpenAI", FakeAzureOpenAI)
    monkeypatch.setattr(
        ingestion,
        "get_bearer_token_provider",
        lambda *_args, **_kwargs: lambda: "token",
    )
    monkeypatch.setattr(ingestion, "SearchClient", FakeSearch)
    monkeypatch.setattr(ingestion, "_update_library", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="exceeds the batch byte limit"):
        ingestion.index_extracted_source(
            _payload(
                extracted_manifest_uri=(
                    "https://storage.example.test/sources/demo/demo-project/"
                    "source-abc123abc123/extracted-manifest.json"
                )
            )
        )


def test_indexing_cleans_up_staged_chunks_after_staging_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_blob = FakeBlob(json.dumps(_manifest()).encode())
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: manifest_blob)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())

    class FakeEmbeddings:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[0.1, 0.2]),
                    SimpleNamespace(embedding=[0.3, 0.4]),
                ]
            )

    class FakeAzureOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.embeddings = FakeEmbeddings()

    deleted: list[list[dict[str, Any]]] = []

    class FakeSearch:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(key=documents[0]["id"], succeeded=True),
                SimpleNamespace(key=documents[1]["id"], succeeded=False),
            ]

        def merge_documents(self, **_kwargs: Any) -> list[SimpleNamespace]:
            raise AssertionError("activation should not run after staging failure")

        def delete_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            deleted.append(documents)
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

    monkeypatch.setattr(ingestion, "AzureOpenAI", FakeAzureOpenAI)
    monkeypatch.setattr(
        ingestion,
        "get_bearer_token_provider",
        lambda *_args, **_kwargs: lambda: "token",
    )
    monkeypatch.setattr(ingestion, "SearchClient", FakeSearch)
    monkeypatch.setattr(ingestion, "_update_library", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Search staging failed for chunks: \\['chunk-2'\\]"):
        ingestion.index_extracted_source(
            _payload(
                extracted_manifest_uri=(
                    "https://storage.example.test/sources/demo/demo-project/"
                    "source-abc123abc123/extracted-manifest.json"
                )
            )
        )

    assert deleted == [[{"id": "chunk-1"}]]


def test_indexing_reports_cleanup_failure_after_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_blob = FakeBlob(json.dumps(_manifest()).encode())
    monkeypatch.setattr(ingestion, "_blob_from_uri", lambda *_args: manifest_blob)
    monkeypatch.setattr(ingestion, "ingestion_settings", settings)
    monkeypatch.setattr(ingestion, "credential", lambda: object())

    class FakeEmbeddings:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(
                data=[
                    SimpleNamespace(embedding=[0.1, 0.2]),
                    SimpleNamespace(embedding=[0.3, 0.4]),
                ]
            )

    class FakeAzureOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.embeddings = FakeEmbeddings()

    deleted: list[list[dict[str, Any]]] = []

    class FakeSearch:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def upload_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]

        def merge_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            return [SimpleNamespace(key=document["id"], succeeded=False) for document in documents]

        def delete_documents(self, *, documents: list[dict[str, Any]]) -> list[SimpleNamespace]:
            deleted.append(documents)
            raise HttpResponseError(message="cleanup failed")

    monkeypatch.setattr(ingestion, "AzureOpenAI", FakeAzureOpenAI)
    monkeypatch.setattr(
        ingestion,
        "get_bearer_token_provider",
        lambda *_args, **_kwargs: lambda: "token",
    )
    monkeypatch.setattr(ingestion, "SearchClient", FakeSearch)
    monkeypatch.setattr(ingestion, "_update_library", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ingestion, "_update_run", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="cleanup failed after ingestion failure") as excinfo:
        ingestion.index_extracted_source(
            _payload(
                extracted_manifest_uri=(
                    "https://storage.example.test/sources/demo/demo-project/"
                    "source-abc123abc123/extracted-manifest.json"
                )
            )
        )

    assert isinstance(excinfo.value.__cause__, HttpResponseError)
    assert deleted == [[{"id": "chunk-1"}, {"id": "chunk-2"}]]


def test_run_and_library_state_wrappers_delegate_to_update_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls: list[dict[str, Any]] = []
    library_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        ingestion,
        "_update_run",
        lambda _payload, **state: run_calls.append(state),
    )
    monkeypatch.setattr(
        ingestion,
        "_update_library",
        lambda _payload, **state: library_calls.append(state),
    )

    ingestion.set_run_state(
        _payload(),
        status="running",
        progress=42,
        current_stage="Chunk, embed & index",
        completed=True,
    )
    ingestion.set_library_state(_payload(), status="ready", evidence_count=7)

    assert run_calls == [
        {
            "status": "running",
            "progress": 42,
            "current_stage": "Chunk, embed & index",
            "completed": True,
        }
    ]
    assert library_calls == [{"status": "ready", "evidence_count": 7}]
