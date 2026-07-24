"""Tests for the durable idempotency domain models, in-memory store
lifecycle, and the async ``IdempotencyPort`` adapter.

This is an independently-designed adapter -- structurally aligned in
shape/semantics only (never imported from, or into) with the harness's own
``agents.shared.idempotency.IdempotencyStore`` contract (inspected read-only
at commit ``cef9975``). See ``agent_studio/idempotency.py``'s module
docstring for the two deliberate design deviations these tests specifically
cover (scope-aware ``load_result``; independently recomputed result digest).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.idempotency import (
    IdempotencyReleaseMismatchError,
    IdempotencyResultIntegrityError,
    IdempotencyResultMismatchError,
    StoreBackedIdempotencyPort,
    _hash_result,
)
from research_assistant_api.agent_studio.models import (
    IdempotencyClaim,
    IdempotencyClaimDisposition,
    IdempotencyKey,
    IdempotencyRecord,
    IdempotencyState,
    idempotency_key_digest,
    utc_now,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import (
    AgentStudioStore,
    AgentStudioStoreError,
    IdempotencyConcurrencyError,
    IdempotencyNotFoundError,
    hash_idempotency_token,
    is_idempotency_lease_expired,
    validate_idempotency_lease_seconds,
)

TENANT = "tenant-1"
PROJECT = "project-1"
SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
OTHER_TENANT_SCOPE = ScopeContext(tenant_id="tenant-2", project_id=PROJECT)
OTHER_PROJECT_SCOPE = ScopeContext(tenant_id=TENANT, project_id="project-2")


def _key(
    *,
    binding_digest: str = "a" * 64,
    operation_id: str = "search",
    destination: str = "descriptor-1.search",
    caller_key: str = "caller-1",
    argument_hash: str = "b" * 64,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> IdempotencyKey:
    return IdempotencyKey(
        tenant_id=tenant_id,
        project_id=project_id,
        binding_digest=binding_digest,
        operation_id=operation_id,
        destination=destination,
        caller_key=caller_key,
        argument_hash=argument_hash,
    )


def _record(**overrides: object) -> IdempotencyRecord:
    fields: dict[str, object] = {
        "key": _key(),
        "state": IdempotencyState.CLAIMED,
        "version": "1",
        "claim_token_hash": hash_idempotency_token("token"),
        "lease_expires_at": utc_now() + timedelta(seconds=300),
        "actor_id": "user-1",
        "release_id": "release-1",
    }
    fields.update(overrides)
    return IdempotencyRecord(**fields)  # type: ignore[arg-type]


# --- IdempotencyKey / digest -----------------------------------------------


def test_idempotency_key_is_frozen_and_rejects_unknown_fields() -> None:
    key = _key()
    with pytest.raises(ValidationError):
        key.caller_key = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        IdempotencyKey(**{**key.model_dump(), "unexpected": "value"})  # type: ignore[arg-type]


def test_idempotency_key_rejects_malformed_digest_fields() -> None:
    with pytest.raises(ValidationError):
        _key(binding_digest="not-a-hex-digest")
    with pytest.raises(ValidationError):
        _key(argument_hash="short")


def test_idempotency_key_digest_is_deterministic_and_versioned_prefixed() -> None:
    key = _key()
    assert key.digest == idempotency_key_digest(key)
    assert key.digest.startswith("idem:v1:sha256:")
    # Golden vector: this exact field tuple must always produce this exact
    # digest. If this assertion ever needs to change, the encoding version
    # must be bumped (see ``_IDEMPOTENCY_KEY_DIGEST_PREFIX``) rather than
    # silently changing what "v1" means.
    assert key.digest == (
        "idem:v1:sha256:bcc9b29a1a6ad828db8e9f902cf539a3ece3468bff63cad782f05031595b2326"
    )


def test_idempotency_key_digest_is_stable_across_repeated_calls() -> None:
    key = _key()
    assert idempotency_key_digest(key) == idempotency_key_digest(key)


def test_idempotency_key_digest_differs_when_any_field_differs() -> None:
    base = _key()
    variants = [
        _key(binding_digest="c" * 64),
        _key(operation_id="different-op"),
        _key(destination="different-destination"),
        _key(caller_key="different-caller"),
        _key(argument_hash="d" * 64),
        _key(tenant_id="other-tenant"),
        _key(project_id="other-project"),
    ]
    digests = {base.digest, *(variant.digest for variant in variants)}
    assert len(digests) == len(variants) + 1


def test_idempotency_key_digest_does_not_collide_across_field_boundaries() -> None:
    """Concatenating adjacent field values differently must never collide --
    proves the JSON-array encoding (not a separator-joined string) actually
    matters here, exactly like ``scope.compute_scope_key``'s equivalent
    test."""
    shifted = _key(operation_id="search-extra", destination="tra.descriptor-1.search")
    assert shifted.digest != _key().digest


# --- IdempotencyRecord / IdempotencyClaim validators -----------------------


def test_record_rejects_claimed_state_with_started_at_set() -> None:
    with pytest.raises(ValidationError, match="CLAIMED"):
        _record(started_at=utc_now())


def test_record_rejects_in_progress_state_without_started_at() -> None:
    with pytest.raises(ValidationError, match="IN_PROGRESS"):
        _record(state=IdempotencyState.IN_PROGRESS)


def test_record_accepts_in_progress_state_with_started_at() -> None:
    record = _record(state=IdempotencyState.IN_PROGRESS, started_at=utc_now())
    assert record.state is IdempotencyState.IN_PROGRESS


def test_record_rejects_completed_state_missing_result_fields() -> None:
    with pytest.raises(ValidationError, match="COMPLETED"):
        _record(state=IdempotencyState.COMPLETED, completed_at=utc_now())


def test_record_accepts_completed_state_with_full_result_identity() -> None:
    record = _record(
        state=IdempotencyState.COMPLETED,
        completed_at=utc_now(),
        result_hash="e" * 64,
        result_ref="idempotency-result::" + "e" * 64,
    )
    assert record.state is IdempotencyState.COMPLETED


def test_record_rejects_failed_state_without_failure_code_or_reconciliation_flag() -> None:
    with pytest.raises(ValidationError, match="FAILED"):
        _record(state=IdempotencyState.FAILED)
    with pytest.raises(ValidationError, match="FAILED"):
        _record(state=IdempotencyState.FAILED, failure_code="boom", reconciliation_required=False)


def test_record_accepts_failed_state_with_failure_code_and_reconciliation_flag() -> None:
    record = _record(state=IdempotencyState.FAILED, failure_code="boom", reconciliation_required=True)
    assert record.state is IdempotencyState.FAILED


def test_record_rejects_irreversible_started_without_started_at() -> None:
    with pytest.raises(ValidationError, match="irreversible_started"):
        _record(irreversible_started=True)


def test_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.state = IdempotencyState.FAILED  # type: ignore[misc]


def test_claim_requires_token_iff_acquired() -> None:
    with pytest.raises(ValidationError, match="ACQUIRED"):
        IdempotencyClaim(disposition=IdempotencyClaimDisposition.ACQUIRED, record=_record())
    with pytest.raises(ValidationError, match="ACQUIRED"):
        IdempotencyClaim(
            disposition=IdempotencyClaimDisposition.IN_PROGRESS,
            record=_record(),
            claim_token="x" * 32,
        )
    acquired = IdempotencyClaim(
        disposition=IdempotencyClaimDisposition.ACQUIRED, record=_record(), claim_token="x" * 32
    )
    assert acquired.claim_token is not None
    not_acquired = IdempotencyClaim(disposition=IdempotencyClaimDisposition.IN_PROGRESS, record=_record())
    assert not_acquired.claim_token is None


# --- store-module helper functions -----------------------------------------


def test_hash_idempotency_token_is_deterministic_sha256_hex() -> None:
    assert hash_idempotency_token("abc") == hash_idempotency_token("abc")
    assert hash_idempotency_token("abc") != hash_idempotency_token("abd")
    assert len(hash_idempotency_token("abc")) == 64


def test_is_idempotency_lease_expired_covers_every_state_and_boundary() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    claimed_not_expired = _record(state=IdempotencyState.CLAIMED, lease_expires_at=now + timedelta(seconds=1))
    claimed_expired = _record(state=IdempotencyState.CLAIMED, lease_expires_at=now - timedelta(seconds=1))
    claimed_boundary = _record(state=IdempotencyState.CLAIMED, lease_expires_at=now)
    in_progress_expired = _record(
        state=IdempotencyState.IN_PROGRESS, started_at=now, lease_expires_at=now - timedelta(seconds=1)
    )
    completed = _record(
        state=IdempotencyState.COMPLETED,
        completed_at=now,
        result_hash="e" * 64,
        result_ref="idempotency-result::" + "e" * 64,
        lease_expires_at=now - timedelta(seconds=1),
    )
    failed = _record(
        state=IdempotencyState.FAILED,
        failure_code="boom",
        reconciliation_required=True,
        lease_expires_at=now - timedelta(seconds=1),
    )

    assert is_idempotency_lease_expired(claimed_not_expired, now=now) is False
    assert is_idempotency_lease_expired(claimed_expired, now=now) is True
    # Exactly-at-expiry counts as expired (`<=`, not `<`).
    assert is_idempotency_lease_expired(claimed_boundary, now=now) is True
    assert is_idempotency_lease_expired(in_progress_expired, now=now) is True
    # COMPLETED/FAILED are terminal states -- an old lease timestamp on them
    # is irrelevant to "is this claim still validly leased".
    assert is_idempotency_lease_expired(completed, now=now) is False
    assert is_idempotency_lease_expired(failed, now=now) is False


def test_validate_idempotency_lease_seconds_bounds() -> None:
    validate_idempotency_lease_seconds(1)
    validate_idempotency_lease_seconds(3600)
    with pytest.raises(ValueError, match="lease_seconds"):
        validate_idempotency_lease_seconds(0)
    with pytest.raises(ValueError, match="lease_seconds"):
        validate_idempotency_lease_seconds(-1)
    with pytest.raises(ValueError, match="lease_seconds"):
        validate_idempotency_lease_seconds(3600.01)


# --- AgentStudioStore in-memory lifecycle ----------------------------------


def test_claim_acquires_fresh_key_with_one_time_token() -> None:
    store = AgentStudioStore()
    claim = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)

    assert claim.disposition is IdempotencyClaimDisposition.ACQUIRED
    assert claim.claim_token is not None
    assert claim.record.state is IdempotencyState.CLAIMED
    assert claim.record.actor_id == "user-1"
    assert claim.record.release_id == "release-1"
    assert claim.record.version == "1"
    # The raw token is never itself persisted on the record.
    assert claim.claim_token not in claim.record.model_dump_json()


def test_claim_rejects_out_of_bounds_lease_seconds() -> None:
    store = AgentStudioStore()
    with pytest.raises(ValueError, match="lease_seconds"):
        store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=0)
    with pytest.raises(ValueError, match="lease_seconds"):
        store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=3601)


def test_claim_rejects_key_scope_mismatch() -> None:
    store = AgentStudioStore()
    mismatched_key = _key(tenant_id="other-tenant")
    with pytest.raises(AgentStudioStoreError, match="does not"):
        store.claim_idempotency(SCOPE, mismatched_key, actor_id="user-1", release_id="release-1", lease_seconds=300)


def test_second_claim_against_same_key_observes_in_progress_without_new_token() -> None:
    store = AgentStudioStore()
    first = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert first.disposition is IdempotencyClaimDisposition.ACQUIRED

    second = store.claim_idempotency(SCOPE, _key(), actor_id="user-2", release_id="release-1", lease_seconds=300)
    assert second.disposition is IdempotencyClaimDisposition.IN_PROGRESS
    assert second.claim_token is None
    assert second.record == first.record


def test_claim_after_lease_expiry_reports_reconciliation_required() -> None:
    store = AgentStudioStore()
    now = utc_now()
    store.claim_idempotency(
        SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=1, now=now - timedelta(seconds=10)
    )

    reclaim = store.claim_idempotency(
        SCOPE, _key(), actor_id="user-2", release_id="release-1", lease_seconds=300, now=now
    )

    assert reclaim.disposition is IdempotencyClaimDisposition.RECONCILIATION_REQUIRED
    assert reclaim.claim_token is None


def test_claim_after_failure_reports_reconciliation_required() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None
    store.fail_idempotency(
        SCOPE, _key(), claim_token=claimed.claim_token, expected_version="1", failure_code="boom"
    )

    reclaim = store.claim_idempotency(SCOPE, _key(), actor_id="user-2", release_id="release-1", lease_seconds=300)

    assert reclaim.disposition is IdempotencyClaimDisposition.RECONCILIATION_REQUIRED
    assert reclaim.record.state is IdempotencyState.FAILED


def test_claim_after_completion_replays_completed_disposition() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None
    store.complete_idempotency(
        SCOPE,
        _key(),
        claim_token=claimed.claim_token,
        expected_version="1",
        result={"status": "ok"},
        result_hash="e" * 64,
    )

    replay = store.claim_idempotency(SCOPE, _key(), actor_id="user-2", release_id="release-1", lease_seconds=300)

    assert replay.disposition is IdempotencyClaimDisposition.COMPLETED
    assert replay.claim_token is None
    assert replay.record.result_hash == "e" * 64


def test_mark_in_progress_requires_matching_token_and_version() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None

    with pytest.raises(IdempotencyConcurrencyError):
        store.mark_idempotency_in_progress(
            SCOPE, _key(), claim_token="wrong-token", expected_version="1", irreversible=False
        )
    with pytest.raises(IdempotencyConcurrencyError):
        store.mark_idempotency_in_progress(
            SCOPE, _key(), claim_token=claimed.claim_token, expected_version="99", irreversible=False
        )

    updated = store.mark_idempotency_in_progress(
        SCOPE, _key(), claim_token=claimed.claim_token, expected_version="1", irreversible=True
    )
    assert updated.state is IdempotencyState.IN_PROGRESS
    assert updated.started_at is not None
    assert updated.irreversible_started is True
    assert updated.version == "2"


def test_transition_against_unclaimed_key_raises_not_found() -> None:
    store = AgentStudioStore()
    with pytest.raises(IdempotencyNotFoundError):
        store.mark_idempotency_in_progress(
            SCOPE, _key(), claim_token="token", expected_version="1", irreversible=False
        )
    with pytest.raises(IdempotencyNotFoundError):
        store.complete_idempotency(
            SCOPE, _key(), claim_token="token", expected_version="1", result={}, result_hash="e" * 64
        )
    with pytest.raises(IdempotencyNotFoundError):
        store.fail_idempotency(SCOPE, _key(), claim_token="token", expected_version="1", failure_code="boom")


def test_complete_persists_result_and_makes_it_loadable() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None

    updated = store.complete_idempotency(
        SCOPE,
        _key(),
        claim_token=claimed.claim_token,
        expected_version="1",
        result={"status": "ok", "value": 42},
        result_hash="e" * 64,
    )

    assert updated.state is IdempotencyState.COMPLETED
    assert updated.result_ref is not None
    loaded = store.load_idempotency_result(SCOPE, updated.result_ref)
    assert loaded == {"status": "ok", "value": 42}


def test_fail_marks_reconciliation_required() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None

    updated = store.fail_idempotency(
        SCOPE, _key(), claim_token=claimed.claim_token, expected_version="1", failure_code="boom"
    )

    assert updated.state is IdempotencyState.FAILED
    assert updated.failure_code == "boom"
    assert updated.reconciliation_required is True


def test_load_result_and_get_record_never_leak_across_scope() -> None:
    store = AgentStudioStore()
    claimed = store.claim_idempotency(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claimed.claim_token is not None
    updated = store.complete_idempotency(
        SCOPE,
        _key(),
        claim_token=claimed.claim_token,
        expected_version="1",
        result={"status": "ok"},
        result_hash="e" * 64,
    )
    assert updated.result_ref is not None

    assert store.get_idempotency_record(SCOPE, _key()) == updated
    assert store.load_idempotency_result(SCOPE, updated.result_ref) == {"status": "ok"}

    # A different project under the *same* tenant must not see either the
    # record or the result, even though it queries with the exact same
    # (otherwise-matching) key fields and the exact same result_ref string.
    with pytest.raises(AgentStudioStoreError):
        store.get_idempotency_record(OTHER_PROJECT_SCOPE, _key())
    assert store.load_idempotency_result(OTHER_PROJECT_SCOPE, updated.result_ref) is None

    # A different tenant under the same project name must not see either.
    with pytest.raises(AgentStudioStoreError):
        store.get_idempotency_record(OTHER_TENANT_SCOPE, _key())
    assert store.load_idempotency_result(OTHER_TENANT_SCOPE, updated.result_ref) is None


def test_get_idempotency_record_returns_none_for_unknown_key() -> None:
    store = AgentStudioStore()
    assert store.get_idempotency_record(SCOPE, _key()) is None


def test_claim_race_yields_exactly_one_acquired_winner() -> None:
    import threading

    store = AgentStudioStore()
    thread_count = 20
    claims: list[IdempotencyClaim] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _attempt(worker_index: int) -> None:
        barrier.wait()
        claim = store.claim_idempotency(
            SCOPE, _key(), actor_id=f"user-{worker_index}", release_id="release-1", lease_seconds=300
        )
        with lock:
            claims.append(claim)

    threads = [threading.Thread(target=_attempt, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    acquired = [claim for claim in claims if claim.disposition is IdempotencyClaimDisposition.ACQUIRED]
    in_progress = [claim for claim in claims if claim.disposition is IdempotencyClaimDisposition.IN_PROGRESS]
    assert len(claims) == thread_count
    assert len(acquired) == 1
    assert len(in_progress) == thread_count - 1
    assert all(claim.record == acquired[0].record for claim in in_progress)


# --- StoreBackedIdempotencyPort (async) ------------------------------------


def test_hash_result_is_order_independent_and_deterministic() -> None:
    assert _hash_result({"a": 1, "b": 2}) == _hash_result({"b": 2, "a": 1})
    assert _hash_result({"a": 1}) != _hash_result({"a": 2})


@pytest.mark.asyncio
async def test_port_full_lifecycle_claim_progress_complete_load() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()

    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.disposition is IdempotencyClaimDisposition.ACQUIRED
    assert claim.claim_token is not None

    in_progress = await port.mark_in_progress(
        SCOPE, key, claim_token=claim.claim_token, expected_version=claim.record.version, irreversible=True
    )
    assert in_progress.state is IdempotencyState.IN_PROGRESS

    completed = await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=in_progress.version,
        result={"status": "ok"},
    )
    assert completed.state is IdempotencyState.COMPLETED
    assert completed.result_ref is not None

    loaded = await port.load_result(SCOPE, key, release_id="release-1")
    assert loaded == {"status": "ok"}
    assert await port.load_result(SCOPE, _key(caller_key="unknown-caller"), release_id="release-1") is None


@pytest.mark.asyncio
async def test_port_complete_recomputes_digest_and_rejects_mismatched_expectation() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    with pytest.raises(IdempotencyResultMismatchError):
        await port.complete(
            SCOPE,
            key,
            claim_token=claim.claim_token,
            expected_version=claim.record.version,
            result={"status": "ok"},
            expected_result_hash="0" * 64,
        )

    # The correct, independently recomputed digest is accepted even when
    # supplied by the caller as a matching sanity assertion.
    correct_hash = _hash_result({"status": "ok"})
    completed = await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        result={"status": "ok"},
        expected_result_hash=correct_hash,
    )
    assert completed.result_hash == correct_hash


@pytest.mark.asyncio
async def test_port_load_result_returns_none_for_unclaimed_or_incomplete_key() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)

    # Never claimed at all.
    assert await port.load_result(SCOPE, _key(), release_id="release-1") is None

    # Claimed but not yet completed.
    other_key = _key(caller_key="caller-2")
    await port.claim(SCOPE, other_key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert await port.load_result(SCOPE, other_key, release_id="release-1") is None


@pytest.mark.asyncio
async def test_port_load_result_rejects_release_mismatch() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None
    await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        result={"status": "ok"},
    )

    # The key durably completed under "release-1" -- asserting a different
    # release must be rejected outright, never silently served, since that
    # would replay a result as if it belonged to a release it never did.
    with pytest.raises(IdempotencyReleaseMismatchError):
        await port.load_result(SCOPE, key, release_id="release-2")

    # The correct release_id still replays successfully.
    assert await port.load_result(SCOPE, key, release_id="release-1") == {"status": "ok"}


@pytest.mark.asyncio
async def test_port_load_result_rejects_missing_result_document() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None
    completed = await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        result={"status": "ok"},
    )
    assert completed.result_ref is not None

    # Simulate a durably COMPLETED record whose result document is gone
    # (or was never actually written) -- an integrity violation, not an
    # ordinary "not found" that should quietly return None.
    del store._idempotency_results[(SCOPE.scope_key, completed.result_ref)]  # type: ignore[attr-defined]

    with pytest.raises(IdempotencyResultIntegrityError):
        await port.load_result(SCOPE, key, release_id="release-1")


@pytest.mark.asyncio
async def test_port_load_result_rejects_record_missing_result_identity() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None
    completed = await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        result={"status": "ok"},
    )
    assert completed.result_ref is not None

    # This state is structurally unreachable through the port's own API --
    # IdempotencyRecord's model_validator forbids a COMPLETED record without
    # both result_ref and result_hash. Simulate it directly against the
    # in-memory store (bypassing validation via model_copy, exactly as a
    # corrupted/partially-migrated durable record would look) to prove the
    # port fails closed instead of assuming the invariant always holds.
    dedup_key = (SCOPE.scope_key, key.digest)
    corrupted = store._idempotency_records[dedup_key].model_copy(  # type: ignore[attr-defined]
        update={"result_ref": None, "result_hash": None}
    )
    store._idempotency_records[dedup_key] = corrupted  # type: ignore[attr-defined]

    with pytest.raises(IdempotencyResultIntegrityError):
        await port.load_result(SCOPE, key, release_id="release-1")


@pytest.mark.asyncio
async def test_port_load_result_rejects_tampered_result_hash() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None
    completed = await port.complete(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        result={"status": "ok"},
    )
    assert completed.result_ref is not None

    # Simulate a corrupted/tampered result document: the payload stored no
    # longer matches the durable result_hash the record was completed with.
    store._idempotency_results[(SCOPE.scope_key, completed.result_ref)] = {"status": "tampered"}  # type: ignore[attr-defined]

    with pytest.raises(IdempotencyResultIntegrityError):
        await port.load_result(SCOPE, key, release_id="release-1")


@pytest.mark.asyncio
async def test_port_fail_records_reconciliation_required() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()
    claim = await port.claim(SCOPE, key, actor_id="user-1", release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    failed = await port.fail(
        SCOPE, key, claim_token=claim.claim_token, expected_version=claim.record.version, failure_code="boom"
    )
    assert failed.state is IdempotencyState.FAILED
    assert failed.reconciliation_required is True


@pytest.mark.asyncio
async def test_port_claim_rejects_out_of_bounds_lease_seconds() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    with pytest.raises(ValueError, match="lease_seconds"):
        await port.claim(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=0)
    with pytest.raises(ValueError, match="lease_seconds"):
        await port.claim(SCOPE, _key(), actor_id="user-1", release_id="release-1", lease_seconds=3601)


@pytest.mark.asyncio
async def test_port_concurrent_claim_race_yields_exactly_one_winner() -> None:
    store = AgentStudioStore()
    port = StoreBackedIdempotencyPort(store)
    key = _key()

    claims = await asyncio.gather(
        *(
            port.claim(SCOPE, key, actor_id=f"user-{index}", release_id="release-1", lease_seconds=300)
            for index in range(20)
        )
    )

    acquired = [claim for claim in claims if claim.disposition is IdempotencyClaimDisposition.ACQUIRED]
    assert len(acquired) == 1
    assert len(claims) == 20
