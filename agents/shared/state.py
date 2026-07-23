from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Sensitivity
from .errors import IsolationError


class ConversationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    session_id: str
    state: dict[str, Any] = {}
    service_session_id: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    principal_id: str
    memory_id: str
    content: str
    sensitivity: Sensitivity
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConversationStore(Protocol):
    async def load(self, tenant_id: str, session_id: str) -> ConversationRecord | None: ...

    async def save(self, record: ConversationRecord) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ConversationRecord] = {}
        self._session_tenants: dict[str, str] = {}

    async def load(self, tenant_id: str, session_id: str) -> ConversationRecord | None:
        owner = self._session_tenants.get(session_id)
        if owner is not None and owner != tenant_id:
            raise IsolationError("Session belongs to another tenant")
        return self._records.get((tenant_id, session_id))

    async def save(self, record: ConversationRecord) -> None:
        owner = self._session_tenants.get(record.session_id)
        if owner is not None and owner != record.tenant_id:
            raise IsolationError("Cannot overwrite another tenant's session")
        self._session_tenants[record.session_id] = record.tenant_id
        self._records[(record.tenant_id, record.session_id)] = record


class InMemoryLongTermMemory:
    def __init__(self, *, max_records: int = 100) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._max_records = max_records
        self._records: dict[tuple[str, str], list[MemoryRecord]] = {}

    async def remember(
        self,
        record: MemoryRecord,
        *,
        allowed_sensitivities: tuple[Sensitivity, ...],
    ) -> None:
        if record.sensitivity not in allowed_sensitivities:
            raise IsolationError("Memory policy rejects this sensitivity")
        key = (record.tenant_id, record.principal_id)
        items = self._records.setdefault(key, [])
        items.append(record)
        del items[: -self._max_records]

    async def recall(self, tenant_id: str, principal_id: str) -> tuple[MemoryRecord, ...]:
        return tuple(self._records.get((tenant_id, principal_id), ()))


def to_agent_session(record: ConversationRecord) -> Any:
    from agent_framework import AgentSession

    session = AgentSession(
        session_id=record.session_id,
        service_session_id=record.service_session_id,
    )
    session.state.update(record.state)
    return session


def from_agent_session(tenant_id: str, session: Any) -> ConversationRecord:
    return ConversationRecord(
        tenant_id=tenant_id,
        session_id=session.session_id,
        service_session_id=session.service_session_id,
        state=dict(session.state),
    )
