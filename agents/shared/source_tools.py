from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from contextvars import ContextVar
from typing import Any

from agent_framework import (
    FunctionInvocationContext,
    FunctionMiddleware,
    MiddlewareTermination,
)
from pydantic import BaseModel, ConfigDict

from .session_files import SessionFile, read_session_file_ids


class RetrievedSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    connector_id: str
    source_uri: str | None = None
    title: str | None = None


_ALLOWED_CONNECTOR_IDS: ContextVar[frozenset[str]] = ContextVar(
    "research_allowed_connector_ids",
    default=frozenset(),
)
_SESSION_FILE_PATHS: ContextVar[frozenset[str]] = ContextVar(
    "research_source_tool_session_file_paths",
    default=frozenset(),
)
_RETRIEVED_SOURCES: ContextVar[dict[str, RetrievedSource] | None] = ContextVar(
    "research_retrieved_sources",
    default=None,
)


def bind_source_tools(
    connector_ids: tuple[str, ...],
    session_files: tuple[SessionFile, ...],
) -> None:
    _ALLOWED_CONNECTOR_IDS.set(frozenset(connector_ids))
    _SESSION_FILE_PATHS.set(frozenset(item.path for item in session_files))
    _RETRIEVED_SOURCES.set({})


def retrieved_sources() -> tuple[RetrievedSource, ...]:
    return tuple((_RETRIEVED_SOURCES.get() or {}).values())


def _connector_id(name: str) -> str | None:
    connector_id, separator, operation = name.partition("___")
    return connector_id if separator and connector_id and operation else None


def _serializable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _serializable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_serializable(item) for item in value]
    return value


def _strings(value: Any) -> tuple[str, ...]:
    serialized = _serializable(value)
    if isinstance(serialized, str):
        return (serialized,)
    if isinstance(serialized, Mapping):
        return tuple(text for item in serialized.values() for text in _strings(item))
    if isinstance(serialized, Sequence) and not isinstance(serialized, (str, bytes, bytearray)):
        return tuple(text for item in serialized for text in _strings(item))
    return ()


def _dict_payloads(value: Any) -> tuple[dict[str, Any], ...]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if item is None:
            return
        if not isinstance(item, (str, bytes, bytearray, int, float, bool)):
            if id(item) in seen:
                return
            seen.add(id(item))
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="json"))
            return
        if isinstance(item, Mapping):
            payload = {str(key): nested for key, nested in item.items()}
            found.append(payload)
            for nested in payload.values():
                visit(nested)
            return
        if isinstance(item, str):
            with suppress(json.JSONDecodeError, TypeError):
                visit(json.loads(item))
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested)
            return
        for attribute in ("content", "structured_content", "output", "result", "text"):
            nested = getattr(item, attribute, None)
            if nested is not None and nested is not item:
                visit(nested)

    visit(value)
    return tuple(found)


def _record_reference(connector_id: str, record: dict[str, Any], fallback_uri: Any) -> RetrievedSource:
    canonical_record = {key: value for key, value in record.items() if key != "evidence_id"}
    canonical = json.dumps(canonical_record, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    source_uri = record.get("canonical_url") or record.get("url") or fallback_uri
    title = record.get("title") or record.get("opportunity_number") or record.get("id")
    return RetrievedSource(
        evidence_id=f"connector:{connector_id}:{digest}",
        connector_id=connector_id,
        source_uri=source_uri if isinstance(source_uri, str) else None,
        title=str(title)[:512] if title is not None else None,
    )


def _capture_connector_records(connector_id: str, result: Any) -> None:
    ledger = _RETRIEVED_SOURCES.get()
    if ledger is None:
        ledger = {}
        _RETRIEVED_SOURCES.set(ledger)
    for payload in _dict_payloads(result):
        if payload.get("source") != connector_id:
            continue
        records = payload.get("records")
        if not isinstance(records, list):
            continue
        fallback_uri = payload.get("retrieved_from")
        for record in records:
            if not isinstance(record, dict):
                continue
            reference = _record_reference(connector_id, record, fallback_uri)
            ledger[reference.evidence_id] = reference


def _annotate_connector_result(connector_id: str, result: Any) -> Any:
    if not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return result
    if not isinstance(payload, dict) or payload.get("source") != connector_id:
        return result
    records = payload.get("records")
    if not isinstance(records, list):
        return result
    fallback_uri = payload.get("retrieved_from")
    for record in records:
        if isinstance(record, dict):
            record["evidence_id"] = _record_reference(
                connector_id,
                record,
                fallback_uri,
            ).evidence_id
    return json.dumps(payload, separators=(",", ":"), default=str)


class SourceToolBoundary(FunctionMiddleware):
    """Enforce request connector policy and prevent file-to-tool data flow."""

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        name = context.function.name
        connector_id = _connector_id(name)
        external = connector_id is not None or name == "web_search"
        if not external:
            await call_next()
            return

        if connector_id is not None and connector_id not in _ALLOWED_CONNECTOR_IDS.get():
            context.result = json.dumps(
                {"status": "denied", "reason": "Connector is not enabled for this request."},
                separators=(",", ":"),
            )
            raise MiddlewareTermination()
        if read_session_file_ids():
            context.result = json.dumps(
                {
                    "status": "denied",
                    "reason": "External retrieval must finish before attached files are read.",
                },
                separators=(",", ":"),
            )
            raise MiddlewareTermination()
        arguments = _serializable(context.arguments)
        argument_text = "\n".join(_strings(arguments)).casefold()
        if any(path.casefold() in argument_text for path in _SESSION_FILE_PATHS.get()):
            context.result = json.dumps(
                {"status": "denied", "reason": "Attachment paths cannot be sent to external tools."},
                separators=(",", ":"),
            )
            raise MiddlewareTermination()

        await call_next()
        if connector_id is not None:
            _capture_connector_records(connector_id, context.result)
            context.result = _annotate_connector_result(connector_id, context.result)