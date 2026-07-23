# mypy: disable-error-code=import-untyped
"""Direct unit tests for ``schema_ref_resolver.py``.

``InlineSchemaRefResolver`` is the fail-closed default resolver used by
``ReleaseService.cut_version`` to independently verify
``AgentManifest.input_schema_ref``/``output_schema_ref`` rather than
trusting a caller-declared digest at face value.
"""

from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.models import SchemaRef
from research_assistant_api.agent_studio.schema_ref_resolver import (
    InlineSchemaRefResolver,
    SchemaResolutionError,
    compute_schema_digest,
)


def test_compute_schema_digest_is_deterministic_and_key_order_independent() -> None:
    schema_a: dict[str, object] = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_b: dict[str, object] = {"properties": {"a": {"type": "string"}}, "type": "object"}

    assert compute_schema_digest(schema_a) == compute_schema_digest(schema_b)
    assert compute_schema_digest(schema_a).startswith("sha256:")
    assert compute_schema_digest({"type": "string"}) != compute_schema_digest(schema_a)


def test_inline_resolver_returns_schema_when_digest_matches() -> None:
    schema: dict[str, object] = {"type": "object"}
    ref = SchemaRef(ref="schema://agent-output", digest=compute_schema_digest(schema), inline_schema=schema)

    resolved = InlineSchemaRefResolver().resolve_and_verify(ref)

    assert resolved == schema


def test_inline_resolver_fails_closed_when_no_inline_schema_is_present() -> None:
    ref = SchemaRef(ref="schema://agent-output", digest="sha256:whatever", inline_schema=None)

    with pytest.raises(SchemaResolutionError, match="has no inline schema to verify"):
        InlineSchemaRefResolver().resolve_and_verify(ref)


def test_inline_resolver_rejects_declared_digest_that_does_not_match_computed_digest() -> None:
    schema: dict[str, object] = {"type": "object"}
    ref = SchemaRef(ref="schema://agent-output", digest="sha256:not-the-real-digest", inline_schema=schema)

    with pytest.raises(SchemaResolutionError, match="digest mismatch"):
        InlineSchemaRefResolver().resolve_and_verify(ref)
