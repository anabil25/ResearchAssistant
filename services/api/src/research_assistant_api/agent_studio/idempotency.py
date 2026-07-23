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

* ``load_result`` here takes an explicit ``scope: ScopeContext`` parameter
  (their reference Protocol has none) -- a real gap in taking an opaque
  ``result_ref`` string alone as sufficient addressing. Every point read here
  is always partitioned by the *caller's own authorized* ``scope.scope_key``,
  never by anything decoded from ``result_ref`` itself, so cross-tenant
  enumeration is structurally impossible even if a caller somehow guesses
  another scope's ``result_ref``.
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

    async def load_result(self, scope: ScopeContext, result_ref: str) -> dict[str, Any] | None: ...


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

    async def load_result(self, scope: ScopeContext, result_ref: str) -> dict[str, Any] | None:
        return self._store.load_idempotency_result(scope, result_ref)


__all__ = [
    "IdempotencyError",
    "IdempotencyPort",
    "IdempotencyResultMismatchError",
    "StoreBackedIdempotencyPort",
]
