"""Sequence-addressed store for immutable ``RuntimeDeploymentMapping`` revisions
plus the per-deployment succession HEAD.

Two kinds of item share one ``/deployment_id`` partition:

* **Revision items** (``deployment_id:sequence``) are IMMUTABLE and create-only.
  Keying by the monotonic ``revision_sequence`` (not the digest) makes id
  uniqueness the backstop for single succession: two different contents at one
  sequence collide on the id, so the second create is rejected.
* **The HEAD record** (``deployment_id:head``) is the SINGLE source of truth for
  "which revision is current" and therefore for ``next = current + 1``. Unlike
  the revision items it is MUTABLE control-plane state -- do NOT "fix" this into
  an immutable document, or succession breaks. Per-client binding rows answer a
  different question ("which revision may THIS client see") and may legitimately
  LAG the head during a partial multi-client repoint; they are not the source of
  ``next``.

Why the head lives here, in the mapping container, same partition: cross-
container atomicity does not exist, but a Cosmos transactional batch IS available
within a single container AND a single partition key value. A revision item and
the head record satisfy both, so SUPERSEDE is ONE atomic batch
``[create revision N+1, replace head If-Match]`` -- succeeds or fails as a unit,
removing the skip/collide ambiguity that deriving ``next`` from N per-client
bindings would create.

Port split (mandatory): the RUNTIME port (``RuntimeDeploymentMappingReader``)
exposes EXACT ``(deployment_id, sequence)`` point reads ONLY -- no head, no
"current/latest", no enumeration. The head and revision enumeration (for
retention) live only on the CONTROL-PLANE port
(``RuntimeDeploymentMappingControlPlane``); a ``list_revisions`` on the runtime
port would reopen exactly the selection surface the no-latest-accessor rule
exists to close.
"""

from __future__ import annotations

from typing import Any, Protocol

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping


class RuntimeDeploymentMappingStoreError(RuntimeError):
    """Base error for runtime deployment mapping persistence."""


class RuntimeMappingConflictError(RuntimeDeploymentMappingStoreError):
    """Raised when a ``deployment_id:sequence`` is created with diverging content.

    A revision is keyed by ``deployment_id:revision_sequence``; a second,
    byte-different content at an already-occupied sequence is a forged/racing
    competitor the store rejects, not an update.
    """


class RuntimeHeadPreconditionError(RuntimeDeploymentMappingStoreError):
    """Raised when the succession HEAD precondition fails.

    Bootstrap expects NO head (create-only / If-None-Match); supersede expects
    the head to still be at the caller-observed sequence (If-Match). Either
    failing means a concurrent supersede moved the head -- the caller must
    re-read the head, recompute ``next``, and retry (never blind-retry).
    """


def _revision_item_id(deployment_id: str, revision_sequence: int) -> str:
    return f"{deployment_id}:{revision_sequence}"


def _head_item_id(deployment_id: str) -> str:
    return f"{deployment_id}:head"


