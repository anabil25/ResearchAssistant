from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from agent_framework import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SESSION_FILE_BYTES = 20_000_000
MAX_SESSION_FILE_TEXT = 160_000
_TEXT_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)


class SessionFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=6, max_length=125)
    path: str = Field(min_length=1, max_length=120)
    content_type: str = Field(min_length=1, max_length=160)
    size_bytes: int = Field(ge=1, le=MAX_SESSION_FILE_BYTES)

    @model_validator(mode="after")
    def safe_request_bound_path(self) -> SessionFile:
        if (
            self.path in {".", ".."}
            or Path(self.path).name != self.path
            or "/" in self.path
            or "\\" in self.path
            or self.evidence_id != f"file:{self.path}"
        ):
            raise ValueError("session files require one safe relative path and matching evidence ID")
        if self.content_type not in _TEXT_CONTENT_TYPES | {"application/pdf"}:
            raise ValueError("session file content type is not supported")
        return self


class SessionFileRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    path: str
    content_type: str
    text: str
    truncated: bool = False


_SESSION_FILES: ContextVar[dict[str, SessionFile] | None] = ContextVar(
    "research_session_files",
    default=None,
)
_READ_SESSION_FILE_IDS: ContextVar[set[str] | None] = ContextVar(
    "research_read_session_file_ids",
    default=None,
)


def bind_session_files(files: tuple[SessionFile, ...]) -> None:
    _SESSION_FILES.set({item.path: item for item in files})
    _READ_SESSION_FILE_IDS.set(set())


def read_session_file_ids() -> frozenset[str]:
    return frozenset(_READ_SESSION_FILE_IDS.get() or ())


def _bounded_text(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_SESSION_FILE_TEXT:
        return value, False
    return value[:MAX_SESSION_FILE_TEXT], True


def _safe_file_path(root: Path, item: SessionFile) -> Path:
    resolved_root = root.resolve()
    candidate = resolved_root / item.path
    if candidate.is_symlink():
        raise ValueError("symbolic links are not accepted")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("session file path escapes the session home") from exc
    if not resolved.is_file():
        raise ValueError("session file was not found")
    observed_size = resolved.stat().st_size
    if observed_size <= 0 or observed_size > MAX_SESSION_FILE_BYTES:
        raise ValueError("session file size is outside the accepted boundary")
    return resolved


def read_session_file(item: SessionFile, *, root: Path | None = None) -> SessionFileRead:
    resolved = _safe_file_path(root or Path.home(), item)
    if item.content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(resolved)
        source_text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
        if not source_text:
            raise ValueError("PDF contains no extractable text")
    else:
        source_text = resolved.read_text(encoding="utf-8")
    text, truncated = _bounded_text(source_text)
    return SessionFileRead(
        evidence_id=item.evidence_id,
        path=item.path,
        content_type=item.content_type,
        text=text,
        truncated=truncated,
    )


def build_session_file_reader() -> Any:
    @tool(
        name="read_session_file",
        description=(
            "Read one file explicitly attached to the current session. Pass an exact path "
            "from session_files. Supports PDF, text, Markdown, CSV, and JSON."
        ),
        approval_mode="never_require",
    )
    def read_current_session_file(path: str) -> str:
        item = (_SESSION_FILES.get() or {}).get(path)
        if item is None:
            return json.dumps(
                {"error": "The file is not attached to the current request.", "path": path},
                separators=(",", ":"),
            )
        try:
            result = read_session_file(item)
        except (OSError, UnicodeError, ValueError) as exc:
            return json.dumps(
                {"error": str(exc), "evidence_id": item.evidence_id, "path": item.path},
                separators=(",", ":"),
            )
        read_ids = _READ_SESSION_FILE_IDS.get()
        if read_ids is None:
            read_ids = set()
            _READ_SESSION_FILE_IDS.set(read_ids)
        read_ids.add(item.evidence_id)
        return result.model_dump_json()

    return read_current_session_file