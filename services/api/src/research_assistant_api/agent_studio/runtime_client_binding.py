"""Server-owned client-to-deployment authority for runtime auth.

Authority over *which* deployment an authenticated client may touch is
server-owned and resolved **before** any mapping is read. This closes the
oracle where a role-holding-but-unauthorized caller could point-read arbitrary
deployment partitions and use the in-mapping allowlist as the sole gate.

Ratified design constraints (do not reinvent):

1. **Exact membership, never selection.** One client identity is bound to
   exactly ONE deployment (one-to-one; multi-deployment scenarios require
   separate app registrations). Authorization is an *exact membership test* on
   the pair ``(authenticated_client_app_id, asserted_deployment_id)``: the
   asserted deployment is an INPUT, never a value the resolver returns. There is
   deliberately no ``authorized_deployment_id(client) -> deployment`` path (one
   refactor away from being used as a *source* for ``deployment_id``); the
   resolver only ever answers "is this pair bound, and which revision is
   current?". A caller that omits/mismatches the deployment is denied.
2. **Keyed by the authenticated client alone.** The index is partitioned by and
   point-read by ``client_app_id`` -- never queried by ``deployment_id`` or
   scanned, and the role is NEVER part of the key. Roles come only from the
   validated token; putting a role in the index key would make the index a
   second, divergent source of role authority.
3. **No timing padding across not-bound vs no-such-mapping.** An unbound caller
   must never touch the mapping container at all (that is correct and
   desirable). We deliberately do NOT equalize timing between "not bound" and
   "bound but mapping absent" -- the residual difference only distinguishes
   states inside the caller's own authorized set, which it already knows.
4. **Writes are control-plane only.** The index is the one *mutable* authority
   here, so its write path is the crown jewel. ``ClientDeploymentBindingResolver``
   (read-only ``resolve_binding``) is the ONLY surface the runtime plane is
   given; grants/revocations live on ``ClientDeploymentBindingWriter``, exercised
   only by the human-authorized control-plane deployment/release path. The
   runtime app-role identity must have read-only data-plane access to the index
   (see the IaC RBAC for the durable adapter); it can never write a binding, so
   an attacker who can land a mapping cannot also land a binding.
5. **Fail-closed ordering (no cross-container atomicity).** Binding partition is
   ``client_app_id`` and mapping partition is ``deployment_id``; no Cosmos
   transactional batch spans them. The producer's GRANT writes the mapping
   revision FIRST then repoints the binding (a binding never points at a missing
   revision); REVOKE is a SINGLE CAS status-flip of the binding to a REVOKED
   tombstone (authority withdrawn immediately; no new mapping revision is
   written). Revocation NEVER hard-deletes the row, but NOT (as an earlier
   rationale claimed) to preserve a succession counter -- under the head-record
   ruling the per-deployment HEAD owns succession, so a deleted binding row would
   NOT break ``next``. The tombstone is still required for two DIFFERENT reasons:
   (a) it preserves the audit record of the revocation, and (b) it keeps "absent"
   unambiguous -- without it a revoked client and a never-granted client are
   indistinguishable (present-and-ACTIVE = bound, present-and-REVOKED = tombstoned,
   absent = never granted). The row denies on ``status != ACTIVE``. Revocation is
   also TERMINAL: no repoint may resurrect a REVOKED binding to ACTIVE, so a
   SUPERSEDE racing a REVOKE on the same row can never silently undo the
   revocation (see ``_validate_repoint_admission``); the ONE sanctioned
   REVOKED->ACTIVE transition is the explicit, separately-audited ``reinstate``,
   which points at the CURRENT head, never the tombstone's retained sequence.
6. **Access is not usability.** A present binding authorizes *access* only.
   After the binding check and mapping load, the mapping's creation-time window
   still applies (``lifecycle_fault``: not-yet-effective/expired both still deny);
   revocation and supersession-staleness are enforced by the binding (a non-ACTIVE
   binding denies via the loader; a stale presented revision denies in
   ``runtime_authz``). A binding never short-circuits any of these checks.

This module defines the read-only resolver protocol, the control-plane writer
protocol, the ``BindingResolution`` the resolver returns, an in-memory index
implementing both, the fail-closed loader, and a durable Cosmos adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol, cast

from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from pydantic import BaseModel, ConfigDict

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingReader

#: A loader taking (trusted_client_app_id, asserted_deployment_id) and returning
#: the authorized mapping or ``None`` (uniformly, without leaking why).
AuthorizedMappingLoader = Callable[[str, str], RuntimeDeploymentMapping | None]


class RuntimeBindingStatus(StrEnum):
    """Lifecycle status of a client->deployment binding row.

    ``ACTIVE`` is the only status a live binding carries. ``REVOKED`` is a
    soft-revoked tombstone: a revoked binding is present but denies (the loader
    treats a non-ACTIVE resolution as a denial, without reading the mapping).
    """

    ACTIVE = "active"
    REVOKED = "revoked"


class BindingResolution(BaseModel):
    """The result of an exact ``(client, asserted_deployment)`` membership test.

    Carries WHICH revision of the asserted deployment is current -- both the
    content-addressed ``revision_id`` (a digest pin: repointing to a different
    document changes it) and the monotonic ``revision_sequence`` the control
    plane uses to reject a rollback -- plus the binding ``status``. It never
    carries a deployment the caller did not assert. The store then performs an
    exact ``(deployment_id, revision_id)`` point read.

    On a REVOKED tombstone, ``revision_sequence``/``revision_id`` are AUDIT-ONLY:
    they record which revision the client was on WHEN it was revoked. Since the
    per-deployment HEAD owns succession, NOTHING may derive succession or a
    re-grant target from a tombstone's retained sequence -- an explicit re-grant
    (``reinstate``) points at the CURRENT head, and uses the retained sequence
    only as the CAS precondition (a concurrency mechanism), never as the target.

    It deliberately does NOT contain ``deployment_id``: the asserted deployment is
    strictly an INPUT the caller already holds, and echoing it back -- even
    redundantly -- is one refactor away from being read as the authoritative
    "which deployment applies to you", which is the exact oracle the whole
    server-owned-binding design closes. The caller passes the deployment it
    asserted and re-uses its own copy for the subsequent point read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    revision_id: str
    revision_sequence: int
    status: RuntimeBindingStatus


