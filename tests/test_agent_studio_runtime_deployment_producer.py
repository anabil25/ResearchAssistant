from __future__ import annotations

from datetime import UTC, datetime

import pytest
from research_assistant_api.agent_studio.audit_service import AuditService, InMemoryAuditStore
from research_assistant_api.agent_studio.models import AuditEvent, AuditEventKind, DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_client_binding import (
    BindingPreconditionError,
    InMemoryClientDeploymentBindingIndex,
    NonMonotonicRepointError,
    ReinstateStateError,
    RevokedBindingResurrectionError,
    RuntimeBindingStatus,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeOperationRef,
)
from research_assistant_api.agent_studio.runtime_deployment_producer import (
    _MAX_SUCCESSION_ATTEMPTS,
    BijectiveCardinalityError,
    BindingLeadsHeadError,
    RevisionStillReferencedError,
    RollbackRepointError,
    RuntimeDeploymentProducer,
    RuntimeDeploymentProducerError,
    SuccessionBuilderContractError,
    SuccessionExhaustedError,
)
from research_assistant_api.agent_studio.runtime_mapping_store import (
    InMemoryRuntimeDeploymentMappingStore,
    RuntimeDeploymentHead,
    RuntimeHeadPreconditionError,
)
from research_assistant_api.agent_studio.scope import ScopeContext

CLIENT = "client-app-1"
ACTOR = "control-plane-actor"
NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
SCOPE = ScopeContext(tenant_id="tenant-1", project_id="project-1")


def _mapping(
    *,
    deployment_id: str = "dep-1",
    backend_version: str = "1.2.3",
    revision_sequence: int = 1,
    client_app_id: str = CLIENT,
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search"),
        operation_ref=RuntimeOperationRef(id="search"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-producer-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="release-1",
        backend_version=backend_version,
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            AllowedClientAppRoleBinding(client_app_id=client_app_id, app_role="research-assistant.runtime"),
        ),
        revision_sequence=revision_sequence,
        revision_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        deployment_created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        created_by="control-plane",
    )


def _producer() -> tuple[
    RuntimeDeploymentProducer,
    InMemoryRuntimeDeploymentMappingStore,
    InMemoryClientDeploymentBindingIndex,
    InMemoryAuditStore,
]:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    audit_store = InMemoryAuditStore()
    producer = RuntimeDeploymentProducer(store, index, index, AuditService(audit_store))
    return producer, store, index, audit_store


def _events(audit_store: InMemoryAuditStore, kind: AuditEventKind, phase: str) -> list[AuditEvent]:
    return [e for e in audit_store.list_events(scope=SCOPE, kind=kind) if e.detail.get("phase") == phase]


# --- GRANT -----------------------------------------------------------------


def test_grant_writes_mapping_then_binds_and_audits_intent_and_applied() -> None:
    producer, store, index, audit = _producer()
    mapping = _mapping()
    assert producer.grant(mapping, actor_id=ACTOR, now=NOW) is mapping
    assert store.get("dep-1", 1) is mapping
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.ACTIVE
    # Intent-first: both an intent and an applied audit event exist for the grant.
    intents = _events(audit, AuditEventKind.RUNTIME_BINDING_GRANTED, "intent")
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_GRANTED, "applied")
    assert len(intents) == 1
    assert len(applied) == 1
    assert applied[0].detail["to_revision_sequence"] == "1"
    assert intents[0].detail["intent_id"] == applied[0].detail["intent_id"]


def test_grant_is_idempotent_for_identical_inputs() -> None:
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 1
    assert store.get("dep-1", 1) is not None


def test_grant_advances_and_repoints() -> None:
    producer, _store, index, audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    v2 = _mapping(revision_sequence=2, backend_version="2.0.0")
    producer.grant(v2, actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_REPOINTED, "applied")
    assert len(applied) == 1
    assert applied[0].detail["from_revision_sequence"] == "1"
    assert applied[0].detail["to_revision_sequence"] == "2"


def test_grant_rejects_rollback_to_older_revision() -> None:
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.revision_sequence == 2


def test_grant_rejects_same_sequence_different_content() -> None:
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=2, backend_version="9.9.9"), actor_id=ACTOR, now=NOW)


