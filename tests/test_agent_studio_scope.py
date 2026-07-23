# mypy: disable-error-code=import-untyped

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.scope import (
    PLATFORM_PROJECT_ID,
    ScopeContext,
    compute_scope_key,
)


def test_compute_scope_key_is_deterministic() -> None:
    assert compute_scope_key("tenant-1", "project-1") == compute_scope_key("tenant-1", "project-1")


def test_compute_scope_key_distinguishes_concatenation_collisions() -> None:
    """Naive concatenation would collide on ("ab", "c") vs ("a", "bc"); the
    canonical JSON-array-encoded, hashed key must not."""
    assert compute_scope_key("ab", "c") != compute_scope_key("a", "bc")


def test_compute_scope_key_is_sensitive_to_tenant_and_project() -> None:
    base = compute_scope_key("tenant-1", "project-1")
    assert compute_scope_key("tenant-2", "project-1") != base
    assert compute_scope_key("tenant-1", "project-2") != base


def test_compute_scope_key_has_stable_prefix_and_length() -> None:
    key = compute_scope_key("tenant-1", "project-1")
    assert key.startswith("scope:v1:sha256:")
    assert len(key) == len("scope:v1:sha256:") + 64  # sha256 hex digest length


def test_compute_scope_key_golden_vector() -> None:
    """Pin the exact encoding: a JSON array of the two strings, UTF-8 encoded,
    SHA-256 hashed. Any accidental drift in the encoding (e.g. switching back
    to separator-joined concatenation, changing key ordering, or changing the
    JSON separators) must fail this test rather than silently changing every
    existing Cosmos partition key's meaning.
    """
    canonical = json.dumps(["tenant-1", "project-1"], ensure_ascii=False, separators=(",", ":"))
    assert canonical == '["tenant-1","project-1"]'
    expected = "scope:v1:sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert compute_scope_key("tenant-1", "project-1") == expected


def test_compute_scope_key_is_order_sensitive() -> None:
    """Swapping tenant_id and project_id must never produce the same key."""
    assert compute_scope_key("a", "b") != compute_scope_key("b", "a")


def test_compute_scope_key_never_exposes_raw_ids() -> None:
    key = compute_scope_key("super-secret-tenant", "super-secret-project")
    assert "super-secret-tenant" not in key
    assert "super-secret-project" not in key


def test_scope_context_scope_key_matches_free_function() -> None:
    scope = ScopeContext(tenant_id="tenant-1", project_id="project-1")
    assert scope.scope_key == compute_scope_key("tenant-1", "project-1")


def test_scope_context_as_tuple() -> None:
    scope = ScopeContext(tenant_id="tenant-1", project_id="project-2")
    assert scope.as_tuple() == ("tenant-1", "project-2")


def test_scope_context_is_frozen_and_forbids_extra_fields() -> None:
    scope = ScopeContext(tenant_id="tenant-1", project_id="project-1")
    with pytest.raises(ValidationError):
        scope.tenant_id = "tenant-2"
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant-1", project_id="project-1", extra="nope")  # type: ignore[call-arg]


def test_scope_context_rejects_blank_ids() -> None:
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="", project_id="project-1")
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant-1", project_id="")


def test_scope_context_rejects_control_characters_in_tenant_id() -> None:
    """The scope-key JSON encoding is collision-safe for any two strings, but
    only if control characters (which a hypothetical future encoding change
    might treat as structurally significant, and which are never legitimate
    id content) can never reach ``compute_scope_key`` through this model."""
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant\x1f1", project_id="project-1")


def test_scope_context_rejects_control_characters_in_project_id() -> None:
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant-1", project_id="project\x1f1")
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant-1", project_id="project\x001")
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="tenant-1", project_id="project\x7f1")


def test_scope_context_would_have_collided_under_the_old_separator_scheme() -> None:
    """Regression for the fixed vulnerability: the previous unit-separator
    concatenation scheme relied on ``\\x1f`` never appearing in an id. These
    two pairs would concatenate to the identical string under that scheme
    (``"a" + "\\x1f" + "b\\x1fc"`` == ``"a\\x1f" + "b\\x1fc"``... i.e. a
    crafted id containing the separator can reproduce another pair's joined
    form). Such ids must now be rejected outright rather than silently
    colliding.
    """
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="a", project_id="b\x1fc")
    with pytest.raises(ValidationError):
        ScopeContext(tenant_id="a\x1fb", project_id="c")


def test_scope_context_normalizes_unicode_to_nfc() -> None:
    """A decomposed-form id (e.g. combining accent) and its precomposed
    equivalent must resolve to the same normalized value / scope_key, so a
    caller cannot bypass isolation by supplying a different Unicode encoding
    of what a human would read as the same identifier. Normalization is a
    ``ScopeContext`` field-validation concern -- the raw ``compute_scope_key``
    function intentionally does not normalize on its own, so this is
    exercised through ``ScopeContext.scope_key``."""
    decomposed = "tenant-cafe\u0301"  # "café" spelled with combining acute accent
    precomposed = "tenant-café"
    assert ScopeContext(tenant_id=decomposed, project_id="p").tenant_id == precomposed
    decomposed_key = ScopeContext(tenant_id=decomposed, project_id="p").scope_key
    precomposed_key = ScopeContext(tenant_id=precomposed, project_id="p").scope_key
    assert decomposed_key == precomposed_key == compute_scope_key(precomposed, "p")


def test_platform_project_id_is_reserved_constant() -> None:
    assert PLATFORM_PROJECT_ID == "__platform__"
    scope = ScopeContext(tenant_id="tenant-1", project_id=PLATFORM_PROJECT_ID)
    assert scope.project_id == PLATFORM_PROJECT_ID
