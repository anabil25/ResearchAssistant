from __future__ import annotations

from typing import Any

import research_assistant_api.search_repository as search_repository
from azure.core.credentials import AccessToken, TokenCredential
from research_assistant_api.config import Settings
from research_assistant_api.search_repository import AzureSearchEvidenceRepository
from research_assistant_core.models import EvidenceChunk, SourceKind
from research_assistant_core.repositories import InMemoryEvidenceRepository


class FakeCredential(TokenCredential):
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        return AccessToken("fake", 4_102_444_800)


class FakeSearchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.document = {
            "id": "paper-rag-methods",
            "source_id": "paper-rag",
            "source_kind": "paper",
            "tenant_ids": ["demo"],
            "project_ids": ["demo-project"],
            "group_ids": ["researchers"],
            "access": "internal",
            "year": 2025,
            "provider": "PubMed",
            "ingestion_status": "ready",
            "safety_status": "safe",
            "title": "Auditable retrieval",
            "section": "Methods",
            "page_start": 4,
            "content": "Hybrid retrieval was evaluated on a synthetic corpus.",
            "checksum": "sha256:test",
            "license": "CC BY 4.0",
            "version": "1.0",
        }

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        if any(
            value in kwargs["filter"]
            for value in (
                "other-tenant",
                "other-project",
                "year ge 2099",
                "Europe PMC",
            )
        ):
            return []
        return [self.document]


class RecordingRepository:
    def __init__(self, endpoint: str, index_name: str, credential: object) -> None:
        self.endpoint = endpoint
        self.index_name = index_name
        self.credential = credential


def test_search_repository_applies_server_tenant_and_kind_filter() -> None:
    client = FakeSearchClient()
    repository = AzureSearchEvidenceRepository(
        "https://search.example.test",
        "evidence",
        FakeCredential(),
        client=client,
    )

    chunks = repository.search(
        "auditable retrieval",
        tenant_id="demo",
        project_id="demo-project",
        group_ids=["researchers"],
        kinds=[SourceKind.PAPER],
        limit=3,
    )

    assert len(chunks) == 1
    assert chunks[0].source_id == "paper-rag"
    assert chunks[0].allowed_tenants == ["demo"]
    call = client.calls[0]
    assert "tenant_ids/any(tenant: tenant eq 'demo')" in call["filter"]
    assert "project_ids/any(project: project eq 'demo-project')" in call["filter"]
    assert "search.in(group, 'researchers', ',')" in call["filter"]
    assert "ingestion_status eq 'ready'" in call["filter"]
    assert "safety_status eq 'safe'" in call["filter"]
    assert "source_kind eq 'paper'" in call["filter"]
    assert call["query_type"] == "semantic"
    assert call["top"] == 3


def test_search_repository_get_cannot_cross_tenant_boundary() -> None:
    client = FakeSearchClient()
    repository = AzureSearchEvidenceRepository(
        "https://search.example.test",
        "evidence",
        FakeCredential(),
        client=client,
    )

    allowed = repository.get(
        "paper-rag-methods",
        tenant_id="demo",
        project_id="demo-project",
        group_ids=["researchers"],
    )
    blocked = repository.get(
        "paper-rag-methods",
        tenant_id="other-tenant",
        project_id="demo-project",
        group_ids=["researchers"],
    )

    assert allowed is not None
    assert blocked is None
    assert client.calls[0]["top"] == 1
    assert client.calls[0]["select"]
    assert "id eq 'paper-rag-methods'" in client.calls[0]["filter"]


def test_search_repository_escapes_odata_tenant_literals() -> None:
    client = FakeSearchClient()
    repository = AzureSearchEvidenceRepository(
        "https://search.example.test",
        "evidence",
        FakeCredential(),
        client=client,
    )

    repository.search(
        "test",
        tenant_id="tenant'oops",
        project_id="project'oops",
        group_ids=["group'oops"],
    )

    assert "tenant''oops" in client.calls[0]["filter"]
    assert "project''oops" in client.calls[0]["filter"]
    assert "group''oops" in client.calls[0]["filter"]


def test_search_repository_blocks_wrong_project_but_internal_is_project_scoped() -> None:
    client = FakeSearchClient()
    repository = AzureSearchEvidenceRepository(
        "https://search.example.test",
        "evidence",
        FakeCredential(),
        client=client,
    )

    wrong_project = repository.search(
        "test",
        tenant_id="demo",
        project_id="other-project",
        group_ids=["researchers"],
    )
    wrong_group = repository.search(
        "test",
        tenant_id="demo",
        project_id="demo-project",
        group_ids=["outsiders"],
    )

    assert wrong_project == []
    assert len(wrong_group) == 1
    assert "access eq 'internal'" in client.calls[1]["filter"]
    assert "access eq 'restricted'" in client.calls[1]["filter"]
    assert "search.in(group, 'outsiders', ',')" in client.calls[1]["filter"]


def test_in_memory_repository_requires_groups_only_for_restricted_content() -> None:
    def policy_chunk(chunk_id: str, access: str) -> EvidenceChunk:
        return EvidenceChunk(
            id=chunk_id,
            source_id="source",
            source_kind=SourceKind.POLICY,
            title="Policy",
            content="Policy evidence",
            section="Scope",
            checksum="sha256:test",
            allowed_tenants=["tenant"],
            allowed_projects=["project"],
            allowed_groups=["reviewers"],
            access=access,
        )

    repository = InMemoryEvidenceRepository(
        [
            policy_chunk("internal", "internal"),
            policy_chunk("restricted", "restricted"),
        ]
    )

    results = repository.search(
        "policy evidence",
        tenant_id="tenant",
        project_id="project",
        group_ids=[],
    )

    assert [item.id for item in results] == ["internal"]


