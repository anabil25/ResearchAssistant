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


class RuntimeHeadClaimError(RuntimeDeploymentMappingStoreError):
    """Raised when a grant would claim a deployment already claimed by another client.

    The head carries ``bound_client_app_id`` -- the deployment->one-client half of
    the BIJECTIVE cardinality, set by CAS FROM NULL. A grant may claim it only when
    it is NULL (a fresh/migrated deployment) or already equals this client (an
    idempotent same-client supersede). A DIFFERENT non-null claim means the
    deployment is legitimately held by another client -- refused. Because this is a
    single-partition conditional write on the head, the deployment direction is
    store-adjudicated without a cross-partition query over the binding container.
    """


def _revision_item_id(deployment_id: str, revision_sequence: int) -> str:
    return f"{deployment_id}:{revision_sequence}"


def _head_item_id(deployment_id: str) -> str:
    return f"{deployment_id}:head"


def _bound_client(mapping: RuntimeDeploymentMapping) -> str:
    """The deployment's sole authorized client (bijective 1:1).

    Under bijective cardinality a mapping authorizes EXACTLY ONE client (the
    control-plane producer validates this before commit), so the head's
    ``bound_client_app_id`` claim is the first (only) allowed client.
    """
    return mapping.allowed_client_app_role_bindings[0].client_app_id


class RuntimeHeadClaimState(StrEnum):
    """Phase of the head's deployment->client claim (see ``RuntimeDeploymentHead``).

    ``CLAIMING`` = a grant claimed the head but has not confirmed its binding;
    ``BOUND`` = the grant created its binding and finalized. Only ``BOUND`` means a
    fully-completed grant; a reaper distinguishes the two so it never reaps an
    in-flight grant.
    """

    CLAIMING = "claiming"
    BOUND = "bound"


def _next_claim_state(
    head: RuntimeDeploymentHead | None, bound_client: str
) -> RuntimeHeadClaimState:
    """The head claim phase to write on ``commit_revision``.

    A FRESH claim (no head, or an unclaimed one) starts ``CLAIMING`` and the grant
    finalizes it to ``BOUND``. A same-client commit KEEPS the current phase -- a
    same-client supersede over a ``BOUND`` head stays ``BOUND`` (no finalize needed),
    and a retried fresh grant over its own ``CLAIMING`` head stays ``CLAIMING``.
    (A different-client claim is refused before this is reached.)
    """
    if head is None or head.bound_client_app_id is None or head.claim_state is None:
        return RuntimeHeadClaimState.CLAIMING
    return head.claim_state


class RuntimeDeploymentHead(BaseModel):
    """The mutable per-deployment succession pointer + TWO-PHASE bound-client claim.

    Besides the succession pointer (``current_sequence``/``current_revision_id``),
    the head carries the deployment->one-client claim as a TWO-PHASE value so that
    GRANT COMPLETION IS VISIBLE ON THE HEAD, closing a reconciliation fail-open: a
    reaper cannot otherwise tell a CRASHED grant (claimed head, no binding) from a
    SLOW one (claimed head, binding not written YET). The phases:

    * ``CLAIMING(client)`` -- a grant CAS'd the head from null but has not yet
      confirmed its binding (the FIRST of the three grant writes).
    * ``BOUND(client)`` -- the grant created its binding and finalized the head
      (the THIRD write). Only ``BOUND`` means the grant fully completed.
    * ``None`` -- unclaimed (before bootstrap, or after revoke/reconcile cleared
      it); grantable to any otherwise-eligible client.

    Invariant: ``bound_client_app_id`` and ``claim_state`` are both ``None`` or
    both set. A reaper reconciling a dangling claim clears a ``CLAIMING`` head with
    a CAS on that exact state, so a concurrently FINALIZING grant (``CLAIMING`` ->
    ``BOUND``) makes the reaper's CAS fail -- exactly one wins.
    """

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


class InMemoryRuntimeDeploymentMappingReader:
    """In-memory RUNTIME reader: exposes ONLY ``get`` over a shared revisions dict."""

    def __init__(self, revisions: dict[str, RuntimeDeploymentMapping]) -> None:
        self._revisions = revisions

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        return self._revisions.get(_revision_item_id(deployment_id, revision_sequence))


class InMemoryRuntimeDeploymentMappingStore:
    """In-memory CONTROL-PLANE adapter (tests/local only).

    Holds a runtime reader over the SAME ``_revisions`` dict (composition), and
    exposes head/enumerate/write only -- deliberately NO ``get`` of its own, so it
    cannot be mis-passed where a runtime reader is expected.
    """

    def __init__(self) -> None:
        self._revisions: dict[str, RuntimeDeploymentMapping] = {}
        self._heads: dict[str, RuntimeDeploymentHead] = {}
        self._reader = InMemoryRuntimeDeploymentMappingReader(self._revisions)

    @property
    def reader(self) -> RuntimeDeploymentMappingReader:
        return self._reader

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
        bound_client = _bound_client(mapping)
        existing_rev = self._revisions.get(item_id)
        head = self._heads.get(mapping.deployment_id)
        # Idempotent replay: the revision AND the head (incl. its claim) already
        # reflect this exact commit -> no-op success (a retried, fully-applied commit).
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
        # A FRESH claim (no prior head, or an unclaimed one) starts in CLAIMING and
        # is finalized to BOUND by the grant's third write; a same-client supersede
        # KEEPS the current phase (usually BOUND) -- the deployment is already held.
        new_state = _next_claim_state(head, bound_client)
        self._heads[mapping.deployment_id] = RuntimeDeploymentHead(
            deployment_id=mapping.deployment_id,
            current_sequence=mapping.revision_sequence,
            current_revision_id=mapping.revision_id,
            bound_client_app_id=bound_client,
            claim_state=new_state,
        )

    def delete(self, deployment_id: str, revision_sequence: int) -> None:
        self._revisions.pop(_revision_item_id(deployment_id, revision_sequence), None)

    def finalize_head_claim(self, deployment_id: str, *, expected_client: str) -> None:
        head = self._heads.get(deployment_id)
        if head is not None and head.bound_client_app_id == expected_client and head.claim_state is (
            RuntimeHeadClaimState.BOUND
        ):
            return  # idempotent -- already finalized
        if head is None or head.bound_client_app_id != expected_client or head.claim_state is not (
            RuntimeHeadClaimState.CLAIMING
        ):
            raise RuntimeHeadClaimError(
                f"cannot finalize claim on '{deployment_id}': not CLAIMING('{expected_client}') "
                f"(observed {None if head is None else (head.bound_client_app_id, head.claim_state)})."
            )
        self._heads[deployment_id] = head.model_copy(update={"claim_state": RuntimeHeadClaimState.BOUND})

    def clear_head_claim(
        self, deployment_id: str, *, expected_client: str, expected_state: RuntimeHeadClaimState
    ) -> None:
        head = self._heads.get(deployment_id)
        if head is None or head.bound_client_app_id is None:
            return  # idempotent -- already cleared
        if head.bound_client_app_id != expected_client or head.claim_state is not expected_state:
            raise RuntimeHeadClaimError(
                f"cannot clear claim on '{deployment_id}': expected {expected_state.value}('{expected_client}'), "
                f"observed {(head.bound_client_app_id, head.claim_state)}."
            )
        self._heads[deployment_id] = head.model_copy(
            update={"bound_client_app_id": None, "claim_state": None}
        )


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
