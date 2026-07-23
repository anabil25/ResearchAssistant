from __future__ import annotations

import pytest
from research_assistant_core.chunking import TextChunk, chunk_text


def test_structural_chunking_is_bounded_deterministic_and_complete() -> None:
    text = "\n\n".join(
        [
            "# Protocol",
            "Eligibility and scope are defined before retrieval.",
            "Methods and limitations must be extractable. " * 30,
            "Every claim resolves to a stored passage.",
        ]
    )

    first = chunk_text(text, max_chars=300, overlap_chars=30)
    second = chunk_text(text, max_chars=300, overlap_chars=30)

    assert first == second
    assert len(first) > 2
    assert [chunk.index for chunk in first] == list(range(len(first)))
    assert all(0 < len(chunk.content) <= 300 for chunk in first)
    assert "# Protocol" in first[0].content
    assert "Every claim resolves" in first[-1].content


def test_overlap_never_drops_oversized_unit_tail() -> None:
    text = " ".join(["evidence"] * 90 + ["MISSING_MARKER"])

    chunks = chunk_text(text, max_chars=240, overlap_chars=80)

    assert "MISSING_MARKER" in "\n".join(chunk.content for chunk in chunks)


def test_empty_content_produces_no_chunks() -> None:
    assert chunk_text("\r\n\r\n") == []


def test_single_paragraph_produces_one_complete_chunk() -> None:
    assert chunk_text("Stored evidence.") == [
        TextChunk(index=0, content="Stored evidence.")
    ]


@pytest.mark.parametrize(
    ("max_chars", "overlap_chars"),
    [(100, 10), (300, -1), (300, 300)],
)
def test_structural_chunking_rejects_invalid_boundaries(
    max_chars: int,
    overlap_chars: int,
) -> None:
    with pytest.raises(ValueError):
        chunk_text(
            "bounded content",
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
