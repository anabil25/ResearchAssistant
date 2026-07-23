"""Resolution/verification for ``AgentManifest.input_schema_ref``/``output_schema_ref``.

Cutting a version must not accept a declared ``SchemaRef.digest`` at face
value: this module resolves the actual schema (from ``inline_schema`` when
present) and independently recomputes its digest using the same convention
as every other schema digest in this package (``sha256:`` + sorted-key,
compact-separator canonical JSON — see ``router.get_agent_manifest_schema``),
rejecting the cut when the computed digest doesn't match the declared one.
A ``SchemaRef`` with no inline schema and no fetch backend is a real,
fail-closed error — never silently skipped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from research_assistant_api.agent_studio.models import SchemaRef


class SchemaResolutionError(RuntimeError):
    pass


def compute_schema_digest(schema: dict[str, object]) -> str:
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SchemaRefResolver(Protocol):
    def resolve_and_verify(self, ref: SchemaRef) -> dict[str, object]: ...


class InlineSchemaRefResolver:
    """Resolves/verifies a ``SchemaRef`` using its own embedded ``inline_schema``.

    A production deployment backed by a real schema registry can supply a
    resolver that fetches the schema by ``ref`` instead; this default
    resolver only trusts what the caller declared inline, but still
    independently recomputes and verifies the digest rather than accepting
    ``ref.digest`` at face value.
    """

    def resolve_and_verify(self, ref: SchemaRef) -> dict[str, object]:
        if ref.inline_schema is None:
            raise SchemaResolutionError(
                f"Schema ref '{ref.ref}' has no inline schema to verify and no fetch backend is configured."
            )
        computed = compute_schema_digest(ref.inline_schema)
        if computed != ref.digest:
            raise SchemaResolutionError(
                f"Schema ref '{ref.ref}' digest mismatch: declared '{ref.digest}', computed '{computed}'."
            )
        return ref.inline_schema