class RuntimeDeploymentHead(BaseModel):
    """The mutable per-deployment succession pointer (single source of ``next``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: str
    current_sequence: int = Field(ge=1)
    current_revision_id: str


class RuntimeDeploymentMappingReader(Protocol):
    """RUNTIME port: exact point reads only -- no head, no latest, no enumeration."""

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        """Fresh exact point read of the ``(deployment_id, revision_sequence)`` revision (or ``None``)."""
        ...


class RuntimeDeploymentMappingControlPlane(RuntimeDeploymentMappingReader, Protocol):
    """CONTROL-PLANE port: head + revision enumeration + succession writes."""

    def get_head(self, deployment_id: str) -> RuntimeDeploymentHead | None:
        """The current succession head for ``deployment_id`` (or ``None`` before bootstrap)."""
        ...

    def list_revisions(self, deployment_id: str) -> tuple[int, ...]:
        """Every stored revision sequence for ``deployment_id`` (ascending), for retention."""
        ...

    def commit_revision(self, mapping: RuntimeDeploymentMapping, *, expected_head_sequence: int | None) -> None:
        """Atomically publish ``mapping`` as the current revision.

        ``expected_head_sequence is None`` bootstraps a brand-new deployment
        (create-only: the head must not already exist). An integer supersedes:
        the head must still be at that sequence (If-Match). Both write the
        revision item and (create/replace) the head as ONE unit; an already-
        committed identical (revision, head) is idempotent.
        """
        ...

    def delete(self, deployment_id: str, revision_sequence: int) -> None:
        """Delete one exact revision item (retention; never the head, never 'latest')."""
        ...


class InMemoryRuntimeDeploymentMappingStore:
    """In-memory adapter implementing both ports (tests/local only)."""

    def __init__(self) -> None:
        self._revisions: dict[str, RuntimeDeploymentMapping] = {}
        self._heads: dict[str, RuntimeDeploymentHead] = {}

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        return self._revisions.get(_revision_item_id(deployment_id, revision_sequence))

    def get_head(self, deployment_id: str) -> RuntimeDeploymentHead | None:
        return self._heads.get(deployment_id)

    def list_revisions(self, deployment_id: str) -> tuple[int, ...]:
        prefix = f"{deployment_id}:"
        seqs = [
            int(item_id[len(prefix) :])
            for item_id in self._revisions
            if item_id.startswith(prefix)
        ]
        return tuple(sorted(seqs))

    def commit_revision(self, mapping: RuntimeDeploymentMapping, *, expected_head_sequence: int | None) -> None:
        item_id = _revision_item_id(mapping.deployment_id, mapping.revision_sequence)
        existing_rev = self._revisions.get(item_id)
        head = self._heads.get(mapping.deployment_id)
        # Idempotent replay: the revision AND the head already reflect this exact
        # commit -> no-op success (a retried, fully-applied commit).
        if (
            existing_rev is not None
            and existing_rev.mapping_digest == mapping.mapping_digest
            and head is not None
            and head.current_sequence == mapping.revision_sequence
            and head.current_revision_id == mapping.revision_id
        ):
            return
        if expected_head_sequence is None:
            if head is not None:
                raise RuntimeHeadPreconditionError(
                    f"bootstrap for '{mapping.deployment_id}' expected no head, but one exists "
                    f"at sequence {head.current_sequence}."
                )
        elif head is None or head.current_sequence != expected_head_sequence:
            observed = None if head is None else head.current_sequence
            raise RuntimeHeadPreconditionError(
                f"supersede for '{mapping.deployment_id}' expected head at {expected_head_sequence}, "
                f"observed {observed}."
            )
        if existing_rev is not None and existing_rev.mapping_digest != mapping.mapping_digest:
            raise RuntimeMappingConflictError(
                f"A runtime deployment mapping revision '{item_id}' already exists with different content."
            )
        self._revisions[item_id] = mapping
        self._heads[mapping.deployment_id] = RuntimeDeploymentHead(
            deployment_id=mapping.deployment_id,
            current_sequence=mapping.revision_sequence,
            current_revision_id=mapping.revision_id,
        )

    def delete(self, deployment_id: str, revision_sequence: int) -> None:
        self._revisions.pop(_revision_item_id(deployment_id, revision_sequence), None)


#: Cosmos ``documentType`` discriminators and partition-key path.
RUNTIME_MAPPING_DOCUMENT_TYPE = "runtimeDeploymentMappingV1"
RUNTIME_MAPPING_HEAD_DOCUMENT_TYPE = "runtimeDeploymentMappingHeadV1"
RUNTIME_MAPPING_PARTITION_KEY_PATH = "/deployment_id"


class CosmosRuntimeDeploymentMappingStore:
    """Durable adapter implementing both ports over a ``/deployment_id`` container.

    ``commit_revision`` is a single-partition TRANSACTIONAL BATCH: bootstrap is
    ``[create revision, create head]`` (both create-only, so a concurrent
    double-bootstrap is store-adjudicated -- one batch 409s); supersede is
    ``[create revision N+1, replace head If-Match]`` (atomic, so ``next`` can
    never skip or collide). ``get`` is a fresh exact point read; ``get_head`` is a
    point read of the head item; ``list_revisions`` is the only query and lives on
    the control-plane port only.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        try:
            document = dict(
                self._container.read_item(
                    item=_revision_item_id(deployment_id, revision_sequence), partition_key=deployment_id
                )
            )
        except CosmosResourceNotFoundError:
            return None
        return RuntimeDeploymentMapping.model_validate(document["payload"])

    def get_head(self, deployment_id: str) -> RuntimeDeploymentHead | None:
        try:
            document = dict(self._container.read_item(item=_head_item_id(deployment_id), partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
        return RuntimeDeploymentHead(
            deployment_id=deployment_id,
            current_sequence=int(document["current_sequence"]),
            current_revision_id=str(document["current_revision_id"]),
        )

    def list_revisions(self, deployment_id: str) -> tuple[int, ...]:
        query = (
            "SELECT c.revision_sequence FROM c "
            "WHERE c.documentType = @t ORDER BY c.revision_sequence ASC"
        )
        items = self._container.query_items(
            query=query,
            parameters=[{"name": "@t", "value": RUNTIME_MAPPING_DOCUMENT_TYPE}],
            partition_key=deployment_id,
        )
        return tuple(int(item["revision_sequence"]) for item in items)

    def _revision_document(self, mapping: RuntimeDeploymentMapping) -> dict[str, Any]:
        return {
            "id": _revision_item_id(mapping.deployment_id, mapping.revision_sequence),
            "documentType": RUNTIME_MAPPING_DOCUMENT_TYPE,
            "deployment_id": mapping.deployment_id,
            "revision_sequence": mapping.revision_sequence,
            "revision_id": mapping.revision_id,
            "mapping_digest": mapping.mapping_digest,
            "tenant_id": mapping.tenant_id,
            "project_id": mapping.project_id,
            "logical_agent_id": mapping.logical_agent_id,
            "payload": mapping.model_dump(mode="json"),
        }

    def _head_document(self, mapping: RuntimeDeploymentMapping) -> dict[str, Any]:
        return {
            "id": _head_item_id(mapping.deployment_id),
            "documentType": RUNTIME_MAPPING_HEAD_DOCUMENT_TYPE,
            "deployment_id": mapping.deployment_id,
            "current_sequence": mapping.revision_sequence,
            "current_revision_id": mapping.revision_id,
        }

    def commit_revision(self, mapping: RuntimeDeploymentMapping, *, expected_head_sequence: int | None) -> None:
        existing_rev = self.get(mapping.deployment_id, mapping.revision_sequence)
        head = self.get_head(mapping.deployment_id)
        if (
            existing_rev is not None
            and existing_rev.mapping_digest == mapping.mapping_digest
            and head is not None
            and head.current_sequence == mapping.revision_sequence
            and head.current_revision_id == mapping.revision_id
        ):
            return
        revision_doc = self._revision_document(mapping)
        head_doc = self._head_document(mapping)
        batch: list[tuple[Any, ...]]
        if expected_head_sequence is None:
            batch = [("create", (revision_doc,)), ("create", (head_doc,))]
        else:
            head_raw = self._read_head_raw(mapping.deployment_id)
            if head_raw is None or int(head_raw["current_sequence"]) != expected_head_sequence:
                observed = None if head_raw is None else int(head_raw["current_sequence"])
                raise RuntimeHeadPreconditionError(
                    f"supersede for '{mapping.deployment_id}' expected head at {expected_head_sequence}, "
                    f"observed {observed}."
                )
            batch = [
                ("create", (revision_doc,)),
                ("replace", (head_doc["id"], head_doc), {"if_match_etag": str(head_raw["_etag"])}),
            ]
        try:
            self._container.execute_item_batch(batch_operations=batch, partition_key=mapping.deployment_id)
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                # A revision collision with matching content is idempotent; any
                # other precondition failure means the head moved -> fail closed.
                existing_rev = self.get(mapping.deployment_id, mapping.revision_sequence)
                if existing_rev is not None and existing_rev.mapping_digest != mapping.mapping_digest:
                    raise RuntimeMappingConflictError(
                        f"A runtime deployment mapping revision at sequence {mapping.revision_sequence} already "
                        "exists with different content."
                    ) from exc
                raise RuntimeHeadPreconditionError(
                    f"succession precondition failed for '{mapping.deployment_id}'; re-read head and retry."
                ) from exc
            raise

    def delete(self, deployment_id: str, revision_sequence: int) -> None:
        try:
            self._container.delete_item(
                item=_revision_item_id(deployment_id, revision_sequence), partition_key=deployment_id
            )
        except CosmosResourceNotFoundError:
            return

    def _read_head_raw(self, deployment_id: str) -> dict[str, Any] | None:
        try:
            return dict(self._container.read_item(item=_head_item_id(deployment_id), partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
