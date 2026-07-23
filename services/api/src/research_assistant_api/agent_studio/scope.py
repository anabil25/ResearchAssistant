"""Tenant+project partition scoping for Agent Studio persistence.

Identity groups alone are not a durable partition boundary: a group can be
renamed/reused, and Entra ID token "group overage" can silently truncate the
``groups`` claim. Every project-scoped Agent Studio document therefore binds
to an explicit, non-optional ``(tenant_id, project_id)`` pair, carried by
``ScopeContext`` and reduced to a single synthetic partition key via
``compute_scope_key``.

``compute_scope_key`` encodes the pair as a canonical, finite JSON array
(``["tenant", "project"]``) before hashing rather than joining the two
strings with a separator character. A separator-joined encoding (even one
using an "reserved" control character such as ``\\x1f``) is only
collision-safe if the inputs are validated to never contain that character;
JSON-array encoding is unambiguous by construction (a 2-element JSON array
with properly escaped string elements has exactly one string→array mapping
for any pair of strings, so ``("ab", "c")`` and ``("a", "bc")`` can never
produce the same JSON text). ``ScopeContext`` additionally rejects control
characters in ``tenant_id``/``project_id`` and applies Unicode NFC
normalization, so the JSON encoding's collision-safety holds for every value
that can actually reach ``compute_scope_key`` through this model, not merely
in the common case.

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
import json
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

#: Versioned prefix for ``compute_scope_key`` output. Bumping this to a new
#: version is the only sanctioned way to ever change the encoding below —
#: never reuse ``v1`` for a different scheme, since that would silently
#: change every existing partition key's meaning.
_SCOPE_KEY_PREFIX = "scope:v1:sha256:"


def _reject_control_characters(value: str, *, field_name: str) -> str:
    """Normalize and validate a scope-component string.

    Applies Unicode NFC normalization (so two differently-encoded but
    visually/semantically identical strings compare and hash identically —
    consistent with the group-name normalization used in
    ``agent_studio.authz``) and rejects any ASCII/Latin-1 control character
    (``U+0000``-``U+001F``, ``U+007F``-``U+009F``). Control characters are
    never a legitimate part of a tenant or project identifier, and excluding
    them removes the entire class of "id smuggles a byte that the encoding
    treats as structural" risk outright, regardless of which encoding
    ``compute_scope_key`` uses internally.
    """
    normalized = unicodedata.normalize("NFC", value)
    if any((ord(char) <= 0x1F) or (0x7F <= ord(char) <= 0x9F) for char in normalized):
        raise ValueError(f"{field_name} must not contain control characters.")
    return normalized


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

    @field_validator("tenant_id")
    @classmethod
    def _validate_tenant_id(cls, value: str) -> str:
        return _reject_control_characters(value, field_name="tenant_id")

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, value: str) -> str:
        return _reject_control_characters(value, field_name="project_id")

    @property
    def scope_key(self) -> str:
        return compute_scope_key(self.tenant_id, self.project_id)

    def as_tuple(self) -> tuple[str, str]:
        return (self.tenant_id, self.project_id)


def compute_scope_key(tenant_id: str, project_id: str) -> str:
    """Canonical, collision-safe partition key for a ``(tenant_id, project_id)`` pair.

    Encodes the pair as a canonical, finite JSON array
    (``json.dumps([tenant_id, project_id])``, UTF-8 encoded, with sorted-key
    formatting irrelevant since this is a plain array, not an object) and
    hashes it with SHA-256. JSON string encoding escapes any character that
    would otherwise be structurally significant (quotes, backslashes,
    control characters), so the resulting text has exactly one possible
    ``(tenant_id, project_id)`` pair that produces it — distinct pairs can
    never collide, unlike a naive separator-joined concatenation whose
    safety depends on the separator never appearing in either input.

    The result is a versioned (``scope:v1:sha256:``), fixed-length, opaque
    string safe to use as a Cosmos DB ``/scope_key`` partition key value; it
    never contains or otherwise exposes the raw ``tenant_id``/``project_id``.
    """

    canonical = json.dumps([tenant_id, project_id], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_SCOPE_KEY_PREFIX}{digest}"
