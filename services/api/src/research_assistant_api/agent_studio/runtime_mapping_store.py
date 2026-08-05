"""Sequence-addressed immutable runtime mapping revisions and succession heads."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping


class RuntimeDeploymentMappingStoreError(RuntimeError):
    """Base error for runtime deployment mapping persistence."""


class RuntimeMappingConflictError(RuntimeDeploymentMappingStoreError):
    """Raised when one immutable sequence is assigned diverging content."""


class RuntimeHeadPreconditionError(RuntimeDeploymentMappingStoreError):
    """Raised when a concurrent update invalidates a succession precondition."""


class RuntimeHeadClaimError(RuntimeDeploymentMappingStoreError):
    """Raised when a deployment head claim violates one-client ownership."""


def _revision_item_id(deployment_id: str, revision_sequence: int) -> str:
    return f"{deployment_id}:{revision_sequence}"


def _head_item_id(deployment_id: str) -> str:
    return f"{deployment_id}:head"


def _bound_client(mapping: RuntimeDeploymentMapping) -> str:
    return mapping.allowed_client_app_role_bindings[0].client_app_id


class RuntimeHeadClaimState(StrEnum):
    CLAIMING = "claiming"
    BOUND = "bound"


def _next_claim_state(
    head: RuntimeDeploymentHead | None,
    bound_client: str,
) -> RuntimeHeadClaimState:
    if head is None or head.bound_client_app_id is None or head.claim_state is None:
        return RuntimeHeadClaimState.CLAIMING
    return head.claim_state


class RuntimeDeploymentHead(BaseModel):
    """Mutable succession pointer and two-phase deployment-to-client claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: str
    current_sequence: int = Field(ge=1)
    current_revision_id: str
    bound_client_app_id: str | None = None
    claim_state: RuntimeHeadClaimState | None = None


@runtime_checkable
class RuntimeDeploymentMappingReader(Protocol):
    """RUNTIME port: the single exact point read, and NOTHING else.

    ``@runtime_checkable`` is deliberate: it lets the absence-control test module
    assert ``not isinstance(control_plane_adapter, RuntimeDeploymentMappingReader)``
    -- turning "the control-plane adapter must not structurally satisfy the runtime
    port" (absence 13) into a POSITIVE executable test. ABSENCE-SITING WARNING: the
    control-plane adapter satisfies this only by NOT exposing ``get`` (it composes a
    reader instead); a later convenience ``get()`` delegating to ``self.reader.get``
    silently restores structural compatibility and this isinstance test stops
    failing -- do not add it.

    STANDING REVIEW RULE -- adding ANY method to this Protocol is a
    SECURITY-RELEVANT change and requires explicit justification. Reachability from
    the runtime authorization path is decided ENTIRELY by what this Protocol
    DECLARES: a runtime reference typed as this port can reach exactly the methods
    named here, regardless of which concrete object is behind it. Every method
    added widens that reachability silently, without touching any adapter. Keep it
    minimal -- ideally just ``get``.

    HONEST THREE-PART CLAIM (do not overstate any single layer):
    * TYPES give "head/enumeration/writes are UNREACHABLE THROUGH THIS REFERENCE"
      -- because this Protocol declares none of them. This is the property that
      does the real work on the runtime path, and it holds no matter what object
      is passed.
    * The COMPOSITION ROOT gives "the runtime plane is never handed a write-capable
      object" -- a separate property (the runtime module must not construct the
      control-plane adapter; see the module-boundary test). Python Protocols are
      STRUCTURAL, so a control-plane adapter that happened to expose ``get`` would
      satisfy this port with no declared relationship -- which is why the
      control-plane adapter is made structurally INCOMPATIBLE (it does not expose
      ``get`` at all; it holds a reader instead), so a mis-wire is a mypy error.
    * RBAC gives "denied even if reached" -- the runtime app-role has read-only
      data-plane access; head/binding writes require a control-plane identity.
    NEITHER types NOR the composition root can give "cannot be the wrong object" on
    their own; only RBAC makes a wrong object harmless. All three are owed.
    """

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        """Fresh exact point read of the ``(deployment_id, revision_sequence)`` revision (or ``None``)."""
        ...