def test_first_grant_must_be_sequence_one() -> None:
    # Bootstrap is decided by the ABSENCE of a head, so a first grant at any
    # sequence other than 1 is refused (no head to derive a successor from).
    producer, store, _index, _audit = _producer()
    with pytest.raises(RollbackRepointError, match="must be sequence 1"):
        producer.grant(_mapping(revision_sequence=2), actor_id=ACTOR, now=NOW)
    assert store.get_head("dep-1") is None


def test_grant_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.grant(_mapping(), actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


# --- REVOKE (binding-status tombstone, no new mapping revision) -------------


def test_revoke_flips_binding_to_revoked_tombstone_in_place() -> None:
    # Ratified: REVOKE is a single CAS write flipping the binding status to
    # REVOKED at the SAME (sequence, revision_id) -- no new mapping revision, no
    # store write. The row survives (counter + digest preserved).
    producer, store, index, audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    producer.revoke(active, actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED
    assert resolution.revision_sequence == 1  # same sequence, tombstoned in place
    # No new/retiring mapping revision was written.
    assert store.get("dep-1", 2) is None
    assert store.get("dep-1", 1) is active
    applied = _events(audit, AuditEventKind.RUNTIME_BINDING_REVOKED, "applied")
    assert len(applied) == 1


def test_supersede_grant_over_revoked_client_is_terminal() -> None:
    # Revocation is TERMINAL: an ordinary supersede grant may NOT resurrect a
    # revoked client's binding. The mapping revision still advances (mapping-first
    # commit), but the binding stays a REVOKED tombstone -- an inspectable partial.
    # The counter is preserved (sequence 1) so a future EXPLICIT re-grant (a
    # separate reviewed control-plane operation) has a value to advance from.
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    producer.revoke(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    with pytest.raises(RevokedBindingResurrectionError):
        producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED
    assert resolution.revision_sequence == 1  # counter preserved for a later explicit re-grant
    assert store.get("dep-1", 2) is not None  # revision committed mapping-first; binding never repointed


class _RevokeThenDelegateWriter:
    """Drives the REAL retry interleaving the fail-open ruling targets.

    On the FIRST repoint-to-ACTIVE attempt it performs a genuine in-place REVOKE
    on the real index (as a concurrent REVOKE winning the CAS would) and raises
    ``BindingPreconditionError`` -- so the producer's retry re-reads the now-REVOKED
    row from the REAL index and must hit the terminal-revocation guard, not a
    stubbed check. Subsequent attempts delegate to the real index.
    """

    def __init__(self, index: InMemoryClientDeploymentBindingIndex) -> None:
        self._index = index
        self._revoked = False

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
        if not self._revoked and status is RuntimeBindingStatus.ACTIVE:
            self._revoked = True
            current = self._index.resolve_binding(client_app_id, deployment_id)
            assert current is not None
            self._index.repoint(
                client_app_id, deployment_id, current.revision_sequence, current.revision_id,
                RuntimeBindingStatus.REVOKED, expected_current_sequence=current.revision_sequence,
            )
            raise BindingPreconditionError("a concurrent revoke won the CAS")
        self._index.repoint(
            client_app_id, deployment_id, revision_sequence, revision_id, status,
            expected_current_sequence=expected_current_sequence,
        )

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:
        self._index.reinstate(
            client_app_id, deployment_id, revision_sequence, revision_id,
            expected_current_sequence=expected_current_sequence,
        )


def test_supersede_retry_after_concurrent_revoke_is_terminal() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    RuntimeDeploymentProducer(store, index, index, AuditService(InMemoryAuditStore())).grant(
        _mapping(revision_sequence=1), actor_id=ACTOR, now=NOW
    )
    racing = RuntimeDeploymentProducer(
        store, _RevokeThenDelegateWriter(index), index, AuditService(InMemoryAuditStore())
    )
    with pytest.raises(RevokedBindingResurrectionError):
        racing.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.REVOKED  # revocation stands after the retry


def test_revoke_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.revoke(_mapping(), actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


# --- retention interlock ---------------------------------------------------


def test_retire_revision_refuses_a_referenced_revision() -> None:
    producer, store, _index, _audit = _producer()
    active = _mapping(revision_sequence=1)
    producer.grant(active, actor_id=ACTOR, now=NOW)
    with pytest.raises(RevisionStillReferencedError):
        producer.retire_revision("dep-1", 1, active.revision_id, (CLIENT,))
    assert store.get("dep-1", 1) is active  # not deleted


def test_retire_revision_deletes_an_unreferenced_revision() -> None:
    producer, store, _index, _audit = _producer()
    v1 = _mapping(revision_sequence=1)
    producer.grant(v1, actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, backend_version="2.0.0"), actor_id=ACTOR, now=NOW)
    # v1 is superseded (binding now points at seq 2), so it may be retired.
    producer.retire_revision("dep-1", 1, v1.revision_id, (CLIENT,))
    assert store.get("dep-1", 1) is None
    assert store.get("dep-1", 2) is not None


# --- CAS concurrency paths (intent-first, retry, non-convergence) ----------


class _FlakyWriter:
    def __init__(self, index: InMemoryClientDeploymentBindingIndex, fail_times: int) -> None:
        self._index = index
        self._remaining = fail_times

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
        if self._remaining > 0:
            self._remaining -= 1
            raise BindingPreconditionError("flaky")
        self._index.repoint(
            client_app_id, deployment_id, revision_sequence, revision_id, status,
            expected_current_sequence=expected_current_sequence,
        )

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:  # pragma: no cover - not exercised by these grant-path tests
        raise NotImplementedError


def test_grant_retries_on_precondition_then_succeeds() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(store, _FlakyWriter(index, 1), index, AuditService(InMemoryAuditStore()))
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    assert index.resolve_binding(CLIENT, "dep-1") is not None


def test_grant_non_convergence_leaves_intent_without_applied() -> None:
    # Intent-first: when the CAS never converges, the INTENT audit event exists
    # but no APPLIED -- a dangling reconciliation signal, not a silent loss.
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    audit_store = InMemoryAuditStore()
    producer = RuntimeDeploymentProducer(store, _FlakyWriter(index, 999), index, AuditService(audit_store))
    with pytest.raises(BindingPreconditionError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_GRANTED, "intent")) == 1
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_GRANTED, "applied")) == 0
    # Exhaustion is resolved EXPLICITLY as "failed" (not left dangling, not applied),
    # so it is distinguishable from a crash's half-resolved intent.
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_GRANTED, "failed")) == 1


