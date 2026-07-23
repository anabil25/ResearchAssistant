from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    content: str


def chunk_text(
    text: str,
    *,
    max_chars: int = 1800,
    overlap_chars: int = 180,
) -> list[TextChunk]:
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be non-negative and less than half of max_chars")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n{2,}", normalized) if paragraph.strip()]
    payload_limit = max_chars - overlap_chars - 2
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= payload_limit:
            units.append(paragraph)
            continue
        remaining = paragraph
        while remaining:
            boundary = remaining.rfind(" ", 0, payload_limit + 1)
            if boundary < payload_limit // 2:
                boundary = payload_limit
            units.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()

    base_chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if len(candidate) <= payload_limit:
            current = candidate
            continue
        if current:
            base_chunks.append(current)
        current = unit
    if current:
        base_chunks.append(current)

    chunks: list[TextChunk] = []
    for index, base in enumerate(base_chunks):
        if index == 0 or overlap_chars == 0:
            content = base
        else:
            overlap = base_chunks[index - 1][-overlap_chars:].lstrip()
            content = f"{overlap}\n\n{base}".strip()
        chunks.append(TextChunk(index=index, content=content))
    return chunks
