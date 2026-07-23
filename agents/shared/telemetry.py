from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

_SECRET_PARTS = ("token", "secret", "password", "authorization", "api_key", "content", "query")


def _redact_value(value: Any) -> str:
    encoded = str(value).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return f"[REDACTED:{digest}]"


def redact_attributes(
    attributes: Mapping[str, Any],
    *,
    extra_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in attributes.items():
        normalized = key.lower()
        if key in extra_fields or any(part in normalized for part in _SECRET_PARTS):
            redacted[key] = _redact_value(value)
        elif isinstance(value, (str, bool, int, float)) or value is None:
            redacted[key] = value
        else:
            redacted[key] = type(value).__name__
    return redacted


def telemetry_identity_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GovernanceAuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    outcome: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    agent_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tenant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation_id: str | None = Field(default=None, min_length=1, max_length=256)
    approval_decision_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    idempotency_key_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GovernanceAuditSink(Protocol):
    def emit(self, event: GovernanceAuditEvent) -> None: ...


class OpenTelemetryGovernanceAuditSink(GovernanceAuditSink):
    def __init__(self) -> None:
        self._tracer = trace.get_tracer("research_assistant.governance")

    def emit(self, event: GovernanceAuditEvent) -> None:
        attributes = redact_attributes(
            event.model_dump(mode="json", exclude={"event_name", "occurred_at"}),
        )
        with self._tracer.start_as_current_span(event.event_name) as span:
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(f"governance.{key}", value)
            span.set_attribute(
                "governance.occurred_at",
                event.occurred_at.isoformat(),
            )