class RuntimeDeploymentMappingControlPlane(Protocol):
    """CONTROL-PLANE port: head + revision enumeration + succession writes.

    Structurally INCOMPATIBLE with ``RuntimeDeploymentMappingReader`` by
    COMPOSITION, not by inheritance or method-renaming: it does NOT expose ``get``
    itself -- revision reads go through the ``reader`` it HOLDS
    (``self.reader.get(...)``). Its own method surface is head/enumerate/write, so a
    control-plane adapter cannot satisfy the (structural) runtime Protocol at all,
    and passing one where a runtime reader is expected is a mypy error rather than
    a silent success. This closes the "wrong object on the runtime path" gap that
    separate-Protocols-with-no-inheritance alone leaves open.
    """

    @property
    def reader(self) -> RuntimeDeploymentMappingReader:
        """The runtime reader this control plane composes over the same storage."""
        ...

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

    def finalize_head_claim(self, deployment_id: str, *, expected_client: str) -> None:
        """Finalize a CLAIMING head to BOUND (the THIRD grant write).

        CAS: the head must be ``CLAIMING(expected_client)`` -> ``BOUND(expected_client)``.
        Already ``BOUND(expected_client)`` is an idempotent no-op. Any other state
        (cleared/reaped by reconciliation, or a different client) raises
        ``RuntimeHeadClaimError`` -- the grant LOST the claim and must roll back its
        binding. This is the serialization point that makes a reconciler reaping a
        dangling claim and a grant completing MUTUALLY EXCLUSIVE.
        """
        ...

    def clear_head_claim(
        self, deployment_id: str, *, expected_client: str, expected_state: RuntimeHeadClaimState
    ) -> None:
        """CLEAR the head claim, CAS on BOTH the client AND its phase.

        ``expected_state`` is ``BOUND`` for a REVOKE's second write (clearing a
        completed grant) and for reaping a half-finished revoke; ``CLAIMING`` for a
        reaper clearing a crashed grant. Requiring the exact phase is what closes
        defect 1: if a concurrent grant FINALIZED (``CLAIMING`` -> ``BOUND``) between
        the reaper's read and its clear, a clear expecting ``CLAIMING`` fails, so the
        reaper cannot reap a just-completed grant. The current claim must equal
        ``expected_client`` AND ``expected_state`` (else ``RuntimeHeadClaimError``);
        an already-null claim is an idempotent no-op.
        """
        ...


#: Cosmos ``documentType`` discriminators and partition-key path.
RUNTIME_MAPPING_DOCUMENT_TYPE = "runtimeDeploymentMappingV1"
RUNTIME_MAPPING_HEAD_DOCUMENT_TYPE = "runtimeDeploymentMappingHeadV1"
RUNTIME_MAPPING_PARTITION_KEY_PATH = "/deployment_id"


class CosmosRuntimeDeploymentMappingReader:
    """Durable RUNTIME reader: a fresh exact point read, and nothing else."""

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