class _NonMonotonicWriter:
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
        raise NonMonotonicRepointError("cas-detected rollback")

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:  # pragma: no cover - not exercised by these grant-path tests
        raise NotImplementedError


def test_grant_maps_cas_nonmonotonic_to_rollback() -> None:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(store, _NonMonotonicWriter(), index, AuditService(InMemoryAuditStore()))
    with pytest.raises(RollbackRepointError):
        producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)


# --- grant_succession: head-derived next + bounded atomic retry ------------


def test_grant_succession_bootstraps_at_sequence_one() -> None:
    producer, store, index, _audit = _producer()
    result = producer.grant_succession(
        "dep-1", lambda seq: _mapping(revision_sequence=seq), actor_id=ACTOR, now=NOW
    )
    assert result.revision_sequence == 1
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 1
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None and resolution.status is RuntimeBindingStatus.ACTIVE


def test_grant_succession_derives_next_from_head() -> None:
    producer, store, _index, _audit = _producer()
    producer.grant_succession("dep-1", lambda seq: _mapping(revision_sequence=seq), actor_id=ACTOR, now=NOW)
    second = producer.grant_succession(
        "dep-1", lambda seq: _mapping(revision_sequence=seq, backend_version="2.0.0"), actor_id=ACTOR, now=NOW
    )
    assert second.revision_sequence == 2
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 2


def test_grant_succession_rejects_builder_contract_violation() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(SuccessionBuilderContractError):
        # Builder returns the wrong sequence for the head-derived next.
        producer.grant_succession(
            "dep-1", lambda _seq: _mapping(revision_sequence=7), actor_id=ACTOR, now=NOW
        )


