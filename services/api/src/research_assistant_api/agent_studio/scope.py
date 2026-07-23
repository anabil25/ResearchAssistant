"""Tenant+project partition scoping for Agent Studio persistence.

Identity groups alone are not a durable partition boundary: a group can be
renamed/reused, and Entra ID token "group overage" can silently truncate the
``groups`` claim. Every project-scoped Agent Studio document therefore binds
to an explicit, non-optional ``(tenant_id, project_id)`` pair, carried by
``ScopeContext`` and reduced to a single synthetic partition key via
``compute_scope_key`` — never an ambiguous ``f"{tenant_id}{project_id}"``
concatenation, which cannot be un-concatenated and can collide
(``("ab", "c")`` vs ``("a", "bc")``).

Repository APIs accept a ``ScopeContext`` (not a bare ``tenant_id: str``) so
every point read/write is scoped to both tenant *and* project by
construction; a cross-partition query that fetches broadly and filters
client-side can never satisfy this signature, which is the point.

Global, application-owned catalog resources (``CapabilityDescriptor``,
future templates) are explicitly *not* scope-keyed — they live in a
separate catalog repository/container, independent of any tenant/project.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

#: Reserved project id for platform/system-owned resources (system agents,
#: the global capability catalog's *governance* records, etc.) that are not
#: naturally owned by any single researcher-created project. Every
#: ``agentStudioMetadataV1``/``agentStudioMemoryV1``/``agentStudioAuditV1``
#: document is still project scoped — system agents are scoped to this
#: reserved project rather than left project-less, so partitioning stays
#: total (no "tenant-wide, no project" documents sneak into project-scoped
#: containers). Platform owners (see ``AgentRole``/ownership) are the only
#: principals expected to hold grants scoped to this project.
PLATFORM_PROJECT_ID = "__platform__"

_SCOPE_KEY_SEPARATOR = "\x1f"  # ASCII unit separator: never a legal id character


class ScopeContext(BaseModel):
    """Non-optional tenant+project partition boundary for a repository call.

    Every Agent Studio repository/service method that reads or writes a
    project-scoped record accepts a ``ScopeContext`` rather than a bare
    ``tenant_id``, so tenant *and* project are always both present and a
    caller cannot accidentally issue a tenant-only (cross-project) query.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)

    @property
    def scope_key(self) -> str:
        return compute_scope_key(self.tenant_id, self.project_id)

    def as_tuple(self) -> tuple[str, str]:
        return (self.tenant_id, self.project_id)


def compute_scope_key(tenant_id: str, project_id: str) -> str:
    """Canonical, collision-safe partition key for a ``(tenant_id, project_id)`` pair.

    Uses a unit-separator-joined tuple (a control character that is never a
    legal id character in this system) hashed with SHA-256, so distinct
    ``(tenant_id, project_id)`` pairs can never collide via naive
    concatenation (e.g. ``("ab", "c")`` vs ``("a", "bc")``) and the result is
    a short, fixed-length string safe to use as a Cosmos DB ``/scope_key``
    partition key value.
    """

    canonical = _SCOPE_KEY_SEPARATOR.join((tenant_id, project_id))
    return "sk_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
