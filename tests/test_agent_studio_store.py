# mypy: disable-error-code=import-untyped
from __future__ import annotations

# ruff: noqa: E402
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "services" / "api" / "src" / "research_assistant_api"
if "research_assistant_api" not in sys.modules:
    package = types.ModuleType("research_assistant_api")
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["research_assistant_api"] = package

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
from research_assistant_api.agent_studio.scope import PLATFORM_PROJECT_ID, ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
AGENT_ID = "agent-store-test"
OTHER_AGENT_ID = "agent-other-test"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"

SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
SAME_TENANT_OTHER_PROJECT_SCOPE = ScopeContext(tenant_id=TENANT, project_id=OTHER_PROJECT)
OTHER_TENANT_SAME_PROJECT_SCOPE = ScopeContext(tenant_id=OTHER_TENANT, project_id=PROJECT)
PLATFORM_SCOPE = ScopeContext(tenant_id=TENANT, project_id=PLATFORM_PROJECT_ID)


def _scope(tenant_id: str = TENANT, project_id: str = PROJECT) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, project_id=project_id)


def _manifest(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
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
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    etag: str = "etag-1",
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        updated_by=USER_ID,
        etag=etag,
    )


def _grant(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    principal_id: str = USER_ID,
    role: AgentRole = AgentRole.OWNER,
) -> OwnershipGrant:
    return OwnershipGrant(
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        principal_id=principal_id,
        role=role,
        granted_by="admin",
    )


def _version(
    *,
    sequence: int = 1,
    version_id: str | None = None,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> AgentVersion:
    return AgentVersion(
        id=version_id or f"version-{sequence}",
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        sequence=sequence,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        manifest_hash=f"hash-{sequence}",
        created_by=USER_ID,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )


def _lineage(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    child_logical_agent_id: str = AGENT_ID,
    child_version_id: str = "version-2",
    parent_logical_agent_id: str = OTHER_AGENT_ID,
    parent_version_id: str = "version-1",
) -> LineageEdge:
    return LineageEdge(
        tenant_id=tenant_id,
        project_id=project_id,
        child_logical_agent_id=child_logical_agent_id,
        child_version_id=child_version_id,
        parent_logical_agent_id=parent_logical_agent_id,
        parent_version_id=parent_version_id,
    )


def _gate_report(report_id: str = "report-1", version_id: str = "version-1") -> ReleaseGateReport:
    return ReleaseGateReport(id=report_id, version_id=version_id, results=())


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    status: ReleaseStatus = ReleaseStatus.GATED,
    previous_release_id: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        status=status,
        previous_release_id=previous_release_id,
        created_by=USER_ID,
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    idempotency_key: str = "key-1",
    state: ApprovalState = ApprovalState.PENDING,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
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
    project_id: str = PROJECT,
    version_id: str = "version-1",
    trace_ref: str | None = None,
) -> DeploymentRecord:
    return DeploymentRecord(
        id=deployment_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by=USER_ID,
        trace_ref=trace_ref,
    )


def _binding(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    version_id: str = "version-1",
) -> LogicalAgentBinding:
    return LogicalAgentBinding(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version_id,
        updated_by=USER_ID,
    )


def _tool_registration(
    *,
    registration_id: str = "reg-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> ToolRegistrationSpec:
    return ToolRegistrationSpec(
        id=registration_id,
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by=USER_ID,
    )


def _proposal(
    *,
    proposal_id: str = "proposal-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    state: BuilderProposalState = BuilderProposalState.PENDING,
) -> BuilderProposal:
    manifest = _manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id)
    return BuilderProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        project_id=project_id,
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


