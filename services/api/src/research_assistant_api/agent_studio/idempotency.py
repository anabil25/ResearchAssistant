"""Durable, cross-instance-safe idempotency claim/lease/completion port.

This answers a different question from ``approval_consumption.py``: that
module durably spends a *single-use approval grant* exactly once; this
module durably records whether *any* runtime operation (approved or not --
e.g. a plain ``WRITE_REVERSIBLE`` tool call that must not double-execute on
client retry) has already run, is currently running, or has already
completed/failed, independent of any approval concept.

The domain shape here is this backend's own, independent design --
structurally aligned (never imported from, or into) with the harness's own
``agents.shared.idempotency.IdempotencyStore`` contract (inspected read-only
at commit ``cef9975`` on their branch purely for shape/semantics alignment):
``IdempotencyState``/``IdempotencyClaimDisposition`` mirror their
``IdempotencyState``/``ClaimDisposition`` state machine; ``claim``/
``mark_in_progress``/``complete``/``fail``/``load_result`` mirror their five
async operations. Two deliberate deviations, both justified by this being an
independently-designed adapter rather than a copy:

* ``load_result`` here takes an explicit ``scope: ScopeContext`` *and* the
  full ``IdempotencyKey`` plus the caller's asserted ``release_id`` -- never
  a bare ``result_ref`` string, which on its own carries no binding to any
  specific key or release. Every read is always partitioned by the
  *caller's own authorized* ``scope.scope_key`` and re-derives ``result_ref``
  from the durable record found by ``key`` -- never from a caller-supplied
  ``result_ref`` -- so cross-tenant enumeration is structurally impossible
  even if a caller somehow guesses another scope's ``result_ref``. It then
  independently re-verifies release provenance (``record.release_id`` must
  equal the caller's asserted ``release_id``) and result integrity (the
  stored payload's independently recomputed digest must still equal
  ``record.result_hash``) before ever returning the payload, rather than
  trusting a ``COMPLETED`` state alone.
* ``complete`` here takes the raw ``result`` payload, never a caller-supplied
  ``result_hash`` -- this port independently recomputes the canonical digest
  of ``result`` itself (via ``_hash_result``) before ever calling the store,
  consistent with this codebase's "never trust a digest asserted by the
  caller" doctrine applied everywhere else (capability descriptor digests,
  scope keys, binding fingerprints).

The port is async (mirroring ``approval_consumption.ApprovalConsumptionPort``
and ``capability_discovery.CapabilityDiscoverySource``) so a future
provider-backed adapter can perform real I/O; the default
``StoreBackedIdempotencyPort`` below needs no I/O of its own but still
satisfies the async contract, and is production-safe on its own (there is no
external provider dependency for a backend-owned idempotency ledger, unlike
capability discovery).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Protocol

from research_assistant_api.agent_studio.models import (
    IdempotencyClaim,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore


class IdempotencyError(RuntimeError):
    """Base class for idempotency-port-level errors (raised before ever
    touching the durable store, or wrapping a store-layer failure)."""


class IdempotencyResultMismatchError(IdempotencyError):
    """Raised by ``complete`` when the independently recomputed digest of
    the supplied ``result`` does not match what the caller expected to
    record -- this can only happen if the caller's own bookkeeping is
    inconsistent, since the port computes the digest itself rather than
    trusting one asserted over the wire."""


class IdempotencyReleaseMismatchError(IdempotencyError):
    """Raised by ``load_result`` when the caller's asserted ``release_id``
    does not match the release that actually completed this idempotency
    key. A stale or incorrect binding replaying a *different* release's
    result must be rejected outright, never silently served -- the
    completed result is only ever a valid replay for the exact release
    that produced it."""


class IdempotencyResultIntegrityError(IdempotencyError):
    """Raised by ``load_result`` when a durably ``COMPLETED`` record's
    result cannot be trusted: either its result document is missing
    entirely, or the payload actually stored no longer hashes to the
    record's durable ``result_hash`` (tampered or corrupted). Both are
    genuine data-integrity violations -- never silently served, and never
    conflated with an ordinary "not found"/``None`` result."""


def _hash_result(result: dict[str, Any]) -> str:
    """Canonical, deterministic digest of an arbitrary JSON-able result
    payload, independent of key ordering -- mirrors the harness's own
    canonical-JSON-digest construction for this specific purpose (hashing an
    arbitrary result dict, not a fixed-field key, so ``scope.compute_scope_key``'s
    fixed-array encoding does not apply here)."""

    canonical = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyPort(Protocol):
    """Port implemented by whatever composition root wires runtime
    invocation to durable idempotency bookkeeping."""

    async def claim(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        actor_id: str,
        release_id: str,
        lease_seconds: float = 300.0,
        now: datetime | None = None,
    ) -> IdempotencyClaim: ...

    async def mark_in_progress(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        irreversible: bool = False,
        now: datetime | None = None,
    ) -> IdempotencyRecord: ...

    async def complete(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        result: dict[str, Any],
        expected_result_hash: str | None = None,
        now: datetime | None = None,
    ) -> IdempotencyRecord: ...

    async def fail(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> IdempotencyRecord: ...

    async def load_result(
        self, scope: ScopeContext, key: IdempotencyKey, *, release_id: str
    ) -> dict[str, Any] | None: ...


class StoreBackedIdempotencyPort:
    """Default, production-safe idempotency port backed directly by
    ``AgentStudioStore`` (in-memory or Cosmos-backed, transparently -- both
    satisfy the exact same atomic claim/transition contract)."""

    def __init__(self, store: AgentStudioStore) -> None:
        self._store = store

    async def claim(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        actor_id: str,
        release_id: str,
        lease_seconds: float = 300.0,
        now: datetime | None = None,
    ) -> IdempotencyClaim:
        if not (0 < lease_seconds <= 3600):
            raise ValueError("lease_seconds must be within (0, 3600].")
        return self._store.claim_idempotency(
            scope,
            key,
            actor_id=actor_id,
            release_id=release_id,
            lease_seconds=lease_seconds,
            now=now,
        )

    async def mark_in_progress(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        irreversible: bool = False,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        return self._store.mark_idempotency_in_progress(
            scope,
            key,
            claim_token=claim_token,
            expected_version=expected_version,
            irreversible=irreversible,
            now=now,
        )

    async def complete(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        result: dict[str, Any],
        expected_result_hash: str | None = None,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        """Durably record ``result`` as the completed outcome.

        ``expected_result_hash`` is an optional caller-side assertion (e.g.
        a hash the caller already computed for its own logging/telemetry
        purposes) -- if supplied, it is checked against the digest this
        port independently recomputes from ``result`` itself, catching a
        caller-side serialization/transcription bug *before* anything is
        durably written. The durably recorded ``result_hash`` is always this
        port's own recomputed digest, never the caller-supplied one.
        """

        result_hash = _hash_result(result)
        if expected_result_hash is not None and expected_result_hash != result_hash:
            raise IdempotencyResultMismatchError(
                "Caller-supplied expected_result_hash does not match the digest independently recomputed "
                "from the supplied result payload."
            )
        return self._store.complete_idempotency(
            scope,
            key,
            claim_token=claim_token,
            expected_version=expected_version,
            result=result,
            result_hash=result_hash,
            now=now,
        )

    async def fail(
        self,
        scope: ScopeContext,
        key: IdempotencyKey,
        *,
        claim_token: str,
        expected_version: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> IdempotencyRecord:
        return self._store.fail_idempotency(
            scope,
            key,
            claim_token=claim_token,
            expected_version=expected_version,
            failure_code=failure_code,
            now=now,
        )

    async def load_result(
        self, scope: ScopeContext, key: IdempotencyKey, *, release_id: str
    ) -> dict[str, Any] | None:
        """Replay a previously completed result, verifying full provenance
        before ever returning it to the caller.

        Always looks the record up by ``key`` (never trusts a caller-
        supplied ``result_ref`` as an addressing key) and re-derives
        ``result_ref`` from that record itself. Returns ``None`` only for
        the ordinary "not found"/"not yet completed" cases; a record found
        but whose provenance fails re-verification raises rather than
        silently returning ``None`` or the untrusted payload, since those
        are integrity violations, not absence.
        """

        record = self._store.get_idempotency_record(scope, key)
        if record is None or record.state is not IdempotencyState.COMPLETED:
            return None
        if record.release_id != release_id:
            raise IdempotencyReleaseMismatchError(
                f"Idempotency key digest '{key.digest}' was completed under release "
                f"'{record.release_id}', not the caller-asserted release '{release_id}'."
            )
        if record.result_ref is None or record.result_hash is None:
            # Structurally unreachable per IdempotencyRecord's own
            # model_validator (COMPLETED requires both), but fail closed
            # rather than assume the invariant instead of asserting it.
            raise IdempotencyResultIntegrityError(
                f"Idempotency key digest '{key.digest}' is COMPLETED but is missing its "
                "result_ref/result_hash identity."
            )
        result = self._store.load_idempotency_result(scope, record.result_ref)
        if result is None:
            raise IdempotencyResultIntegrityError(
                f"Idempotency key digest '{key.digest}' is durably COMPLETED but its result document "
                f"'{record.result_ref}' could not be read back."
            )
        if _hash_result(result) != record.result_hash:
            raise IdempotencyResultIntegrityError(
                f"Idempotency key digest '{key.digest}': the stored result payload's digest no longer "
                "matches the record's durable result_hash (tampered or corrupted result document)."
            )
        return result


__all__ = [
    "IdempotencyError",
    "IdempotencyPort",
    "IdempotencyReleaseMismatchError",
    "IdempotencyResultIntegrityError",
    "IdempotencyResultMismatchError",
    "StoreBackedIdempotencyPort",
]
