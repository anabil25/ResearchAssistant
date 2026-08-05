from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Sensitivity


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
    is_durable: bool

    async def load(self, tenant_id: str, session_id: str) -> ConversationRecord | None: ...

    async def save(self, record: ConversationRecord) -> None: ...


class LongTermMemoryStore(Protocol):
    is_durable: bool

    async def remember(
        self,
        record: MemoryRecord,
        *,
        allowed_sensitivities: tuple[Sensitivity, ...],
    ) -> None: ...

    async def recall(
        self,
        tenant_id: str,
        principal_id: str,
    ) -> tuple[MemoryRecord, ...]: ...


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