class CosmosRuntimeDeploymentMappingStore:
    """Durable CONTROL-PLANE adapter over a ``/deployment_id`` container.

    Holds a ``CosmosRuntimeDeploymentMappingReader`` over the SAME container
    (composition) and exposes head/enumerate/write only -- deliberately NO ``get``
    of its own, so it cannot satisfy the runtime reader Protocol and a mis-wire is
    a mypy error. Revision reads it needs internally go through ``self.reader``.

    ``commit_revision`` is a single-partition TRANSACTIONAL BATCH: bootstrap is
    ``[create revision, create head]`` (both create-only, so a concurrent
    double-bootstrap is store-adjudicated -- one batch 409s); supersede is
    ``[create revision N+1, replace head If-Match]`` (atomic, so ``next`` can
    never skip or collide). ``get_head`` is a point read of the head item;
    ``list_revisions`` is the only query and lives on the control-plane port only.

    SDK-CONTRACT-VERIFIED ASSUMPTION (do not overstate): the all-or-nothing
    atomicity of the batch rests on ``ContainerProxy.execute_item_batch`` being
    single-partition-scoped and honoring the per-op preconditions. That primitive
    was verified against the PINNED ``azure-cosmos==4.16.2``
    (``execute_item_batch(batch_operations, partition_key)`` exists and takes ONE
    ``partition_key``; ``_format_batch_operations`` honors ``if_match_etag`` ->
    ``ifMatch`` and ``if_none_match_etag`` -> ``ifNoneMatch``). Whether Cosmos
    actually applies BOTH operations or NEITHER cannot be proven by a container
    double -- only by a real service/emulator -- so tests here assert operation
    SHAPES, PRECONDITIONS, and the single partition key, never "atomicity tested".
    An ``azure-cosmos`` version bump INVALIDATES this verification and MUST re-run
    it, because the whole succession design rests on this primitive's semantics.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container
        self._reader = CosmosRuntimeDeploymentMappingReader(container)

    @property
    def reader(self) -> RuntimeDeploymentMappingReader:
        return self._reader

    def get_head(self, deployment_id: str) -> RuntimeDeploymentHead | None:
        try:
            document = dict(self._container.read_item(item=_head_item_id(deployment_id), partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
        claim = document.get("bound_client_app_id")
        state = document.get("claim_state")
        return RuntimeDeploymentHead(
            deployment_id=deployment_id,
            current_sequence=int(document["current_sequence"]),
            current_revision_id=str(document["current_revision_id"]),
            bound_client_app_id=None if claim is None else str(claim),
            claim_state=None if state is None else RuntimeHeadClaimState(str(state)),
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

    def _head_document(self, mapping: RuntimeDeploymentMapping, claim_state: RuntimeHeadClaimState) -> dict[str, Any]:
        return {
            "id": _head_item_id(mapping.deployment_id),
            "documentType": RUNTIME_MAPPING_HEAD_DOCUMENT_TYPE,
            "deployment_id": mapping.deployment_id,
            "current_sequence": mapping.revision_sequence,
            "current_revision_id": mapping.revision_id,
            "bound_client_app_id": _bound_client(mapping),
            "claim_state": claim_state.value,
        }

    def commit_revision(self, mapping: RuntimeDeploymentMapping, *, expected_head_sequence: int | None) -> None:
        bound_client = _bound_client(mapping)
        existing_rev = self._reader.get(mapping.deployment_id, mapping.revision_sequence)
        head = self.get_head(mapping.deployment_id)
        if (
            existing_rev is not None
            and existing_rev.mapping_digest == mapping.mapping_digest
            and head is not None
            and head.current_sequence == mapping.revision_sequence
            and head.current_revision_id == mapping.revision_id
            and head.bound_client_app_id == bound_client
        ):
            return
        # Deployment->one-client claim (CAS from null): a DIFFERENT non-null claim
        # means the deployment is legitimately held by another client -> refuse.
        if head is not None and head.bound_client_app_id is not None and head.bound_client_app_id != bound_client:
            raise RuntimeHeadClaimError(
                f"deployment '{mapping.deployment_id}' is claimed by client '{head.bound_client_app_id}'; "
                f"'{bound_client}' cannot claim it (revoke the current client first)."
            )
        revision_doc = self._revision_document(mapping)
        head_doc = self._head_document(mapping, _next_claim_state(head, bound_client))
        # ONE batch shape (two ops, one partition key, all-or-nothing), TWO head
        # preconditions -- never an unconditional upsert on the head (a bare upsert
        # would silently clobber a concurrent head advance with NO 412, destroying
        # monotonicity). BOOTSTRAP: the head op is a create-only ``create`` (lands
        # only if ABSENT). SUPERSEDE: the head op is a ``replace`` with If-Match
        # (lands only if UNCHANGED). Both abort the whole batch on their
        # precondition, so head can neither be skipped nor overwritten.
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
                existing_rev = self._reader.get(mapping.deployment_id, mapping.revision_sequence)
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

    def finalize_head_claim(self, deployment_id: str, *, expected_client: str) -> None:
        head_raw = self._read_head_raw(deployment_id)
        client = None if head_raw is None else head_raw.get("bound_client_app_id")
        state = None if head_raw is None else head_raw.get("claim_state")
        if client == expected_client and state == RuntimeHeadClaimState.BOUND.value:
            return  # idempotent -- already finalized
        if head_raw is None or client != expected_client or state != RuntimeHeadClaimState.CLAIMING.value:
            raise RuntimeHeadClaimError(
                f"cannot finalize claim on '{deployment_id}': not CLAIMING('{expected_client}') "
                f"(observed {(client, state)})."
            )
        finalized = dict(head_raw)
        finalized["claim_state"] = RuntimeHeadClaimState.BOUND.value
        self._replace_head_or_claim_error(deployment_id, finalized, str(head_raw["_etag"]))

    def clear_head_claim(
        self, deployment_id: str, *, expected_client: str, expected_state: RuntimeHeadClaimState
    ) -> None:
        head_raw = self._read_head_raw(deployment_id)
        if head_raw is None or head_raw.get("bound_client_app_id") is None:
            return  # nothing to clear (idempotent)
        if str(head_raw["bound_client_app_id"]) != expected_client or head_raw.get("claim_state") != (
            expected_state.value
        ):
            raise RuntimeHeadClaimError(
                f"cannot clear claim on '{deployment_id}': expected {expected_state.value}('{expected_client}'), "
                f"observed {(head_raw.get('bound_client_app_id'), head_raw.get('claim_state'))}."
            )
        cleared = dict(head_raw)
        cleared["bound_client_app_id"] = None
        cleared["claim_state"] = None
        self._replace_head_or_claim_error(deployment_id, cleared, str(head_raw["_etag"]))

    def _replace_head_or_claim_error(self, deployment_id: str, body: dict[str, Any], etag: str) -> None:
        try:
            self._container.replace_item(
                item=body["id"], body=body, etag=etag, match_condition=MatchConditions.IfNotModified
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code == 412:
                raise RuntimeHeadClaimError(
                    f"claim on '{deployment_id}' was modified concurrently; re-read and retry."
                ) from exc
            raise

    def _read_head_raw(self, deployment_id: str) -> dict[str, Any] | None:
        try:
            return dict(self._container.read_item(item=_head_item_id(deployment_id), partition_key=deployment_id))
        except CosmosResourceNotFoundError:
            return None