def test_in_memory_repository_enforces_every_search_and_get_boundary() -> None:
    def chunk(
        chunk_id: str,
        *,
        tenant: str = "tenant",
        project: str = "project",
        group: str = "reviewers",
        access: str = "internal",
        year: int | None = 2026,
        provider: str = "PubMed",
    ) -> EvidenceChunk:
        return EvidenceChunk(
            id=chunk_id,
            source_id=f"source-{chunk_id}",
            source_kind=SourceKind.POLICY,
            title=f"Policy {chunk_id}",
            content="Policy evidence for retention",
            section="Scope",
            checksum=f"sha256:{chunk_id}",
            allowed_tenants=[tenant],
            allowed_projects=[project],
            allowed_groups=[group],
            access=access,
            year=year,
            metadata={"provider": provider},
        )

    repository = InMemoryEvidenceRepository(
        [
            chunk("allowed"),
            chunk("wrong-tenant", tenant="other"),
            chunk("wrong-project", project="other"),
            chunk("wrong-group", access="restricted", group="other"),
            chunk("wrong-year", year=None),
            chunk("wrong-provider", provider="Europe PMC"),
        ]
    )

    results = repository.search(
        "retention",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
        kinds=[SourceKind.POLICY],
        year_from=2025,
        year_to=2027,
        sources=["pubmed"],
    )

    assert [item.id for item in results] == ["allowed"]
    assert repository.get(
        "missing",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
    ) is None
    assert repository.get(
        "wrong-tenant",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
    ) is None
    assert repository.get(
        "wrong-project",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
    ) is None
    assert repository.get(
        "wrong-group",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
    ) is None
    assert repository.get(
        "allowed",
        tenant_id="tenant",
        project_id="project",
        group_ids=["reviewers"],
    ) is not None


def test_search_repository_applies_protocol_year_and_provider_filters() -> None:
    client = FakeSearchClient()
    repository = AzureSearchEvidenceRepository(
        "https://search.example.test",
        "evidence",
        FakeCredential(),
        client=client,
    )

    results = repository.search(
        "test",
        tenant_id="demo",
        project_id="demo-project",
        group_ids=["researchers"],
        year_from=2099,
        year_to=2100,
        sources=["Europe PMC"],
    )

    assert results == []
    filter_text = client.calls[0]["filter"]
    assert "year ge 2099" in filter_text
    assert "year le 2100" in filter_text
    assert "search.in(provider, 'Europe PMC', ',')" in filter_text


def test_filter_uses_false_clauses_for_missing_groups_and_empty_sources() -> None:
    filter_text = AzureSearchEvidenceRepository._filter(
        "tenant",
        "project",
        [],
        [SourceKind.POLICY],
        sources=[],
        chunk_id="chunk-1",
    )

    assert "(access eq 'public'" in filter_text
    assert "group_ids/any" not in filter_text
    assert "and false)" in filter_text
    assert "source_kind eq 'policy'" in filter_text
    assert filter_text.count("false") == 2
    assert "id eq 'chunk-1'" in filter_text


def test_chunk_supplies_defaults_for_optional_fields() -> None:
    chunk = AzureSearchEvidenceRepository._chunk(
        {
            "id": "chunk-1",
            "source_id": "source-1",
            "source_kind": "policy",
            "title": "Policy",
            "content": "Evidence",
            "section": "Scope",
            "checksum": "sha256:abc",
        }
    )

    assert chunk.page_start is None
    assert chunk.year is None
    assert chunk.license == "License not supplied"
    assert chunk.version == "1"
    assert chunk.allowed_tenants == []
    assert chunk.allowed_projects == []
    assert chunk.allowed_groups == []
    assert chunk.access == "internal"
    assert chunk.metadata == {"provider": ""}


def test_build_research_service_uses_azure_search_when_configured(monkeypatch: Any) -> None:
    managed_credential = object()
    requested: list[Any] = []

    def _credential(client_id: Any = None) -> object:
        requested.append(client_id)
        return managed_credential

    monkeypatch.setattr(search_repository, "azure_credential", _credential)
    monkeypatch.setattr(search_repository, "AzureSearchEvidenceRepository", RecordingRepository)

    service = search_repository.build_research_service(
        Settings(
            search_endpoint="https://search.example.test/",
            search_index_name="evidence-index",
            managed_identity_client_id="mi-client",
            execution_mode="hosted",
        )
    )

    assert isinstance(service.repository, RecordingRepository)
    assert service.repository.endpoint == "https://search.example.test"
    assert service.repository.index_name == "evidence-index"
    assert service.repository.credential is managed_credential
    assert requested == ["mi-client"]
    assert service.mode == "hosted"
    assert service.model_deployment == "foundry-hosted-specialists"


def test_build_research_service_uses_fixture_repository_when_search_is_disabled(
    monkeypatch: Any,
) -> None:
    # AZURE_SEARCH_ENDPOINT can be left set in the real process environment by
    # unrelated tests (e.g. provisioning tests that sync azd outputs directly
    # into os.environ). Force a clean, deterministic settings snapshot here so
    # this test's outcome never depends on suite ordering.
    monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
    service = search_repository.build_research_service(
        Settings(execution_mode="mock", search_endpoint=None)
    )

    assert isinstance(service.repository, InMemoryEvidenceRepository)
    assert service.mode == "mock"
    assert service.model_deployment == "deterministic-fixture-model"
