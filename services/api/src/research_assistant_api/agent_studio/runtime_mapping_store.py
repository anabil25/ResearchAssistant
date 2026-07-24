"""Retrieval port for immutable, content-addressed ``RuntimeDeploymentMapping``
revisions.

A mapping is immutable and **content-addressed**: the store item id carries the
revision (``deployment_id::revision_id`` where ``revision_id`` is the hex tail of
``mapping_digest``), so multiple revisions of one ``deployment_id`` coexist in
the same ``/deployment_id`` partition under distinct ids. This resolves the
supersession/immutability tension: a lifecycle transition (ACTIVE ->
REVOKED/RETIRED/SUPERSEDED) is effected by writing a **new revision** that
records the transition, never by mutating an existing document, so
``revoked_at`` staying inside the digest is coherent with create-only storage.

Consequences of the revision model (do not reinvent):

* **``get`` is an exact ``(deployment_id, revision_id)`` point read**, never a
  scan or a "latest for this deployment" query. Authority over WHICH revision is
  current lives in the binding index (partitioned by ``client_app_id``); this
  store never chooses a revision for a caller. An absent/empty ``deployment_id``
  is never a fallback -- the loader denies before it ever reaches this store.
* **``put`` is create-only per revision.** Because the id carries the digest, a
  superseding revision has a different id and never collides; re-putting the
  identical revision (same id, same content) is idempotent, and the only way to
  observe a 409 is a byte-identical re-put. A 409 whose stored content diverges
  from the intended content is an integrity violation (fail closed), never a
  silent overwrite.

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
    """Raised when a revision id is re-put with different content.

    Content-addressed immutability: a revision is keyed by
    ``deployment_id::revision_id`` where ``revision_id`` is the mapping's own
    content digest. A stored revision's content therefore can never diverge from
    its id, so a 409 with diverging content is an integrity violation, not an
    update.
    """


def _revision_item_id(deployment_id: str, revision_id: str) -> str:
    """Compose the content-addressed store item id for a mapping revision."""
    return f"{deployment_id}::{revision_id}"


class RuntimeDeploymentMappingStore(Protocol):
    """Domain port for storing and point-reading runtime deployment mapping revisions."""

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        """Persist ``mapping`` as an immutable revision; idempotent for identical content."""
        ...

    def get(self, deployment_id: str, revision_id: str) -> RuntimeDeploymentMapping | None:
        """Fresh exact point read of the ``(deployment_id, revision_id)`` revision (or ``None``)."""
        ...


class InMemoryRuntimeDeploymentMappingStore:
    """In-memory adapter keyed by the content-addressed item id (tests/local only).

    Because the item id is derived from the content digest, two puts under the
    same id are the same revision by construction; a re-put is therefore
    idempotent (the first-stored revision is retained and returned). Divergent
    content under one id is only reachable via a hash collision, which this
    layer does not defend against.
    """

    def __init__(self) -> None:
        self._by_item_id: dict[str, RuntimeDeploymentMapping] = {}

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        item_id = _revision_item_id(mapping.deployment_id, mapping.revision_id)
        existing = self._by_item_id.get(item_id)
        if existing is not None:
            return existing
        self._by_item_id[item_id] = mapping
        return mapping

    def get(self, deployment_id: str, revision_id: str) -> RuntimeDeploymentMapping | None:
        return self._by_item_id.get(_revision_item_id(deployment_id, revision_id))


#: Cosmos ``documentType`` discriminator and partition-key path for the
#: dedicated runtime deployment mapping container. The container is
#: partitioned by ``/deployment_id`` so every revision of one deployment shares
#: a partition and a runtime point-read is always a single-partition
#: ``read_item`` by the content-addressed item id -- never a scope-keyed lookup
#: (a runtime never supplies a scope) and never a cross-partition query.
RUNTIME_MAPPING_DOCUMENT_TYPE = "runtimeDeploymentMappingV1"
RUNTIME_MAPPING_PARTITION_KEY_PATH = "/deployment_id"


class CosmosRuntimeDeploymentMappingStore:
    """Durable adapter: one immutable document per mapping **revision**,
    partitioned by ``/deployment_id``.

    Writes use ``create_item`` (never ``upsert``) so Cosmos itself is the atomic
    create-if-absent primitive. Because the item id carries the revision digest,
    a superseding revision is a *new* item (different id) that never 409s; the
    only 409 is a byte-identical re-put of the same revision, treated as
    idempotent. A 409 whose stored content diverges is a hard
    ``RuntimeMappingConflictError``. ``get`` is always a fresh exact point
    ``read_item`` of the ``(deployment_id, revision_id)`` item (404 -> None),
    never a cache-first return and never a "latest revision" query.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def put(self, mapping: RuntimeDeploymentMapping) -> RuntimeDeploymentMapping:
        item_id = _revision_item_id(mapping.deployment_id, mapping.revision_id)
        document = {
            "id": item_id,
            "documentType": RUNTIME_MAPPING_DOCUMENT_TYPE,
            "deployment_id": mapping.deployment_id,
            "revision_id": mapping.revision_id,
            # Denormalized index fields for governance queries; the authoritative
            # values always live inside ``payload``.
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
            existing = self.get(mapping.deployment_id, mapping.revision_id)
            if existing is None or existing.mapping_digest != mapping.mapping_digest:
                raise RuntimeMappingConflictError(
                    f"A runtime deployment mapping revision '{item_id}' already exists "
                    "with different content; mapping revisions are immutable."
                ) from exc
            return existing
        return mapping

    def get(self, deployment_id: str, revision_id: str) -> RuntimeDeploymentMapping | None:
        item_id = _revision_item_id(deployment_id, revision_id)
        try:
            document = dict(self._container.read_item(item=item_id, partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
        return RuntimeDeploymentMapping.model_validate(document["payload"])
