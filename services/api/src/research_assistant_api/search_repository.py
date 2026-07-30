from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from azure.core.credentials import TokenCredential
from azure.search.documents import SearchClient
from research_assistant_core.azure_auth import azure_credential
from research_assistant_core.fixtures import SAMPLE_EVIDENCE
from research_assistant_core.models import EvidenceChunk, SourceKind
from research_assistant_core.repositories import (
    EvidenceRepository,
    InMemoryEvidenceRepository,
)
from research_assistant_core.service import ResearchService

from research_assistant_api.config import Settings

_SELECT_FIELDS = [
    "id",
    "source_id",
    "source_kind",
    "tenant_ids",
    "project_ids",
    "group_ids",
    "access",
    "year",
    "provider",
    "ingestion_status",
    "safety_status",
    "title",
    "section",
    "page_start",
    "content",
    "checksum",
    "license",
    "version",
]


def _odata_literal(value: str) -> str:
    return value.replace("'", "''")


class AzureSearchEvidenceRepository:
    def __init__(
        self,
        endpoint: str,
        index_name: str,
        credential: TokenCredential,
        *,
        client: Any | None = None,
    ) -> None:
        self._client = client or SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=credential,
        )

    @staticmethod
    def _filter(
        tenant_id: str,
        project_id: str,
        group_ids: Sequence[str],
        kinds: Sequence[SourceKind] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        sources: Sequence[str] | None = None,
        *,
        chunk_id: str | None = None,
    ) -> str:
        clauses = [
            f"tenant_ids/any(tenant: tenant eq '{_odata_literal(tenant_id)}')"
        ]
        project_access = (
            "project_ids/any(project: "
            f"project eq '{_odata_literal(project_id)}')"
        )
        if group_ids:
            group_literals = ",".join(_odata_literal(group_id) for group_id in group_ids)
            restricted_access = (
                "group_ids/any(group: "
                f"search.in(group, '{group_literals}', ','))"
            )
        else:
            restricted_access = "false"
        clauses.append(
            "(access eq 'public' "
            f"or (access eq 'internal' and {project_access}) "
            f"or (access eq 'restricted' and {project_access} "
            f"and {restricted_access}))"
        )
        clauses.append("ingestion_status eq 'ready'")
        clauses.append("safety_status eq 'safe'")
        if kinds:
            kind_filter = " or ".join(f"source_kind eq '{_odata_literal(kind.value)}'" for kind in kinds)
            clauses.append(f"({kind_filter})")
        if year_from is not None:
            clauses.append(f"year ge {year_from}")
        if year_to is not None:
            clauses.append(f"year le {year_to}")
        if sources is not None:
            if not sources:
                clauses.append("false")
            else:
                source_literals = ",".join(_odata_literal(source) for source in sources)
                clauses.append(f"search.in(provider, '{source_literals}', ',')")
        if chunk_id:
            clauses.append(f"id eq '{_odata_literal(chunk_id)}'")
        return " and ".join(clauses)

    @staticmethod
    def _chunk(document: dict[str, Any]) -> EvidenceChunk:
        return EvidenceChunk(
            id=str(document["id"]),
            source_id=str(document["source_id"]),
            source_kind=SourceKind(str(document["source_kind"])),
            title=str(document["title"]),
            content=str(document["content"]),
            section=str(document["section"]),
            page_start=(int(document["page_start"]) if document.get("page_start") is not None else None),
            checksum=str(document["checksum"]),
            license=str(document.get("license") or "License not supplied"),
            version=str(document.get("version") or "1"),
            allowed_tenants=[str(value) for value in document.get("tenant_ids", [])],
            allowed_projects=[str(value) for value in document.get("project_ids", [])],
            allowed_groups=[str(value) for value in document.get("group_ids", [])],
            access=str(document.get("access") or "internal"),
            year=(int(document["year"]) if document.get("year") is not None else None),
            metadata={"provider": str(document.get("provider") or "")},
        )

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        project_id: str,
        group_ids: Sequence[str],
        kinds: Sequence[SourceKind] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        sources: Sequence[str] | None = None,
        limit: int = 8,
    ) -> list[EvidenceChunk]:
        results = self._client.search(
            search_text=query,
            filter=self._filter(
                tenant_id,
                project_id,
                group_ids,
                kinds,
                year_from=year_from,
                year_to=year_to,
                sources=sources,
            ),
            top=limit,
            select=_SELECT_FIELDS,
            query_type="semantic",
            semantic_configuration_name="research-semantic",
        )
        return [self._chunk(dict(result)) for result in results]

    def get(
        self,
        chunk_id: str,
        *,
        tenant_id: str,
        project_id: str,
        group_ids: Sequence[str],
    ) -> EvidenceChunk | None:
        results = self._client.search(
            search_text="*",
            filter=self._filter(
                tenant_id,
                project_id,
                group_ids,
                chunk_id=chunk_id,
            ),
            top=1,
            select=_SELECT_FIELDS,
        )
        document = next(iter(results), None)
        return self._chunk(dict(document)) if document is not None else None


def build_research_service(settings: Settings) -> ResearchService:
    repository: EvidenceRepository
    if settings.search_endpoint:
        repository = AzureSearchEvidenceRepository(
            settings.search_endpoint,
            settings.search_index_name,
            azure_credential(settings.managed_identity_client_id),
        )
    else:
        repository = InMemoryEvidenceRepository(SAMPLE_EVIDENCE)
    return ResearchService(
        repository,
        mode=settings.execution_mode,
        model_deployment=(
            "foundry-hosted-specialists" if settings.execution_mode == "hosted" else "deterministic-fixture-model"
        ),
    )
