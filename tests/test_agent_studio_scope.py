# mypy: disable-error-code=import-untyped

from __future__ import annotations

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
    unit-separator-joined, hashed encoding must not."""
    assert compute_scope_key("ab", "c") != compute_scope_key("a", "bc")


def test_compute_scope_key_is_sensitive_to_tenant_and_project() -> None:
    base = compute_scope_key("tenant-1", "project-1")
    assert compute_scope_key("tenant-2", "project-1") != base
    assert compute_scope_key("tenant-1", "project-2") != base


def test_compute_scope_key_has_stable_prefix_and_length() -> None:
    key = compute_scope_key("tenant-1", "project-1")
    assert key.startswith("sk_")
    assert len(key) == len("sk_") + 64  # sha256 hex digest length


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


def test_platform_project_id_is_reserved_constant() -> None:
    assert PLATFORM_PROJECT_ID == "__platform__"
    scope = ScopeContext(tenant_id="tenant-1", project_id=PLATFORM_PROJECT_ID)
    assert scope.project_id == PLATFORM_PROJECT_ID