class ClientDeploymentBindingResolver(Protocol):
    """Read-only authority the runtime plane depends on: exact membership only."""

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        """Resolve the binding for the exact ``(client, asserted_deployment)`` pair, else ``None``.

        Returns ``None`` when the client is not bound to *this* asserted
        deployment (unbound, or bound to a different deployment). When the pair
        is bound it returns the current revision + status; it never selects or
        returns a deployment the caller did not assert.
        """
        ...


class BindingPreconditionError(RuntimeError):
    """Raised when a CAS repoint's expected-current precondition no longer holds.

    A concurrent control-plane writer moved the binding between the caller's read
    and its conditional write. The caller must RE-READ the current pointer and
    retry (never blind-retry), so a stale forward-or-backward move can never win
    a lost-update race.
    """


class NonMonotonicRepointError(RuntimeError):
    """Raised when a repoint is not a monotonic advance of the observed current.

    A repoint may only advance a (client, deployment) binding to a strictly
    greater sequence (or re-affirm the identical current revision, idempotently).
    A lower, or equal-but-different, sequence is refused -- this is the rollback
    control, evaluated INSIDE the CAS against the observed current.
    """


class RevokedBindingResurrectionError(RuntimeError):
    """Raised when a repoint would resurrect a REVOKED binding to ACTIVE.

    Revocation is TERMINAL and wins all ties: once a binding row is a REVOKED
    tombstone, no repoint may turn it back to ACTIVE. This closes a fail-open in
    which a SUPERSEDE CAS races a REVOKE on the SAME binding row -- because REVOKE
    keeps the sequence, a CAS/monotonic re-check alone would see current=N,
    target=N+1 and write back an ACTIVE binding, silently undoing the revocation.
    The full admission check (run on the first attempt AND every CAS retry) treats
    a REVOKED current as terminal, so no retry path can resurrect it. Re-granting
    a revoked client is an EXPLICIT control-plane GRANT with its own intent/audit,
    never a side effect of a supersede or a retry.
    """


