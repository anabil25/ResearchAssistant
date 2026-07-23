from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import research_assistant_api.agent_studio.store as store_module
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistrationKind,
    ToolRegistrationSpec,
)
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError

TENANT = "demo"
OTHER_TENANT = "other-tenant"
AGENT_ID = "agent-store-test"
OTHER_AGENT_ID = "agent-other-test"
USER_ID = "user-1"


def _manifest(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    project_id: str = "default",
    display_name: str = "Store Test Agent",
) -> AgentManifest:
    return AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        display_name=display_name,
        owner_kind=AgentOwnerKind.USER,
        owner_id=USER_ID,
    )


def _draft(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    project_id: str = "default",
    etag: str = "etag-1",
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        manifest=_manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id, project_id=project_id),
        updated_by=USER_ID,
        etag=etag,
    )


def _version(
    *,
    sequence: int = 1,
    version_id: str | None = None,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
) -> AgentVersion:
    return AgentVersion(
        id=version_id or f"version-{sequence}",
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        sequence=sequence,
        manifest=_manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id),
        manifest_hash=f"hash-{sequence}",
        created_by=USER_ID,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    status: ReleaseStatus = ReleaseStatus.GATED,
    previous_release_id: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        status=status,
        previous_release_id=previous_release_id,
        created_by=USER_ID,
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    idempotency_key: str = "key-1",
    state: ApprovalState = ApprovalState.PENDING,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=tenant_id,
        kind=ApprovalKind.RELEASE_PROMOTION,
        state=state,
        gated_action="promote_version",
        destination="prod",
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=idempotency_key,
    )


def _deployment(
    *,
    deployment_id: str = "deployment-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    version_id: str = "version-1",
    trace_ref: str | None = None,
) -> DeploymentRecord:
    return DeploymentRecord(
        id=deployment_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        version_id=version_id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by=USER_ID,
        trace_ref=trace_ref,
    )


def _proposal(
    *,
    proposal_id: str = "proposal-1",
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    state: BuilderProposalState = BuilderProposalState.PENDING,
) -> BuilderProposal:
    manifest = _manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id)
    return BuilderProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        logical_agent_id=logical_agent_id,
        draft_base_etag="etag-1",
        before_manifest=manifest,
        after_manifest=manifest,
        before_manifest_hash="hash-before",
        after_manifest_hash="hash-after",
        provenance=BuilderProvenance(
            generator="test-generator",
            message="Add a search tool.",
            requested_by=USER_ID,
        ),
        state=state,
    )


def _binding(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    version_id: str = "version-1",
) -> LogicalAgentBinding:
    return LogicalAgentBinding(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version_id,
        updated_by=USER_ID,
    )


def _tool_registration(
    *,
    registration_id: str = "reg-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
) -> ToolRegistrationSpec:
    return ToolRegistrationSpec(
        id=registration_id,
        tenant_id=tenant_id,
        logical_agent_id=logical_agent_id,
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by=USER_ID,
    )


def test_store_persistence_and_drafts_are_tenant_scoped() -> None:
    store = AgentStudioStore()
    draft = _draft()

    assert store.persistence == "in-memory"
    assert store.save_draft(draft) is draft
    assert store.get_draft(TENANT, AGENT_ID) == draft
    assert store.get_draft(OTHER_TENANT, AGENT_ID) is None
    assert store.list_drafts(TENANT) == (draft,)
    assert store.list_drafts(OTHER_TENANT) == ()


def test_role_for_honors_precedence_and_project_scoping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AgentStudioStore()
    scoped_owner = OwnershipGrant(
        tenant_id=TENANT,
        logical_agent_id=AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.OWNER,
        granted_by="admin",
        project_id="p1",
    )
    tenant_wide_viewer = OwnershipGrant(
        tenant_id=TENANT,
        logical_agent_id=AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.VIEWER,
        granted_by="admin",
    )
    scoped_contributor = OwnershipGrant(
        tenant_id=TENANT,
        logical_agent_id=AGENT_ID,
        principal_id="scoped-user",
        role=AgentRole.CONTRIBUTOR,
        granted_by="admin",
        project_id="p1",
    )
    for grant in (scoped_owner, tenant_wide_viewer, scoped_contributor):
        store.grant_ownership(grant)

    assert store.list_ownership(TENANT, AGENT_ID) == (scoped_owner, tenant_wide_viewer, scoped_contributor)
    assert store.role_for(TENANT, AGENT_ID, USER_ID) is AgentRole.OWNER
    assert store.role_for(TENANT, AGENT_ID, USER_ID, project_id="p1") is AgentRole.OWNER
    assert store.role_for(TENANT, AGENT_ID, USER_ID, project_id="p2") is AgentRole.VIEWER
    assert store.role_for(TENANT, AGENT_ID, "scoped-user", project_id="p1") is AgentRole.CONTRIBUTOR
    assert store.role_for(TENANT, AGENT_ID, "scoped-user", project_id="p2") is None
    assert store.role_for(TENANT, AGENT_ID, "missing-user") is None

    monkeypatch.setattr(store_module, "_ROLE_PRECEDENCE", ())
    assert store.role_for(TENANT, AGENT_ID, USER_ID) is None