class _ContendedControlPlane:
    """Drives REAL head contention: on the first ``fail_times`` commits it lands a
    rival revision at the same next-sequence (a genuine concurrent superseder) and
    raises ``RuntimeHeadPreconditionError`` so the retry must re-read the advanced
    head and rebuild at the new next; after that it delegates."""

    def __init__(self, inner: InMemoryRuntimeDeploymentMappingStore, fail_times: int) -> None:
        self._inner = inner
        self._fail = fail_times

    def get(self, deployment_id: str, revision_sequence: int) -> RuntimeDeploymentMapping | None:
        return self._inner.get(deployment_id, revision_sequence)

    def get_head(self, deployment_id: str) -> RuntimeDeploymentHead | None:
        return self._inner.get_head(deployment_id)

    def list_revisions(self, deployment_id: str) -> tuple[int, ...]:
        return self._inner.list_revisions(deployment_id)

    def delete(self, deployment_id: str, revision_sequence: int) -> None:
        self._inner.delete(deployment_id, revision_sequence)

    def commit_revision(self, mapping: RuntimeDeploymentMapping, *, expected_head_sequence: int | None) -> None:
        if self._fail > 0:
            self._fail -= 1
            head = self._inner.get_head(mapping.deployment_id)
            current = 0 if head is None else head.current_sequence
            rival = _mapping(revision_sequence=current + 1, backend_version=f"rival-{current + 1}")
            self._inner.commit_revision(
                rival, expected_head_sequence=None if head is None else head.current_sequence
            )
            raise RuntimeHeadPreconditionError("a concurrent superseder won the head CAS")
        self._inner.commit_revision(mapping, expected_head_sequence=expected_head_sequence)


def test_grant_succession_retries_under_contention_then_converges() -> None:
    inner = InMemoryRuntimeDeploymentMappingStore()
    contended = _ContendedControlPlane(inner, fail_times=2)
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(contended, index, index, AuditService(InMemoryAuditStore()))
    result = producer.grant_succession(
        "dep-1", lambda seq: _mapping(revision_sequence=seq), actor_id=ACTOR, now=NOW
    )
    # Two rivals landed at sequences 1 and 2, so this grant converges at 3 -- it
    # re-read the advanced head and rebuilt at the new next each time.
    assert result.revision_sequence == 3
    head = inner.get_head("dep-1")
    assert head is not None and head.current_sequence == 3


def test_grant_succession_fails_loud_on_exhaustion_without_fallback() -> None:
    inner = InMemoryRuntimeDeploymentMappingStore()
    contended = _ContendedControlPlane(inner, fail_times=_MAX_SUCCESSION_ATTEMPTS)
    index = InMemoryClientDeploymentBindingIndex()
    producer = RuntimeDeploymentProducer(contended, index, index, AuditService(InMemoryAuditStore()))
    with pytest.raises(SuccessionExhaustedError):
        producer.grant_succession("dep-1", lambda seq: _mapping(revision_sequence=seq), actor_id=ACTOR, now=NOW)
    # No non-atomic fallback write: only the rivals' revisions exist; this grant
    # never force-wrote its own revision after exhaustion.
    assert inner.list_revisions("dep-1") == tuple(range(1, _MAX_SUCCESSION_ATTEMPTS + 1))
    # And no binding was ever activated for the exhausted grant.
    assert index.resolve_binding(CLIENT, "dep-1") is None


def test_grant_succession_binding_admission_runs_after_commit() -> None:
    # After the atomic commit, the binding repoint still runs the FULL admission:
    # a client revoked beforehand cannot be resurrected by the succession grant.
    producer, _store, index, _audit = _producer()
    producer.grant_succession("dep-1", lambda seq: _mapping(revision_sequence=seq), actor_id=ACTOR, now=NOW)
    producer.revoke(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    with pytest.raises(RevokedBindingResurrectionError):
        producer.grant_succession(
            "dep-1", lambda seq: _mapping(revision_sequence=seq, backend_version="2.0.0"), actor_id=ACTOR, now=NOW
        )
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None and resolution.status is RuntimeBindingStatus.REVOKED


# --- explicit reinstate (the ONE sanctioned REVOKED->ACTIVE re-grant) -------

OTHER = "other-app-2"


def test_reinstate_points_at_current_head_not_tombstone() -> None:
    # BLOCKING negative: revoke at N, advance head to N+k, re-grant, and assert the
    # binding points at the CURRENT head (N+k) and NOT the tombstone's retained
    # sequence N (which would silently roll the client back to its pre-revocation
    # revision -- Flaw A through the re-grant path).
    producer, store, index, audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)  # CLIENT active@1
    producer.revoke(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)  # CLIENT revoked, tombstone retains 1
    # Advance head to 3 WITHOUT touching CLIENT's binding (revisions bind a different client).
    producer.grant_succession(
        "dep-1", lambda seq: _mapping(revision_sequence=seq, client_app_id=OTHER), actor_id=ACTOR, now=NOW
    )
    producer.grant_succession(
        "dep-1", lambda seq: _mapping(revision_sequence=seq, client_app_id=OTHER), actor_id=ACTOR, now=NOW
    )
    head = store.get_head("dep-1")
    assert head is not None and head.current_sequence == 3
    reinstated = producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)
    assert reinstated.revision_sequence == 3
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None
    assert resolution.status is RuntimeBindingStatus.ACTIVE
    assert resolution.revision_sequence == 3  # points at CURRENT head N+k...
    assert resolution.revision_sequence != 1  # ...NOT the tombstone's retained sequence
    assert resolution.revision_id == head.current_revision_id
    # Recorded under a DISTINCT audit kind, intent-first.
    assert len(_events(audit, AuditEventKind.RUNTIME_BINDING_REINSTATED, "intent")) == 1
    assert len(_events(audit, AuditEventKind.RUNTIME_BINDING_REINSTATED, "applied")) == 1


