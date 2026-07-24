"""Retrieval port for immutable ``RuntimeDeploymentMapping`` records.

The runtime-control plane resolves a mapping by its **opaque, server-generated
``deployment_id`` only** -- never by a caller-supplied tenant/project
partition. The mapping's own ``tenant_id``/``project_id`` (inside the stored
document) are the authoritative scope; a runtime cannot widen or redirect its
request by choosing a partition, because it never names one. ``deployment_id``
is therefore the partition/point-read key for this store.

Mappings are immutable and content-addressed by ``deployment_id``: putting the
same ``deployment_id`` twice is idempotent **only** when the content
(``mapping_digest``) is identical; a second put with different content for an
already-present ``deployment_id`` is a hard conflict (fail closed) rather than
a silent overwrite, so a released mapping can never be mutated in place.

This module defines the port plus an in-memory adapter for tests/local. A
durable Cosmos point-read adapter (partitioned by ``deployment_id``, always a
fresh read, never cache-first) is a separately-reviewed slice implementing the
same ``RuntimeDeploymentMappingStore`` protocol.
"""

from __future__ import annotations

from typing import Protocol

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping


class RuntimeDeploymentMappingStoreError(RuntimeError):
    """Base error for runtime deployment mapping persistence."""


class RuntimeMappingConflictError(RuntimeDeploymentMappingStoreError):
    """Raised when a ``deployment_id`` is re-put with different content.

    Content-addressed immutability: a mapping is keyed by its opaque
    ``deployment_id`` and, once stored, its content may never change. A second
    put with a diverging ``mapping_digest`` for the same ``deployment_id`` is
    an integrity violation, not an update.
    """


class RuntimeDeploymentMappingStore(Protocol):
    """Domain port for storing and point-reading runtime deployment mappings."""

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        """Persist ``mapping``; idempotent for identical content, conflict otherwise."""
        ...

    def get(self, deployment_id: str) -> RuntimeDeploymentMapping | None:
        """Fresh point read of the mapping for ``deployment_id`` (or ``None``)."""
        ...


class InMemoryRuntimeDeploymentMappingStore:
    """In-memory adapter keyed by opaque ``deployment_id`` (tests/local only)."""

    def __init__(self) -> None:
        self._by_deployment_id: dict[str, RuntimeDeploymentMapping] = {}

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        existing = self._by_deployment_id.get(mapping.deployment_id)
        if existing is not None and existing.mapping_digest != mapping.mapping_digest:
            raise RuntimeMappingConflictError(
                f"A runtime deployment mapping for deployment_id '{mapping.deployment_id}' "
                "already exists with different content; mappings are immutable."
            )
        self._by_deployment_id[mapping.deployment_id] = mapping
        return mapping

    def get(self, deployment_id: str) -> RuntimeDeploymentMapping | None:
        return self._by_deployment_id.get(deployment_id)
