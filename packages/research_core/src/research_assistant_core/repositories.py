from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from rapidfuzz.fuzz import token_set_ratio

from research_assistant_core.models import EvidenceChunk, SourceKind

_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{1,}")


class EvidenceRepository(Protocol):
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
    ) -> list[EvidenceChunk]: ...

    def get(
        self,
        chunk_id: str,
        *,
        tenant_id: str,
        project_id: str,
        group_ids: Sequence[str],
    ) -> EvidenceChunk | None: ...


class InMemoryEvidenceRepository:
    def __init__(self, chunks: Iterable[EvidenceChunk]) -> None:
        self._chunks = tuple(chunks)
        self._by_id = {chunk.id: chunk for chunk in self._chunks}

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
        allowed_kinds = set(kinds) if kinds else None
        query_tokens = set(_TOKEN_PATTERN.findall(query.lower()))
        scored: list[tuple[float, EvidenceChunk]] = []

        for chunk in self._chunks:
            if tenant_id not in chunk.allowed_tenants:
                continue
            if (
                chunk.access in {"internal", "restricted"}
                and project_id not in chunk.allowed_projects
            ):
                continue
            if (
                chunk.access == "restricted"
                and chunk.allowed_groups
                and not set(group_ids).intersection(chunk.allowed_groups)
            ):
                continue
            if allowed_kinds is not None and chunk.source_kind not in allowed_kinds:
                continue
            if year_from is not None and (chunk.year is None or chunk.year < year_from):
                continue
            if year_to is not None and (chunk.year is None or chunk.year > year_to):
                continue
            if sources is not None:
                provider = str(chunk.metadata.get("provider", ""))
                if provider.casefold() not in {source.casefold() for source in sources}:
                    continue

            haystack = f"{chunk.title} {chunk.section} {chunk.content}".lower()
            overlap = len(query_tokens.intersection(_TOKEN_PATTERN.findall(haystack)))
            fuzzy = token_set_ratio(query, haystack) / 100
            score = overlap * 4 + fuzzy
            scored.append((score, chunk))

        scored.sort(key=lambda item: (-item[0], item[1].title, item[1].id))
        return [chunk for _, chunk in scored[:limit]]

    def get(
        self,
        chunk_id: str,
        *,
        tenant_id: str,
        project_id: str,
        group_ids: Sequence[str],
    ) -> EvidenceChunk | None:
        chunk = self._by_id.get(chunk_id)
        if chunk is None or tenant_id not in chunk.allowed_tenants:
            return None
        if (
            chunk.access in {"internal", "restricted"}
            and project_id not in chunk.allowed_projects
        ):
            return None
        if (
            chunk.access == "restricted"
            and chunk.allowed_groups
            and not set(group_ids).intersection(chunk.allowed_groups)
        ):
            return None
        return chunk