def test_reinstate_requires_a_revoked_tombstone() -> None:
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)  # CLIENT still ACTIVE
    with pytest.raises(ReinstateStateError):
        producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)


def test_reinstate_without_head_raises() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(RuntimeDeploymentProducerError, match="no head"):
        producer.reinstate("dep-unknown", (CLIENT,), actor_id=ACTOR, now=NOW)


def test_reinstate_head_pointing_at_missing_revision_raises() -> None:
    producer, store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    store.delete("dep-1", 1)  # head still points at 1, but the revision is gone
    with pytest.raises(RuntimeDeploymentProducerError, match="missing revision"):
        producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)


def test_reinstate_rejects_naive_now() -> None:
    producer, _store, _index, _audit = _producer()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=datetime(2026, 1, 2, 12, 0, 0))


# --- third invariant: a binding may LAG head but never LEAD it -------------


def test_repoint_refuses_a_binding_that_leads_the_head() -> None:
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)  # head@1, CLIENT active@1
    # Corrupt the binding so it LEADS the head (points at a nonexistent seq 5).
    index.repoint(CLIENT, "dep-1", 5, "rev-5", RuntimeBindingStatus.ACTIVE, expected_current_sequence=1)
    # Any repoint against the head@1 mapping must now detect the lead and fail loud.
    with pytest.raises(BindingLeadsHeadError):
        producer.revoke(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)


# --- reinstate CAS retry / rollback / non-convergence ----------------------


def _grant_then_revoke() -> tuple[
    InMemoryRuntimeDeploymentMappingStore, InMemoryClientDeploymentBindingIndex
]:
    store = InMemoryRuntimeDeploymentMappingStore()
    index = InMemoryClientDeploymentBindingIndex()
    setup = RuntimeDeploymentProducer(store, index, index, AuditService(InMemoryAuditStore()))
    setup.grant(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    setup.revoke(_mapping(revision_sequence=1), actor_id=ACTOR, now=NOW)
    return store, index


class _FlakyReinstateWriter:
    def __init__(self, index: InMemoryClientDeploymentBindingIndex, fail_times: int) -> None:
        self._index = index
        self._remaining = fail_times

    def repoint(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        status: RuntimeBindingStatus, *, expected_current_sequence: int | None,
    ) -> None:  # pragma: no cover - reinstate-focused double
        raise NotImplementedError

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise BindingPreconditionError("flaky reinstate")
        self._index.reinstate(
            client_app_id, deployment_id, revision_sequence, revision_id,
            expected_current_sequence=expected_current_sequence,
        )


def test_reinstate_retries_on_precondition_then_succeeds() -> None:
    store, index = _grant_then_revoke()
    producer = RuntimeDeploymentProducer(
        store, _FlakyReinstateWriter(index, 1), index, AuditService(InMemoryAuditStore())
    )
    producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None and resolution.status is RuntimeBindingStatus.ACTIVE


class _RollbackReinstateWriter:
    def repoint(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        status: RuntimeBindingStatus, *, expected_current_sequence: int | None,
    ) -> None:  # pragma: no cover - reinstate-focused double
        raise NotImplementedError

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:
        raise NonMonotonicRepointError("reinstate points behind the tombstone")


def test_reinstate_maps_nonmonotonic_to_rollback() -> None:
    store, index = _grant_then_revoke()
    producer = RuntimeDeploymentProducer(store, _RollbackReinstateWriter(), index, AuditService(InMemoryAuditStore()))
    with pytest.raises(RollbackRepointError):
        producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)