class ReinstateStateError(RuntimeError):
    """Raised when ``reinstate`` is asked to reinstate a binding that is not a
    REVOKED tombstone (absent, or already ACTIVE). Reinstate is the ONE sanctioned
    REVOKED->ACTIVE transition and applies only to a revoked row; a fresh grant of
    a never-revoked client is an ordinary ``repoint``, not a reinstate."""


class CrossDeploymentBindingError(RuntimeError):
    """Raised when a write would bind a client to a DIFFERENT deployment than its
    existing row -- violating the locked 1:1 cardinality (one client bound to
    exactly one deployment, for its lifetime).

    Each Hosted Agent deployment has its OWN managed identity, so a single
    ``client_app_id`` belongs to exactly one deployment by construction; a write
    re-targeting it to another deployment is a misconfiguration and exactly the
    shared-runtime-spanning-deployments blast-radius case the contract forbids.
    Enforced at the WRITER (the control-plane mutation surface), NOT the resolver:
    the resolver already returns ``None`` for a wrong-deployment pair, but only the
    writer can refuse to CREATE the cross-deployment state in the first place.
    """


class ClientDeploymentBindingWriter(Protocol):
    """Control-plane-only mutation surface (never handed to the runtime plane)."""

    def repoint(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        status: RuntimeBindingStatus,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        """CAS repoint of ``client_app_id``'s binding to the given revision + status.

        Conditional on the currently-stored sequence for this (client,
        deployment) equalling ``expected_current_sequence`` (``None`` meaning the
        caller expects no current binding for this deployment). Raises
        ``BindingPreconditionError`` if that precondition fails (concurrent
        modification), ``NonMonotonicRepointError`` if the new sequence is not a
        NON-DECREASING advance of -- or an idempotent re-affirmation of -- the
        observed current, and ``RevokedBindingResurrectionError`` if it would flip
        a REVOKED tombstone back to ACTIVE (revocation is terminal). The FULL
        admission check runs atomically with the write, and is re-run in its
        entirety on every CAS retry -- never just the condition that failed.

        ``status`` is ``ACTIVE`` for a grant and ``REVOKED`` for a revocation
        TOMBSTONE. Revocation NEVER hard-deletes the row -- NOT to preserve a
        succession counter (the HEAD owns succession now), but to keep the
        revocation AUDIT RECORD and to keep "absent" unambiguous (a revoked client
        must remain distinguishable from a never-granted one). The row denies on
        ``status != ACTIVE``.
        """
        ...

    def reinstate(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        """The ONE sanctioned REVOKED->ACTIVE transition: explicit, audited re-grant.

        Distinct from ``repoint`` (which is terminal on a REVOKED current) so the
        supersede/retry path can never reach it. The current binding MUST be a
        REVOKED tombstone (else ``ReinstateStateError``); the CAS is conditional on
        ``expected_current_sequence`` matching the tombstone's retained sequence;
        and the new sequence must be NON-DECREASING relative to that retained
        sequence -- the caller points at the CURRENT head revision, never behind
        the tombstone (pointing behind it would roll the re-granted client back to
        its pre-revocation revision). On success the row becomes ACTIVE at the
        given (revision_sequence, revision_id).
        """
        ...


def _validate_repoint_admission(
    *,
    observed_sequence: int | None,
    observed_revision_id: str | None,
    observed_status: RuntimeBindingStatus | None,
    new_sequence: int,
    new_revision_id: str,
    new_status: RuntimeBindingStatus,
) -> None:
    """Run the FULL repoint admission check, atomically with the CAS.

    NOTE ON THE TWO DISTINCT MONOTONICITY RULES (deliberately NOT shared): the
    per-deployment HEAD is STRICTLY INCREASING (next == current + 1; a repeat is a
    duplicate-sequence conflict) and that rule lives in the store/producer, NOT
    here. A per-client BINDING's ``current_sequence`` is only NON-DECREASING: it
    may legitimately stay EQUAL (an idempotent re-affirmation of the same head
    revision) or jump ahead (a lagging client catching up), so this check must
    never demand a strict successor. Sharing one comparison helper between the two
    is exactly how a ``>`` vs ``>=`` bug arrives at re-grant, so the binding rule
    is kept in this function and the head rule in the succession store.

    On the first attempt AND on every CAS retry, the caller re-reads the current
    binding and re-runs this ENTIRE check -- never just the condition that failed.
    Two rules, in order:

    1. **Revocation is terminal (wins all ties).** If the observed current binding
       is a REVOKED tombstone, the only permitted target is REVOKED again
       (idempotent). Any repoint to ACTIVE is refused
       (``RevokedBindingResurrectionError``). This is what stops a SUPERSEDE that
       races a REVOKE on the same row from writing an ACTIVE binding back: because
       REVOKE keeps the sequence, the monotonic check alone would accept N -> N+1
       and silently undo the revocation. Re-granting a revoked client is the
       explicit, separately-audited ``reinstate``, never a repoint/supersede side
       effect.
    2. **Non-decreasing advance (or idempotent re-affirmation).** A binding row
       answers only "which revision may THIS client see" and may legitimately LAG
       the succession head during a partial multi-client repoint, so it need not
       be a STRICT successor -- a client at N may jump straight to N+2 if it missed
       a supersession. It may only ever be NON-DECREASING, though: a first binding
       (``observed_sequence is None``) may take any sequence; otherwise the new
       sequence must be greater than the observed one, unless it re-affirms the
       identical current revision (same sequence AND same digest), which is
       idempotent. A lower sequence, or an equal sequence with different content,
       is a rollback and is refused. (Strict single-succession is enforced at the
       head/succession record, not per binding.)
    """
    if observed_status is RuntimeBindingStatus.REVOKED and new_status is not RuntimeBindingStatus.REVOKED:
        raise RevokedBindingResurrectionError(
            f"repoint to sequence {new_sequence} would resurrect a REVOKED binding to '{new_status.value}'; "
            "revocation is terminal (re-granting a revoked client is the explicit reinstate operation)."
        )
    if observed_sequence is None:
        return
    is_idempotent = new_sequence == observed_sequence and new_revision_id == observed_revision_id
    is_advance = new_sequence > observed_sequence
    if not (is_idempotent or is_advance):
        raise NonMonotonicRepointError(
            f"repoint to sequence {new_sequence} does not advance the current binding at {observed_sequence}."
        )


def _validate_reinstate_admission(
    *,
    observed_status: RuntimeBindingStatus | None,
    observed_sequence: int | None,
    new_sequence: int,
) -> None:
    """Admission for the explicit REVOKED->ACTIVE reinstate (distinct from repoint).

    Requires the current binding to be a REVOKED tombstone (only a revoked row can
    be reinstated). The target (the CURRENT head revision) must be NON-DECREASING
    relative to the tombstone's retained sequence -- equal if the head has not
    advanced since revocation, or greater if it has, but NEVER behind it, which
    would roll the re-granted client back to its pre-revocation revision.
    """
    if observed_status is not RuntimeBindingStatus.REVOKED or observed_sequence is None:
        raise ReinstateStateError(
            f"reinstate requires a REVOKED tombstone, observed status "
            f"{None if observed_status is None else observed_status.value}."
        )
    if new_sequence < observed_sequence:
        raise NonMonotonicRepointError(
            f"reinstate to sequence {new_sequence} points behind the revoked tombstone at {observed_sequence}."
        )


def build_authorized_mapping_loader(
    resolver: ClientDeploymentBindingResolver,
    mapping_store: RuntimeDeploymentMappingReader,
) -> AuthorizedMappingLoader:
    """Compose a read-only binding resolver + mapping store into an authorized loader.

    Authorizes the ``(client, asserted_deployment)`` binding by exact membership
    FIRST; an unbound (or wrong-deployment) caller returns ``None`` immediately
    WITHOUT touching the mapping container (constraint 3 -- zero mapping reads).
    A soft-revoked binding (status != ACTIVE) also denies WITHOUT a mapping read.
    Only an ACTIVE binding point-reads the EXACT current revision the binding
    supplies -- the index supplies WHICH revision, never WHICH deployment (the
    resolution carries no ``deployment_id``; the point read uses the caller's own
    asserted deployment). A binding pointing at an absent revision also returns
    ``None`` (fail-closed reconciliation, constraint 5). No selection, no default,
    no enumeration.
    """

    def _load(client_app_id: str, asserted_deployment_id: str) -> RuntimeDeploymentMapping | None:
        resolution = resolver.resolve_binding(client_app_id, asserted_deployment_id)
        if resolution is None:
            return None
        if resolution.status is not RuntimeBindingStatus.ACTIVE:
            return None
        # The asserted deployment is the caller's OWN input; the resolution never
        # echoes a deployment back, so it can never redirect the point read.
        mapping = mapping_store.get(asserted_deployment_id, resolution.revision_sequence)
        if mapping is None:
            return None
        # A1 digest pin: the binding pins the target digest, so a store revision
        # whose content does not match the pinned digest (tampering, or an
        # out-of-band overwrite) is a denial, never trusted.
        if mapping.revision_id != resolution.revision_id:
            return None
        return mapping

    return _load


#: Cosmos ``documentType`` discriminator and partition-key path for the durable
#: client->deployment binding index. The container is partitioned by
#: ``/client_app_id`` and each client has exactly ONE item (id == client_app_id)
#: so a runtime authorization is a single-partition ``read_item`` keyed by the
#: AUTHENTICATED client (constraint 2) -- never a query by ``deployment_id``,
#: never a scan, and the role is never part of the key.
RUNTIME_BINDING_DOCUMENT_TYPE = "runtimeClientDeploymentBindingV1"
RUNTIME_BINDING_PARTITION_KEY_PATH = "/client_app_id"


class CosmosClientDeploymentBindingIndex:
    """Durable one-to-one binding index partitioned by ``/client_app_id``.

    Implements both the read-only resolver and the control-plane writer, but the
    two surfaces are governed by *different* data-plane identities in IaC: the
    runtime app-role identity is granted Cosmos **Data Reader** on this
    container (it only ever calls ``resolve_binding``), while grants/revocations
    run under the control-plane identity with **Data Contributor**. The type
    split here is the code half; the RBAC split is the enforcement half.

    ``resolve_binding`` is a fresh single-partition point ``read_item`` (404 ->
    ``None``); it returns ``None`` unless the stored row's ``deployment_id``
    equals the asserted one, so it can never redirect the caller. ``repoint`` is
    a CAS write: it reads the current row (+ ETag), checks the observed sequence
    against ``expected_current_sequence`` and the full repoint admission (monotonic
    advance + terminal revocation), then
    writes conditionally (``create_item`` when there is no current row, else
    ``replace_item`` with ``If-Match`` on the ETag). Because the ETag covers the
    whole row, a REVOKE that keeps the sequence still bumps the ETag, so a racing
    SUPERSEDE's conditional write fails (412), re-reads the now-REVOKED row, and
    is refused by the terminal-revocation admission rather than resurrecting it. A
    concurrent modification (409 on create, 412 on replace) surfaces as
    ``BindingPreconditionError`` for the caller to re-read and retry -- so two
    overlapping repoints can never lost-update a rollback in. Revocation is a
    ``repoint`` to a ``REVOKED`` TOMBSTONE (never a hard delete) -- kept not for a
    succession counter (the HEAD owns succession) but for the revocation audit
    record and to keep "absent" unambiguous. ``reinstate`` is the ONE sanctioned
    REVOKED->ACTIVE transition (explicit, separately audited), pointing at the
    current head, never the tombstone's retained sequence.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def repoint(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        status: RuntimeBindingStatus,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        document = self._read(client_app_id)
        if (
            document is not None
            and document.get("deployment_id") != deployment_id
            and str(document.get("status")) == RuntimeBindingStatus.ACTIVE.value
        ):
            raise CrossDeploymentBindingError(
                f"client '{client_app_id}' is already ACTIVELY bound to '{document.get('deployment_id')}'; "
                f"it cannot be bound to '{deployment_id}' (1:1 cardinality). Revoke the existing "
                "binding first (a revoked tombstone does not block a later grant)."
            )
        observed_sequence: int | None = None
        observed_revision_id: str | None = None
        observed_status: RuntimeBindingStatus | None = None
        if document is not None and document.get("deployment_id") == deployment_id:
            observed_sequence = int(str(document["current_revision_sequence"]))
            observed_revision_id = str(document["current_revision_id"])
            observed_status = RuntimeBindingStatus(str(document["status"]))
        if observed_sequence != expected_current_sequence:
            raise BindingPreconditionError(
                f"binding for '{client_app_id}' changed under the repoint "
                f"(expected current sequence {expected_current_sequence}, observed {observed_sequence})."
            )
        _validate_repoint_admission(
            observed_sequence=observed_sequence,
            observed_revision_id=observed_revision_id,
            observed_status=observed_status,
            new_sequence=revision_sequence,
            new_revision_id=revision_id,
            new_status=status,
        )
        body = {
            "id": client_app_id,
            "documentType": RUNTIME_BINDING_DOCUMENT_TYPE,
            "client_app_id": client_app_id,
            "deployment_id": deployment_id,
            "current_revision_id": revision_id,
            "current_revision_sequence": revision_sequence,
            "status": status.value,
        }
        try:
            if document is None:
                self._container.create_item(body)
            else:
                self._container.replace_item(
                    item=client_app_id,
                    body=body,
                    etag=str(document["_etag"]),
                    match_condition=MatchConditions.IfNotModified,
                )
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise BindingPreconditionError(
                    f"binding for '{client_app_id}' was modified concurrently; re-read and retry."
                ) from exc
            raise

    def reinstate(
        self,
        client_app_id: str,
        deployment_id: str,
        revision_sequence: int,
        revision_id: str,
        *,
        expected_current_sequence: int | None,
    ) -> None:
        document = self._read(client_app_id)
        if document is not None and document.get("deployment_id") != deployment_id:
            raise CrossDeploymentBindingError(
                f"client '{client_app_id}' is already bound to '{document.get('deployment_id')}'; "
                f"it cannot be reinstated onto '{deployment_id}' (1:1 cardinality)."
            )
        observed_sequence: int | None = None
        observed_status: RuntimeBindingStatus | None = None
        observed_etag: str | None = None
        if document is not None and document.get("deployment_id") == deployment_id:
            observed_sequence = int(str(document["current_revision_sequence"]))
            observed_status = RuntimeBindingStatus(str(document["status"]))
            observed_etag = str(document["_etag"])
        if observed_sequence != expected_current_sequence:
            raise BindingPreconditionError(
                f"binding for '{client_app_id}' changed under the reinstate "
                f"(expected current sequence {expected_current_sequence}, observed {observed_sequence})."
            )
        # A reinstate always REPLACES an existing REVOKED tombstone (never creates);
        # the admission below rejects any non-REVOKED observed status, so a matching
        # tombstone -- and therefore ``observed_etag`` -- is guaranteed past it.
        _validate_reinstate_admission(
            observed_status=observed_status, observed_sequence=observed_sequence, new_sequence=revision_sequence
        )
        body = {
            "id": client_app_id,
            "documentType": RUNTIME_BINDING_DOCUMENT_TYPE,
            "client_app_id": client_app_id,
            "deployment_id": deployment_id,
            "current_revision_id": revision_id,
            "current_revision_sequence": revision_sequence,
            "status": RuntimeBindingStatus.ACTIVE.value,
        }
        try:
            self._container.replace_item(
                item=client_app_id,
                body=body,
                etag=cast(str, observed_etag),
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code in (409, 412):
                raise BindingPreconditionError(
                    f"binding for '{client_app_id}' was modified concurrently; re-read and retry."
                ) from exc
            raise

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        document = self._read(client_app_id)
        if document is None or document.get("deployment_id") != asserted_deployment_id:
            return None
        return BindingResolution(
            revision_id=str(document["current_revision_id"]),
            revision_sequence=int(str(document["current_revision_sequence"])),
            status=RuntimeBindingStatus(str(document["status"])),
        )

    def _read(self, client_app_id: str) -> dict[str, object] | None:
        try:
            return dict(self._container.read_item(item=client_app_id, partition_key=client_app_id))
        except CosmosResourceNotFoundError:
            return None