def test_store_persistence_and_drafts_are_scope_scoped() -> None:
    store = AgentStudioStore()
    draft = _draft()

    assert store.persistence == "in-memory"
    assert store.save_draft(SCOPE, draft) == draft
    assert store.get_draft(SCOPE, AGENT_ID) == draft
    assert store.list_drafts(SCOPE) == (draft,)
    assert store.get_draft(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) is None
    assert store.list_drafts(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert store.get_draft(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) is None
    assert store.list_drafts(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()


def test_write_methods_reject_records_addressed_through_a_different_scope() -> None:
    store = AgentStudioStore()

    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.save_draft(SCOPE, _draft(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.grant_ownership(SCOPE, _grant(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.allocate_version(SCOPE, AGENT_ID, lambda sequence: _version(sequence=sequence, project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_version(SCOPE, _version(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.add_lineage_edge(SCOPE, _lineage(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_release(SCOPE, _release(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_approval(SCOPE, _approval(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.save_approval_decision(
            SCOPE,
            _approval(approval_id="approval-mismatch", project_id=OTHER_PROJECT, state=ApprovalState.APPROVED),
        )
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_deployment(SCOPE, _deployment(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.update_deployment(SCOPE, _deployment(deployment_id="deployment-mismatch", project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.set_binding(SCOPE, _binding(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_tool_registration(SCOPE, _tool_registration(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.create_builder_proposal(SCOPE, _proposal(project_id=OTHER_PROJECT))
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.save_builder_proposal_decision(
            SCOPE,
            _proposal(
                proposal_id="proposal-mismatch",
                project_id=OTHER_PROJECT,
                state=BuilderProposalState.APPLIED,
            ),
        )


def test_role_for_honors_precedence_and_exact_scope_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AgentStudioStore()
    owner = _grant(role=AgentRole.OWNER)
    contributor = _grant(principal_id=OTHER_USER_ID, role=AgentRole.CONTRIBUTOR)
    viewer_other_project = _grant(project_id=OTHER_PROJECT, role=AgentRole.VIEWER)
    platform_maintainer = _grant(project_id=PLATFORM_PROJECT_ID, role=AgentRole.MAINTAINER)

    assert store.grant_ownership(SCOPE, owner) == owner
    assert store.grant_ownership(SCOPE, contributor) == contributor
    assert store.grant_ownership(SAME_TENANT_OTHER_PROJECT_SCOPE, viewer_other_project) == viewer_other_project
    assert store.grant_ownership(PLATFORM_SCOPE, platform_maintainer) == platform_maintainer

    assert store.list_ownership(SCOPE, AGENT_ID) == (owner, contributor)
    assert store.list_ownership(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == (viewer_other_project,)
    assert store.list_ownership(PLATFORM_SCOPE, AGENT_ID) == (platform_maintainer,)
    assert store.list_ownership(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.role_for(SCOPE, AGENT_ID, USER_ID) is AgentRole.OWNER
    assert store.role_for(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, USER_ID) is AgentRole.VIEWER
    assert store.role_for(PLATFORM_SCOPE, AGENT_ID, USER_ID) is AgentRole.MAINTAINER
    assert store.role_for(SCOPE, AGENT_ID, OTHER_USER_ID) is AgentRole.CONTRIBUTOR
    assert store.role_for(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, OTHER_USER_ID) is None
    assert store.role_for(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, USER_ID) is None
    assert store.role_for(SCOPE, AGENT_ID, "missing-user") is None

    monkeypatch.setattr(store_module, "_ROLE_PRECEDENCE", ())
    assert store.role_for(SCOPE, AGENT_ID, USER_ID) is None


def test_next_sequence_is_advisory_and_partitioned_by_scope_and_agent() -> None:
    store = AgentStudioStore()

    assert store.next_sequence(SCOPE, AGENT_ID) == 1
    assert store.create_version(SCOPE, _version(sequence=1, version_id="version-1")) is not None
    assert (
        store.create_version(
            SAME_TENANT_OTHER_PROJECT_SCOPE,
            _version(sequence=1, version_id="other-project-version-1", project_id=OTHER_PROJECT),
        )
        is not None
    )
    assert store.create_version(
        SCOPE,
        _version(sequence=1, version_id="other-agent-v1", logical_agent_id=OTHER_AGENT_ID),
    )
    assert store.next_sequence(SCOPE, AGENT_ID) == 2
    assert store.next_sequence(SCOPE, OTHER_AGENT_ID) == 2
    assert store.next_sequence(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == 2
    assert store.next_sequence(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == 1


def test_allocate_version_persists_monotonic_sequences_and_hides_cross_scope_records() -> None:
    store = AgentStudioStore()

    first = store.allocate_version(SCOPE, AGENT_ID, lambda sequence: _version(sequence=sequence))
    second = store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert store.list_versions(SCOPE, AGENT_ID) == (first, second)
    assert store.get_version(SCOPE, first.id) == first
    assert store.get_version(SCOPE, "missing") is None
    assert store.get_version(SAME_TENANT_OTHER_PROJECT_SCOPE, first.id) is None
    assert store.list_versions(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.get_version(OTHER_TENANT_SAME_PROJECT_SCOPE, first.id) is None
    assert store.list_versions(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    with pytest.raises(AgentStudioStoreError, match="expected atomically-reserved 3"):
        store.allocate_version(SCOPE, AGENT_ID, lambda sequence: _version(sequence=sequence + 1))
    assert store.next_sequence(SCOPE, AGENT_ID) == 3

    with pytest.raises(AgentStudioStoreError, match="already exists"):
        store.allocate_version(SCOPE, AGENT_ID, lambda sequence: _version(sequence=sequence, version_id=second.id))


def test_allocate_version_is_atomic_for_concurrent_calls() -> None:
    store = AgentStudioStore()

    def allocate(_index: int) -> AgentVersion:
        return store.allocate_version(SCOPE, AGENT_ID, lambda sequence: _version(sequence=sequence))

    with ThreadPoolExecutor(max_workers=8) as executor:
        versions = list(executor.map(allocate, range(8)))

    assert sorted(version.sequence for version in versions) == list(range(1, 9))
    assert len({version.id for version in versions}) == 8
    assert store.next_sequence(SCOPE, AGENT_ID) == 9


def test_create_version_rejects_duplicate_ids() -> None:
    store = AgentStudioStore()
    version = _version()

    assert store.create_version(SCOPE, version) == version
    with pytest.raises(AgentStudioStoreError, match="already exists"):
        store.create_version(SCOPE, version)


def test_lineage_and_gate_reports_round_trip_without_scope_leakage() -> None:
    store = AgentStudioStore()
    edge = _lineage()
    report = _gate_report()

    assert store.add_lineage_edge(SCOPE, edge) == edge
    assert store.list_lineage(SCOPE, AGENT_ID) == (edge,)
    assert store.list_lineage(SCOPE, OTHER_AGENT_ID) == (edge,)
    assert store.list_lineage(SCOPE, "agent-unrelated") == ()
    assert store.list_lineage(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.list_lineage(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    assert store.save_gate_report(report) == report
    assert store.get_gate_report(report.id) == report
    assert store.get_gate_report("missing-report") is None


def test_releases_round_trip_latest_transition_and_scope_guards() -> None:
    store = AgentStudioStore()
    gated = _release(release_id="release-1")
    active = _release(
        release_id="release-2",
        status=ReleaseStatus.ACTIVE,
        previous_release_id=gated.id,
    )
    other_version = _release(release_id="release-3", version_id="version-2")

    assert store.latest_release_for_version(SCOPE, "version-1") is None
    assert store.create_release(SCOPE, gated) == gated
    assert store.create_release(SCOPE, active) == active
    assert store.create_release(SCOPE, other_version) == other_version
    assert store.get_release(SCOPE, gated.id) == gated
    assert store.get_release(SCOPE, "missing-release") is None
    assert store.list_releases_for_version(SCOPE, "version-1") == (gated, active)
    assert store.latest_release_for_version(SCOPE, "version-1") == active
    assert store.list_releases_for_version(SCOPE, "version-2") == (other_version,)
    assert store.get_release(SAME_TENANT_OTHER_PROJECT_SCOPE, gated.id) is None
    assert store.list_releases_for_version(SAME_TENANT_OTHER_PROJECT_SCOPE, "version-1") == ()
    assert store.latest_release_for_version(SAME_TENANT_OTHER_PROJECT_SCOPE, "version-1") is None
    assert store.get_release(OTHER_TENANT_SAME_PROJECT_SCOPE, gated.id) is None
    assert store.list_releases_for_version(OTHER_TENANT_SAME_PROJECT_SCOPE, "version-1") == ()
    assert store.latest_release_for_version(OTHER_TENANT_SAME_PROJECT_SCOPE, "version-1") is None


def test_approvals_are_idempotent_only_while_pending_and_scope_isolation_holds() -> None:
    store = AgentStudioStore()
    pending = _approval()
    duplicate_pending = _approval(approval_id="approval-duplicate")

    assert store.create_approval(SCOPE, pending) == pending
    assert store.create_approval(SCOPE, duplicate_pending) == pending
    assert store.find_pending_approval(SCOPE, pending.idempotency_key) == pending
    assert store.find_pending_approval(SAME_TENANT_OTHER_PROJECT_SCOPE, pending.idempotency_key) is None
    assert store.find_pending_approval(OTHER_TENANT_SAME_PROJECT_SCOPE, pending.idempotency_key) is None

    approved = pending.model_copy(update={"state": ApprovalState.APPROVED, "approver_id": "approver-1"})
    assert store.save_approval_decision(SCOPE, approved) == approved
    assert store.find_pending_approval(SCOPE, pending.idempotency_key) is None
    assert store.get_approval(SCOPE, pending.id) == approved
    assert store.list_approvals(SCOPE, version_id=pending.version_id) == (approved,)

    replacement = _approval(approval_id="approval-2", idempotency_key="key-2")
    assert store.create_approval(SCOPE, replacement) == replacement
    assert store.list_approvals(SCOPE) == (approved, replacement)
    assert store.list_approvals(SCOPE, version_id="missing-version") == ()
    assert store.get_approval(SAME_TENANT_OTHER_PROJECT_SCOPE, pending.id) is None
    assert store.list_approvals(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert store.get_approval(OTHER_TENANT_SAME_PROJECT_SCOPE, pending.id) is None
    assert store.list_approvals(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_approval_decision(SCOPE, approved)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.save_approval_decision(SCOPE, _approval(approval_id="missing-approval", idempotency_key="key-missing"))


def test_deployments_round_trip_update_and_scope_guards() -> None:
    store = AgentStudioStore()
    deployment = _deployment()
    other_agent = _deployment(deployment_id="deployment-2", logical_agent_id=OTHER_AGENT_ID)

    assert store.create_deployment(SCOPE, deployment) == deployment
    assert store.create_deployment(SCOPE, other_agent) == other_agent
    assert store.list_deployments(SCOPE, AGENT_ID) == (deployment,)
    assert store.list_deployments(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    assert store.get_deployment(SCOPE, deployment.id) == deployment
    assert store.get_deployment(SCOPE, "missing-deployment") is None
    assert store.get_deployment(SAME_TENANT_OTHER_PROJECT_SCOPE, deployment.id) is None
    assert store.list_deployments(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.get_deployment(OTHER_TENANT_SAME_PROJECT_SCOPE, deployment.id) is None
    assert store.list_deployments(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    updated = deployment.model_copy(update={"trace_ref": "trace-1"})
    assert store.update_deployment(SCOPE, updated) == updated
    assert store.get_deployment(SCOPE, deployment.id) == updated

    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.update_deployment(SCOPE, _deployment(deployment_id="missing-deployment"))


def test_bindings_and_tool_registrations_round_trip_without_cross_scope_visibility() -> None:
    store = AgentStudioStore()
    binding = _binding()
    registration = _tool_registration()
    other_registration = _tool_registration(registration_id="reg-2", logical_agent_id=OTHER_AGENT_ID)

    assert store.set_binding(SCOPE, binding) == binding
    assert store.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert store.get_binding(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert store.get_binding(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None

    assert store.create_tool_registration(SCOPE, registration) == registration
    assert store.create_tool_registration(SCOPE, other_registration) == other_registration
    assert store.list_tool_registrations(SCOPE, AGENT_ID) == (registration,)
    assert store.list_tool_registrations(SCOPE, OTHER_AGENT_ID) == (other_registration,)
    assert store.list_tool_registrations(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.list_tool_registrations(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()


def test_builder_proposals_round_trip_and_decisions_validate_state() -> None:
    store = AgentStudioStore()
    pending = _proposal()
    other_agent = _proposal(proposal_id="proposal-2", logical_agent_id=OTHER_AGENT_ID)

    assert store.create_builder_proposal(SCOPE, pending) == pending
    assert store.create_builder_proposal(SCOPE, other_agent) == other_agent
    assert store.get_builder_proposal(SCOPE, pending.id) == pending
    assert store.get_builder_proposal(SCOPE, "missing-proposal") is None
    assert store.list_builder_proposals(SCOPE, AGENT_ID) == (pending,)
    assert store.list_builder_proposals(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    assert store.get_builder_proposal(SAME_TENANT_OTHER_PROJECT_SCOPE, pending.id) is None
    assert store.list_builder_proposals(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert store.get_builder_proposal(OTHER_TENANT_SAME_PROJECT_SCOPE, pending.id) is None
    assert store.list_builder_proposals(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    decided = pending.model_copy(update={"state": BuilderProposalState.APPLIED, "decided_by": "reviewer-1"})
    assert store.save_builder_proposal_decision(SCOPE, decided) == decided
    assert store.get_builder_proposal(SCOPE, pending.id) == decided

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_builder_proposal_decision(SCOPE, decided)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.save_builder_proposal_decision(SCOPE, _proposal(proposal_id="missing-proposal"))