class _AlwaysPreconditionReinstateWriter:
    def repoint(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        status: RuntimeBindingStatus, *, expected_current_sequence: int | None,
    ) -> None:  # pragma: no cover - reinstate-focused double
        raise NotImplementedError

    def reinstate(
        self, client_app_id: str, deployment_id: str, revision_sequence: int, revision_id: str,
        *, expected_current_sequence: int | None,
    ) -> None:
        raise BindingPreconditionError("never converges")


def test_reinstate_non_convergence_raises_precondition() -> None:
    store, index = _grant_then_revoke()
    audit_store = InMemoryAuditStore()
    producer = RuntimeDeploymentProducer(
        store, _AlwaysPreconditionReinstateWriter(), index, AuditService(audit_store)
    )
    with pytest.raises(BindingPreconditionError):
        producer.reinstate("dep-1", (CLIENT,), actor_id=ACTOR, now=NOW)
    # Exhaustion resolved explicitly as "failed", intent recorded, never applied.
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_REINSTATED, "intent")) == 1
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_REINSTATED, "applied")) == 0
    assert len(_events(audit_store, AuditEventKind.RUNTIME_BINDING_REINSTATED, "failed")) == 1


# --- bijective 1:1 cardinality (deployment -> one client) ------------------

OTHER_CLIENT = "other-client-9"


def test_grant_refuses_a_second_client_on_an_actively_bound_deployment() -> None:
    # Deployment->one-client direction: a deployment actively bound to one client
    # cannot be granted to a DIFFERENT client without an explicit revoke first.
    producer, _store, _index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1, client_app_id=CLIENT), actor_id=ACTOR, now=NOW)
    with pytest.raises(BijectiveCardinalityError):
        producer.grant(_mapping(revision_sequence=2, client_app_id=OTHER_CLIENT), actor_id=ACTOR, now=NOW)


def test_grant_allows_new_client_after_the_old_one_is_revoked() -> None:
    # Migration is an explicit REVOKE-then-GRANT (no dual-client overlap window).
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1, client_app_id=CLIENT), actor_id=ACTOR, now=NOW)
    producer.revoke(_mapping(revision_sequence=1, client_app_id=CLIENT), actor_id=ACTOR, now=NOW)
    producer.grant(_mapping(revision_sequence=2, client_app_id=OTHER_CLIENT), actor_id=ACTOR, now=NOW)
    assert index.resolve_binding(OTHER_CLIENT, "dep-1") is not None  # new client is active
    old = index.resolve_binding(CLIENT, "dep-1")
    assert old is not None and old.status is RuntimeBindingStatus.REVOKED  # old client tombstoned, not active


def test_grant_refuses_a_mapping_authorizing_multiple_clients() -> None:
    producer, _store, _index, _audit = _producer()
    two_clients = _mapping(revision_sequence=1).model_copy(
        update={
            "allowed_client_app_role_bindings": (
                AllowedClientAppRoleBinding(client_app_id=CLIENT, app_role="research-assistant.runtime"),
                AllowedClientAppRoleBinding(client_app_id=OTHER_CLIENT, app_role="research-assistant.runtime"),
            )
        }
    )
    with pytest.raises(BijectiveCardinalityError, match="EXACTLY ONE client"):
        producer.grant(two_clients, actor_id=ACTOR, now=NOW)


def test_same_client_supersede_is_not_a_cardinality_violation() -> None:
    producer, _store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1, client_app_id=CLIENT), actor_id=ACTOR, now=NOW)
    producer.grant(
        _mapping(revision_sequence=2, client_app_id=CLIENT, backend_version="2.0.0"), actor_id=ACTOR, now=NOW
    )
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None and resolution.revision_sequence == 2


def test_cardinality_check_skips_when_head_revision_is_missing() -> None:
    # Degenerate state: head points at a revision retention removed. The
    # deployment->one-client check cannot read the current client, so it skips
    # (grant's own head-sequence checks still run); covers the missing-revision guard.
    producer, store, index, _audit = _producer()
    producer.grant(_mapping(revision_sequence=1, client_app_id=CLIENT), actor_id=ACTOR, now=NOW)
    store.delete("dep-1", 1)  # head still at 1, revision gone
    producer.grant(
        _mapping(revision_sequence=2, client_app_id=CLIENT, backend_version="2.0.0"), actor_id=ACTOR, now=NOW
    )
    resolution = index.resolve_binding(CLIENT, "dep-1")
    assert resolution is not None and resolution.revision_sequence == 2
