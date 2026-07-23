from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

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