def test_next_sequence_is_scoped_per_tenant_and_agent() -> None:
    store = AgentStudioStore()

    assert store.next_sequence(TENANT, AGENT_ID) == 1
    store.create_version(_version(sequence=1, version_id="version-1"))
    store.create_version(_version(sequence=1, version_id="other-agent-v1", logical_agent_id=OTHER_AGENT_ID))

    assert store.next_sequence(TENANT, AGENT_ID) == 2
    assert store.next_sequence(TENANT, OTHER_AGENT_ID) == 2
    assert store.next_sequence(OTHER_TENANT, AGENT_ID) == 1


def test_allocate_version_persists_monotonic_sequences_and_validates_builder() -> None:
    store = AgentStudioStore()

    first = store.allocate_version(TENANT, AGENT_ID, lambda sequence: _version(sequence=sequence))
    second = store.allocate_version(
        TENANT,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert store.list_versions(TENANT, AGENT_ID) == (first, second)
    assert store.get_version(TENANT, first.id) == first
    assert store.get_version(TENANT, "missing") is None

    with pytest.raises(AgentStudioStoreError, match="expected atomically-reserved 3"):
        store.allocate_version(TENANT, AGENT_ID, lambda sequence: _version(sequence=sequence + 1))
    assert store.next_sequence(TENANT, AGENT_ID) == 3

    with pytest.raises(AgentStudioStoreError, match="already exists"):
        store.allocate_version(TENANT, AGENT_ID, lambda sequence: _version(sequence=sequence, version_id=second.id))


def test_allocate_version_is_atomic_for_concurrent_calls() -> None:
    store = AgentStudioStore()

    def allocate(_index: int) -> AgentVersion:
        return store.allocate_version(TENANT, AGENT_ID, lambda sequence: _version(sequence=sequence))

    with ThreadPoolExecutor(max_workers=8) as executor:
        versions = list(executor.map(allocate, range(8)))

    sequences = sorted(version.sequence for version in versions)
    assert sequences == list(range(1, 9))
    assert len({version.id for version in versions}) == 8
    assert store.next_sequence(TENANT, AGENT_ID) == 9


def test_create_version_rejects_duplicate_ids() -> None:
    store = AgentStudioStore()
    version = _version()

    assert store.create_version(version) == version
    with pytest.raises(AgentStudioStoreError, match="already exists"):
        store.create_version(version)


def test_lineage_and_gate_reports_round_trip() -> None:
    store = AgentStudioStore()
    edge = LineageEdge(
        tenant_id=TENANT,
        child_logical_agent_id=AGENT_ID,
        child_version_id="version-2",
        parent_logical_agent_id=OTHER_AGENT_ID,
        parent_version_id="version-1",
    )
    report = ReleaseGateReport(id="report-1", version_id="version-1", results=())

    assert store.add_lineage_edge(edge) == edge
    assert store.list_lineage(TENANT, AGENT_ID) == (edge,)
    assert store.list_lineage(TENANT, OTHER_AGENT_ID) == (edge,)
    assert store.list_lineage(TENANT, "agent-unrelated") == ()
    assert store.list_lineage(OTHER_TENANT, AGENT_ID) == ()

    assert store.save_gate_report(report) == report
    assert store.get_gate_report("report-1") == report
    assert store.get_gate_report("missing") is None


def test_release_crud_and_latest_transition_round_trip() -> None:
    store = AgentStudioStore()
    gated = _release(release_id="release-1")
    active = _release(
        release_id="release-2",
        status=ReleaseStatus.ACTIVE,
        previous_release_id=gated.id,
    )
    other_version = _release(release_id="release-3", version_id="version-2")

    assert store.latest_release_for_version(TENANT, "version-1") is None
    assert store.create_release(gated) == gated
    assert store.create_release(active) == active
    assert store.create_release(other_version) == other_version
    assert store.get_release(TENANT, gated.id) == gated
    assert store.get_release(TENANT, "missing") is None
    assert store.list_releases_for_version(TENANT, "version-1") == (gated, active)
    assert store.latest_release_for_version(TENANT, "version-1") == active
    assert store.list_releases_for_version(TENANT, "version-2") == (other_version,)


def test_approvals_are_idempotent_only_while_pending_and_decisions_validate_state() -> None:
    store = AgentStudioStore()
    pending = _approval()
    duplicate_pending = _approval(approval_id="approval-duplicate")

    assert store.create_approval(pending) == pending
    assert store.create_approval(duplicate_pending) == pending
    assert store.find_pending_approval(TENANT, pending.idempotency_key) == pending
    assert store.find_pending_approval(OTHER_TENANT, pending.idempotency_key) is None

    approved = pending.model_copy(update={"state": ApprovalState.APPROVED})
    assert store.save_approval_decision(approved) == approved
    assert store.find_pending_approval(TENANT, pending.idempotency_key) is None
    assert store.get_approval(TENANT, pending.id) == approved
    assert store.list_approvals(TENANT, version_id=pending.version_id) == (approved,)

    replacement = _approval(approval_id="approval-2")
    assert store.create_approval(replacement) == replacement
    assert store.list_approvals(TENANT) == (approved, replacement)

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_approval_decision(approved)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.save_approval_decision(_approval(approval_id="missing"))


def test_builder_proposals_round_trip_and_decisions_validate_state() -> None:
    store = AgentStudioStore()
    pending = _proposal()

    assert store.create_builder_proposal(pending) == pending
    assert store.get_builder_proposal(TENANT, pending.id) == pending
    assert store.get_builder_proposal(OTHER_TENANT, pending.id) is None
    assert store.get_builder_proposal(TENANT, "missing-proposal") is None
    assert store.list_builder_proposals(TENANT, AGENT_ID) == (pending,)
    assert store.list_builder_proposals(OTHER_TENANT, AGENT_ID) == ()

    decided = pending.model_copy(update={"state": BuilderProposalState.APPLIED})
    assert store.save_builder_proposal_decision(decided) == decided
    assert store.get_builder_proposal(TENANT, pending.id) == decided

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_builder_proposal_decision(decided)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.save_builder_proposal_decision(_proposal(proposal_id="missing-proposal"))


def test_deployments_update_and_missing_errors() -> None:
    store = AgentStudioStore()
    deployment = _deployment()

    assert store.create_deployment(deployment) == deployment
    assert store.get_deployment(TENANT, deployment.id) == deployment
    assert store.get_deployment(TENANT, "missing") is None
    assert store.list_deployments(TENANT, AGENT_ID) == (deployment,)

    updated = deployment.model_copy(update={"trace_ref": "trace-1"})
    assert store.update_deployment(updated) == updated
    assert store.get_deployment(TENANT, deployment.id) == updated

    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.update_deployment(_deployment(deployment_id="missing-deployment"))


def test_bindings_and_tool_registrations_round_trip() -> None:
    store = AgentStudioStore()
    binding = _binding()
    registration = _tool_registration()

    assert store.set_binding(binding) == binding
    assert store.get_binding(TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert store.get_binding(OTHER_TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None

    assert store.create_tool_registration(registration) == registration
    assert store.list_tool_registrations(TENANT, AGENT_ID) == (registration,)
    assert store.list_tool_registrations(TENANT, OTHER_AGENT_ID) == ()


def test_store_methods_enforce_cross_tenant_isolation() -> None:
    store = AgentStudioStore()
    draft = _draft()
    grant = OwnershipGrant(
        tenant_id=TENANT,
        logical_agent_id=AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.OWNER,
        granted_by="admin",
    )
    version = _version()
    release = _release()
    approval = _approval()
    deployment = _deployment()
    binding = _binding()
    lineage = LineageEdge(
        tenant_id=TENANT,
        child_logical_agent_id=AGENT_ID,
        child_version_id=version.id,
        parent_logical_agent_id=OTHER_AGENT_ID,
        parent_version_id="parent-version",
    )
    registration = _tool_registration()

    store.save_draft(draft)
    store.grant_ownership(grant)
    store.create_version(version)
    store.create_release(release)
    store.create_approval(approval)
    store.create_deployment(deployment)
    store.set_binding(binding)
    store.add_lineage_edge(lineage)
    store.create_tool_registration(registration)

    assert store.get_draft(OTHER_TENANT, AGENT_ID) is None
    assert store.list_drafts(OTHER_TENANT) == ()
    assert store.role_for(OTHER_TENANT, AGENT_ID, USER_ID) is None
    assert store.list_ownership(OTHER_TENANT, AGENT_ID) == ()
    assert store.get_version(OTHER_TENANT, version.id) is None
    assert store.list_versions(OTHER_TENANT, AGENT_ID) == ()
    assert store.get_release(OTHER_TENANT, release.id) is None
    assert store.list_releases_for_version(OTHER_TENANT, version.id) == ()
    assert store.latest_release_for_version(OTHER_TENANT, version.id) is None
    assert store.find_pending_approval(OTHER_TENANT, approval.idempotency_key) is None
    assert store.get_approval(OTHER_TENANT, approval.id) is None
    assert store.list_approvals(OTHER_TENANT) == ()
    assert store.get_deployment(OTHER_TENANT, deployment.id) is None
    assert store.list_deployments(OTHER_TENANT, AGENT_ID) == ()
    assert store.get_binding(OTHER_TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert store.list_lineage(OTHER_TENANT, AGENT_ID) == ()
    assert store.list_tool_registrations(OTHER_TENANT, AGENT_ID) == ()
