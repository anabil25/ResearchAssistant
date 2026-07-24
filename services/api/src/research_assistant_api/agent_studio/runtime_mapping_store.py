"""Retrieval port for immutable, sequence-addressed ``RuntimeDeploymentMapping``
revisions.

A mapping is immutable and every revision of a deployment coexists in the
``/deployment_id`` partition under the item id ``deployment_id:sequence`` (the
monotonic ``revision_sequence``, NOT the digest). Keying by the sequence -- not
the digest -- is what makes the store the ATOMIC ADJUDICATOR of single
succession: two *different* contents at the same sequence collide on the id, so
create-only semantics reject the second with a 409, and forging a competing
revision at an existing sequence is IMPOSSIBLE rather than merely disallowed,
even if the control plane is buggy or racing. (Had the id carried the digest,
two different contents at one sequence would get two distinct ids and BOTH would
create successfully, silently destroying the single-successor property.)

Consequences (do not reinvent):

* **``get`` is an exact ``(deployment_id, revision_sequence)`` point read**,
  never a scan or a "latest for this deployment" query. Authority over WHICH
  revision is current lives in the binding index (partitioned by
  ``client_app_id``); this store never chooses a revision for a caller.
* **``put`` is create-only per sequence.** A superseding revision has a greater
  sequence -> a new id -> never collides. A re-put of the IDENTICAL revision
  (same sequence, same digest -- e.g. a control-plane retry of the same
  caller-supplied payload) hits the 409 path with MATCHING content and is
  idempotent (this is the branch R4's escalation showed was previously
  unreachable). A 409 whose stored content DIVERGES from the intended content
  at the same sequence is a hard integrity violation (fail closed) -- a forged
  or racing competitor -- never a silent overwrite.

This module defines the port plus an in-memory adapter for tests/local. The
Cosmos adapter partitions by ``/deployment_id`` and always performs a fresh
point ``read_item`` (never cache-first).
"""

from __future__ import annotations

from typing import Protocol

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping


class RuntimeDeploymentMappingStoreError(RuntimeError):
    """Base error for runtime deployment mapping persistence."""


class RuntimeMappingConflictError(RuntimeDeploymentMappingStoreError):
    """Raised when a ``deployment_id:sequence`` is created with diverging content.

    Sequence-addressed single succession: a revision is keyed by
    ``deployment_id:revision_sequence``. A second, byte-different content at an
    already-occupied sequence is a forged/racing competitor the store rejects
    atomically, not an update.
    """


def _revision_item_id(deployment_id: str, revision_sequence: int) -> str:
    """Compose the sequence-addressed store item id for a mapping revision."""
    return f"{deployment_id}:{revision_sequence}"


class RuntimeDeploymentMappingStore(Protocol):
    """Domain port for storing and point-reading runtime deployment mapping revisions."""

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        """Persist ``mapping`` at its ``deployment_id:sequence``; idempotent for identical content."""
        ...

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        """Fresh exact point read of the ``(deployment_id, revision_sequence)`` revision (or ``None``)."""
        ...


class InMemoryRuntimeDeploymentMappingStore:
    """In-memory adapter keyed by ``deployment_id:sequence`` (tests/local only).

    Mirrors the store-adjudicated single-successor semantics: a re-put of the
    identical revision (same sequence, same digest) is idempotent and returns the
    stored revision; a DIFFERENT content at an already-occupied sequence raises
    ``RuntimeMappingConflictError`` (a forged/racing competitor), never a silent
    overwrite.
    """

    def __init__(self) -> None:
        self._by_item_id: dict[str, RuntimeDeploymentMapping] = {}

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        item_id = _revision_item_id(mapping.deployment_id, mapping.revision_sequence)
        existing = self._by_item_id.get(item_id)
        if existing is not None:
            if existing.mapping_digest != mapping.mapping_digest:
                raise RuntimeMappingConflictError(
                    f"A runtime deployment mapping revision '{item_id}' already exists "
                    "with different content; a sequence has exactly one successor."
                )
            return existing
        self._by_item_id[item_id] = mapping
        return mapping

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        return self._by_item_id.get(_revision_item_id(deployment_id, revision_sequence))


#: Cosmos ``documentType`` discriminator and partition-key path for the
#: dedicated runtime deployment mapping container. The container is
#: partitioned by ``/deployment_id`` so every revision of one deployment shares
#: a partition and a runtime point-read is always a single-partition
#: ``read_item`` by the content-addressed item id -- never a scope-keyed lookup
#: (a runtime never supplies a scope) and never a cross-partition query.
RUNTIME_MAPPING_DOCUMENT_TYPE = "runtimeDeploymentMappingV1"
RUNTIME_MAPPING_PARTITION_KEY_PATH = "/deployment_id"


class CosmosRuntimeDeploymentMappingStore:
    """Durable adapter: one immutable document per mapping **revision**, keyed by
    ``deployment_id:sequence`` and partitioned by ``/deployment_id``.

    Writes use ``create_item`` (never ``upsert``) so Cosmos is the atomic
    adjudicator of single succession: a superseding revision has a greater
    sequence -> a new id -> never 409s; two writers racing the SAME next sequence
    both attempt the same id and exactly one create wins. A 409 whose stored
    content matches (a control-plane retry of the identical payload) is
    idempotent; a 409 whose stored content DIVERGES at that sequence is a hard
    ``RuntimeMappingConflictError`` (forged/racing competitor). ``get`` is always
    a fresh exact point ``read_item`` of the ``(deployment_id, revision_sequence)``
    item (404 -> None), never cache-first and never a "latest revision" query.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        item_id = _revision_item_id(mapping.deployment_id, mapping.revision_sequence)
        document = {
            "id": item_id,
            "documentType": RUNTIME_MAPPING_DOCUMENT_TYPE,
            "deployment_id": mapping.deployment_id,
            "revision_sequence": mapping.revision_sequence,
            # Denormalized index fields for governance queries; the authoritative
            # values always live inside ``payload``.
            "revision_id": mapping.revision_id,
            "mapping_digest": mapping.mapping_digest,
            "tenant_id": mapping.tenant_id,
            "project_id": mapping.project_id,
            "logical_agent_id": mapping.logical_agent_id,
            "lifecycle_state": mapping.lifecycle_state.value,
            "payload": mapping.model_dump(mode="json"),
        }
        try:
            self._container.create_item(document)
        except CosmosHttpResponseError as exc:
            if exc.status_code != 409:
                raise
            existing = self.get(mapping.deployment_id, mapping.revision_sequence)
            if existing is None or existing.mapping_digest != mapping.mapping_digest:
                raise RuntimeMappingConflictError(
                    f"A runtime deployment mapping revision '{item_id}' already exists "
                    "with different content; a sequence has exactly one successor."
                ) from exc
            return existing
        return mapping

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        item_id = _revision_item_id(deployment_id, revision_sequence)
        try:
            document = dict(self._container.read_item(item=item_id, partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
        return RuntimeDeploymentMapping.model_validate(document["payload"])
