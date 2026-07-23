from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_registry import CapabilityAttachmentError, default_registry
from research_assistant_api.agent_studio.models import (
    AgentOwnerKind,
    AgentRole,
    AgentVersion,
    AgentVersionStatus,
    AgentVisibility,
    ApprovalState,
    CapabilityBinding,
    ModelDeploymentRef,
    StudioApprovalRecord,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    ReleaseService,
    ReleaseServiceError,
    manifest_hash,
    resolve_actor_role,
)
from research_assistant_api.agent_studio.store import AgentStudioStore


@pytest.fixture
def service() -> ReleaseService:
    return ReleaseService(AgentStudioStore(), default_registry())


def test_manifest_hash_is_deterministic() -> None:
    service = ReleaseService(AgentStudioStore(), default_registry())
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-hash", display_name="Hash Agent",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    assert manifest_hash(draft.manifest) == manifest_hash(draft.manifest)
    other_hash = manifest_hash(draft.manifest.model_copy(update={"display_name": "Different"}))
    assert other_hash != manifest_hash(draft.manifest)


def test_create_agent_user_owned_succeeds_without_platform_owner(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    assert draft.manifest.owner_kind == AgentOwnerKind.USER
    resolved_role = resolve_actor_role(
        service._store, tenant_id="demo", logical_agent_id="agent-one", principal_id="user-1"
    )
    assert resolved_role == AgentRole.OWNER


def test_create_agent_system_owned_requires_platform_owner(service: ReleaseService) -> None:
    with pytest.raises(AuthorizationError, match="platform owners"):
        service.create_agent(
            tenant_id="demo", logical_agent_id="agent-sys", display_name="System Agent",
            owner_kind=AgentOwnerKind.SYSTEM, owner_id="platform", requested_by="user-1", is_platform_owner=False,
        )


def test_create_agent_system_owned_succeeds_for_platform_owner(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-sys", display_name="System Agent",
        owner_kind=AgentOwnerKind.SYSTEM, owner_id="platform", requested_by="admin-1", is_platform_owner=True,
    )
    assert draft.manifest.owner_kind == AgentOwnerKind.SYSTEM


def test_create_agent_rejects_duplicate_logical_agent_id(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(ReleaseServiceError, match="already exists"):
        service.create_agent(
            tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One Again",
            owner_kind=AgentOwnerKind.USER, owner_id="user-2", requested_by="user-2", is_platform_owner=False,
        )


def test_update_draft_requires_contributor_role(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.update_draft(
            tenant_id="demo", logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"display_name": "Renamed"}),
            updated_by="user-2", actor_role=AgentRole.VIEWER,
        )


def test_update_draft_rejects_mismatched_ids(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(ReleaseServiceError, match="must match"):
        service.update_draft(
            tenant_id="demo", logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"logical_agent_id": "other-agent"}),
            updated_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_update_draft_raises_when_no_draft_exists(service: ReleaseService) -> None:
    from research_assistant_api.agent_studio.models import AgentManifest

    manifest = AgentManifest(
        logical_agent_id="agent-missing", tenant_id="demo", display_name="Missing",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1",
    )
    with pytest.raises(ReleaseServiceError, match="has no draft"):
        service.update_draft(
            tenant_id="demo", logical_agent_id="agent-missing", manifest=manifest,
            updated_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_update_draft_succeeds_with_contributor_role(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    updated = service.update_draft(
        tenant_id="demo", logical_agent_id="agent-one",
        manifest=draft.manifest.model_copy(update={"display_name": "Renamed Agent"}),
        updated_by="user-1", actor_role=AgentRole.CONTRIBUTOR,
    )
    assert updated.manifest.display_name == "Renamed Agent"


def test_fork_rejects_unknown_source_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.fork(
            tenant_id="demo", source_logical_agent_id="agent-one", source_version_id="missing-version",
            new_logical_agent_id="agent-fork", requested_by="user-2",
        )


def test_fork_rejects_cross_tenant_source_version(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="tenant-a", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id="tenant-a", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER,
    )
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.fork(
            tenant_id="tenant-b", source_logical_agent_id="agent-one", source_version_id=version.id,
            new_logical_agent_id="agent-fork", requested_by="user-2",
        )
    assert draft.manifest.tenant_id == "tenant-a"


def test_fork_succeeds_and_creates_private_user_owned_draft(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.SYSTEM, owner_id="platform", requested_by="admin-1", is_platform_owner=True,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="admin-1", actor_role=AgentRole.OWNER,
    )
    forked = service.fork(
        tenant_id="demo", source_logical_agent_id="agent-one", source_version_id=version.id,
        new_logical_agent_id="agent-one-fork", requested_by="researcher-1",
    )
    assert forked.manifest.owner_kind == AgentOwnerKind.USER
    assert forked.manifest.owner_id == "researcher-1"
    assert forked.manifest.visibility == AgentVisibility.PRIVATE
    assert forked.based_on_version_id == version.id
    assert resolve_actor_role(
        service._store, tenant_id="demo", logical_agent_id="agent-one-fork", principal_id="researcher-1"
    ) == AgentRole.OWNER


def test_fork_rejects_duplicate_new_logical_agent_id(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER,
    )
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one-fork", display_name="Existing",
        owner_kind=AgentOwnerKind.USER, owner_id="user-2", requested_by="user-2", is_platform_owner=False,
    )
    with pytest.raises(ReleaseServiceError, match="already exists"):
        service.fork(
            tenant_id="demo", source_logical_agent_id="agent-one", source_version_id=version.id,
            new_logical_agent_id="agent-one-fork", requested_by="user-2",
        )


def test_cut_version_requires_contributor_role(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(AuthorizationError):
        service.cut_version(
            tenant_id="demo", logical_agent_id="agent-one", actor_id="user-2", actor_role=AgentRole.VIEWER
        )


def test_cut_version_raises_when_no_draft(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="has no draft"):
        service.cut_version(
            tenant_id="demo", logical_agent_id="agent-missing", actor_id="user-1", actor_role=AgentRole.OWNER
        )


def test_cut_version_sequence_and_parent_lineage(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    first = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    assert first.sequence == 1
    assert first.parent_version_id is None
    assert first.fork_of_version_id is None

    second = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    assert second.sequence == 2
    assert second.parent_version_id == first.id


def test_cut_version_of_forked_draft_records_lineage_edge(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    parent_version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    service.fork(
        tenant_id="demo", source_logical_agent_id="agent-one", source_version_id=parent_version.id,
        new_logical_agent_id="agent-one-fork", requested_by="user-2",
    )
    fork_version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one-fork", actor_id="user-2", actor_role=AgentRole.OWNER
    )
    assert fork_version.fork_of_version_id == parent_version.id
    lineage = service._store.list_lineage("demo", "agent-one-fork")
    assert len(lineage) == 1
    assert lineage[0].parent_version_id == parent_version.id
    assert lineage[0].child_version_id == fork_version.id

    # Cutting a *second* version of the fork must not re-record fork lineage.
    second_fork_version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one-fork", actor_id="user-2", actor_role=AgentRole.OWNER
    )
    assert second_fork_version.fork_of_version_id is None
    assert len(service._store.list_lineage("demo", "agent-one-fork")) == 1


def test_cut_version_skips_lineage_edge_when_fork_source_no_longer_resolves(service: ReleaseService) -> None:
    # Defensive branch: the draft references a fork source version_id that
    # cannot be resolved in the store (e.g. it was deleted out-of-band).
    # cut_version must still succeed, simply without recording a lineage edge.
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    draft = service._store.get_draft("demo", "agent-one")
    assert draft is not None
    service._store.save_draft(draft.model_copy(update={"based_on_version_id": "ghost-version"}))

    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    assert version.fork_of_version_id == "ghost-version"
    assert service._store.list_lineage("demo", "agent-one") == ()


def test_run_release_gates_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.run_release_gates(tenant_id="demo", version_id="missing", evidence=GateEvidence())


def test_run_release_gates_updates_status_to_gated_on_pass(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    report = service.run_release_gates(
        tenant_id="demo", version_id=version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    assert report.passed
    reloaded = service._store.get_version("demo", version.id)
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.GATED
    assert reloaded.gate_report_id == report.id


def test_run_release_gates_leaves_status_as_draft_on_failure(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    report = service.run_release_gates(tenant_id="demo", version_id=version.id, evidence=GateEvidence())
    assert not report.passed
    reloaded = service._store.get_version("demo", version.id)
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.DRAFT


def _gated_version(
    service: ReleaseService,
    tenant_id: str = "demo",
    logical_agent_id: str = "agent-one",
    owner: str = "user-1",
) -> AgentVersion:
    service.create_agent(
        tenant_id=tenant_id, logical_agent_id=logical_agent_id, display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id=owner, requested_by=owner, is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id=tenant_id, logical_agent_id=logical_agent_id, actor_id=owner, actor_role=AgentRole.OWNER
    )
    service.run_release_gates(
        tenant_id=tenant_id, version_id=version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    reloaded = service._store.get_version(tenant_id, version.id)
    assert reloaded is not None
    return reloaded


def test_request_promotion_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.request_promotion(
            tenant_id="demo", version_id="missing", actor_id="user-1", actor_role=AgentRole.OWNER,
            destination="prod", evidence_summary="evidence",
        )


def test_request_promotion_raises_when_not_gated(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-one", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    with pytest.raises(ReleaseServiceError, match="must pass all hard gates"):
        service.request_promotion(
            tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.OWNER,
            destination="prod", evidence_summary="evidence",
        )


def test_request_promotion_auto_promotes_for_maintainer_role(service: ReleaseService) -> None:
    version = _gated_version(service)
    result = service.request_promotion(
        tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.MAINTAINER,
        destination="prod", evidence_summary="All gates green.",
    )
    assert isinstance(result, AgentVersion)
    assert result.status == AgentVersionStatus.RELEASED


def test_request_promotion_requires_approval_for_contributor_role(service: ReleaseService) -> None:
    version = _gated_version(service)
    result = service.request_promotion(
        tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.CONTRIBUTOR,
        destination="prod", evidence_summary="All gates green.",
    )
    assert isinstance(result, StudioApprovalRecord)
    assert result.state == ApprovalState.PENDING
    assert result.kind.value == "release_promotion"


def test_request_promotion_uses_fork_promotion_kind_when_forked(service: ReleaseService) -> None:
    parent = _gated_version(service, logical_agent_id="agent-parent")
    service.fork(
        tenant_id="demo", source_logical_agent_id="agent-parent", source_version_id=parent.id,
        new_logical_agent_id="agent-fork", requested_by="user-2",
    )
    fork_version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-fork", actor_id="user-2", actor_role=AgentRole.OWNER
    )
    service.run_release_gates(
        tenant_id="demo", version_id=fork_version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    result = service.request_promotion(
        tenant_id="demo", version_id=fork_version.id, actor_id="user-2", actor_role=AgentRole.CONTRIBUTOR,
        destination="prod", evidence_summary="Forked agent ready.",
    )
    assert isinstance(result, StudioApprovalRecord)
    assert result.kind.value == "fork_promotion"


def test_decide_promotion_raises_for_missing_approval(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.decide_promotion(
            tenant_id="demo", approval_id="missing", approver_id="approver-1", approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_promotion_approves_and_releases_version(service: ReleaseService) -> None:
    version = _gated_version(service)
    request = service.request_promotion(
        tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.CONTRIBUTOR,
        destination="prod", evidence_summary="Ready.",
    )
    decided = service.decide_promotion(
        tenant_id="demo", approval_id=request.id, approver_id="maintainer-1", approver_role=AgentRole.MAINTAINER,
        approve=True, rationale="LGTM",
    )
    assert decided.state == ApprovalState.APPROVED
    reloaded = service._store.get_version("demo", version.id)
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.RELEASED


def test_decide_promotion_rejection_does_not_release_version(service: ReleaseService) -> None:
    version = _gated_version(service)
    request = service.request_promotion(
        tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.CONTRIBUTOR,
        destination="prod", evidence_summary="Ready.",
    )
    decided = service.decide_promotion(
        tenant_id="demo", approval_id=request.id, approver_id="maintainer-1", approver_role=AgentRole.MAINTAINER,
        approve=False,
    )
    assert decided.state == ApprovalState.REJECTED
    reloaded = service._store.get_version("demo", version.id)
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.GATED


def test_request_role_escalation_creates_pending_approval(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    record = service.request_role_escalation(
        tenant_id="demo", logical_agent_id="agent-one", requested_by="user-2",
        requested_role=AgentRole.MAINTAINER, evidence_summary="Needs maintainer access.",
    )
    assert record.state == ApprovalState.PENDING
    assert record.kind.value == "admin_escalation"
    assert record.requested_role == AgentRole.MAINTAINER


def test_decide_role_escalation_raises_for_missing_approval(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.decide_role_escalation(
            tenant_id="demo", approval_id="missing", approver_id="owner-1", approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_role_escalation_raises_for_non_escalation_approval(service: ReleaseService) -> None:
    version = _gated_version(service)
    promotion = service.request_promotion(
        tenant_id="demo", version_id=version.id, actor_id="user-1", actor_role=AgentRole.CONTRIBUTOR,
        destination="prod", evidence_summary="Ready.",
    )
    with pytest.raises(ReleaseServiceError, match="not an admin escalation"):
        service.decide_role_escalation(
            tenant_id="demo", approval_id=promotion.id, approver_id="owner-1", approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_role_escalation_approved_grants_role(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    record = service.request_role_escalation(
        tenant_id="demo", logical_agent_id="agent-one", requested_by="user-2",
        requested_role=AgentRole.MAINTAINER, evidence_summary="Needs maintainer access.",
    )
    decided = service.decide_role_escalation(
        tenant_id="demo", approval_id=record.id, approver_id="user-1", approver_role=AgentRole.OWNER, approve=True,
    )
    assert decided.state == ApprovalState.APPROVED
    assert resolve_actor_role(
        service._store, tenant_id="demo", logical_agent_id="agent-one", principal_id="user-2"
    ) == AgentRole.MAINTAINER


def test_decide_role_escalation_rejected_does_not_grant_role(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-one", display_name="Agent One",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    record = service.request_role_escalation(
        tenant_id="demo", logical_agent_id="agent-one", requested_by="user-2",
        requested_role=AgentRole.MAINTAINER, evidence_summary="Needs maintainer access.",
    )
    decided = service.decide_role_escalation(
        tenant_id="demo", approval_id=record.id, approver_id="user-1", approver_role=AgentRole.OWNER, approve=False,
    )
    assert decided.state == ApprovalState.REJECTED
    assert resolve_actor_role(
        service._store, tenant_id="demo", logical_agent_id="agent-one", principal_id="user-2"
    ) == AgentRole.VIEWER


def test_resolve_actor_role_defaults_to_viewer_for_unknown_principal(service: ReleaseService) -> None:
    assert resolve_actor_role(
        service._store, tenant_id="demo", logical_agent_id="agent-unknown", principal_id="ghost"
    ) == AgentRole.VIEWER


# -- cut_version new contract fields ---------------------------------------


def test_cut_version_populates_model_deployment_capability_versions_and_package_protocol(
    service: ReleaseService,
) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-cut-fields", display_name="Agent Cut Fields",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    draft = service._store.get_draft("demo", "agent-cut-fields")
    assert draft is not None
    model_deployment = ModelDeploymentRef(
        deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"
    )
    binding = CapabilityBinding(
        descriptor_id="foundry.web_search", operation="search", attached_by="user-1", descriptor_version="1"
    )
    updated_manifest = draft.manifest.model_copy(
        update={"model_deployment": model_deployment, "capabilities": (binding,)}
    )
    service.update_draft(
        tenant_id="demo", logical_agent_id="agent-cut-fields", manifest=updated_manifest,
        updated_by="user-1", actor_role=AgentRole.OWNER,
    )
    version = service.cut_version(
        tenant_id="demo", logical_agent_id="agent-cut-fields", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    assert version.model_deployment == model_deployment
    assert version.capability_versions == {"foundry.web_search": "1"}
    assert version.package_version == "1.0.0"
    assert version.protocol_version == "agent-studio.protocol.v1"


# -- Tool registration ------------------------------------------------------


def test_register_tool_requires_contributor_role(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-tool", display_name="Agent Tool",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(AuthorizationError):
        service.register_tool(
            tenant_id="demo", logical_agent_id="agent-tool", descriptor_id="foundry.web_search",
            operation="search", kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
            handler_ref="builtin://web_search", registered_by="user-2", actor_role=AgentRole.VIEWER,
        )


def test_register_tool_rejects_non_ga_operation(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-tool-nonga", display_name="Agent Tool NonGA",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    with pytest.raises(CapabilityAttachmentError):
        service.register_tool(
            tenant_id="demo", logical_agent_id="agent-tool-nonga", descriptor_id="foundry.memory",
            operation="recall", kind=ToolRegistrationKind.CUSTOM_HANDLER,
            handler_ref="custom://memory", registered_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_register_tool_succeeds_and_list_tool_registrations_returns_it(service: ReleaseService) -> None:
    service.create_agent(
        tenant_id="demo", logical_agent_id="agent-tool-ok", display_name="Agent Tool OK",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    registration = service.register_tool(
        tenant_id="demo", logical_agent_id="agent-tool-ok", descriptor_id="foundry.web_search",
        operation="search", kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search", registered_by="user-1", actor_role=AgentRole.OWNER,
    )
    assert registration.descriptor_id == "foundry.web_search"
    listed = service.list_tool_registrations("demo", "agent-tool-ok")
    assert listed == (registration,)
    assert service.list_tool_registrations("demo", "agent-tool-other") == ()
