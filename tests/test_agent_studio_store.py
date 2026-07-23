from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    AgentVersion,
    AgentVersionStatus,
    ApprovalKind,
    ApprovalState,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistration,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.store import AgentStudioStore, AgentStudioStoreError


def _manifest() -> AgentManifest:
    return AgentManifest(
        logical_agent_id="agent-store-test",
        tenant_id="demo",
        display_name="Store Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
    )


def _draft() -> AgentDraft:
    return AgentDraft(
        logical_agent_id="agent-store-test",
        tenant_id="demo",
        manifest=_manifest(),
        updated_by="user-1",
    )


def test_save_and_get_draft() -> None:
    store = AgentStudioStore()
    store.save_draft(_draft())
    assert store.get_draft("demo", "agent-store-test") is not None
    assert store.get_draft("other-tenant", "agent-store-test") is None
    assert len(store.list_drafts("demo")) == 1
    assert store.list_drafts("other-tenant") == ()


def test_ownership_role_precedence_returns_highest_role() -> None:
    store = AgentStudioStore()
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-1", principal_id="user-1",
            role=AgentRole.VIEWER, granted_by="admin",
        )
    )
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-1", principal_id="user-1",
            role=AgentRole.OWNER, granted_by="admin",
        )
    )
    assert store.role_for("demo", "agent-1", "user-1") == AgentRole.OWNER
    assert store.role_for("demo", "agent-1", "unknown-user") is None
    assert len(store.list_ownership("demo", "agent-1")) == 2


def test_role_for_falls_through_to_none_if_role_not_in_precedence_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defensive branch: every AgentRole is covered by _ROLE_PRECEDENCE today,
    # so this simulates a future role value that the precedence table
    # doesn't (yet) enumerate, to ensure role_for degrades to None rather
    # than raising.
    import research_assistant_api.agent_studio.store as store_module

    store = AgentStudioStore()
    store.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-1", principal_id="user-1",
            role=AgentRole.VIEWER, granted_by="admin",
        )
    )
    monkeypatch.setattr(store_module, "_ROLE_PRECEDENCE", ())
    assert store.role_for("demo", "agent-1", "user-1") is None


def _version(sequence: int = 1, **overrides: object) -> AgentVersion:
    base = dict(
        id=f"version-{sequence}",
        logical_agent_id="agent-store-test",
        tenant_id="demo",
        sequence=sequence,
        manifest=_manifest(),
        manifest_hash="hash",
        created_by="user-1",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )
    base.update(overrides)
    return AgentVersion(**base)  # type: ignore[arg-type]


def test_create_version_rejects_duplicate_id() -> None:
    store = AgentStudioStore()
    version = _version()
    store.create_version(version)
    with pytest.raises(AgentStudioStoreError, match="already exists"):
        store.create_version(version)
    assert store.next_sequence("demo", "agent-store-test") == 2


def test_get_version_enforces_tenant_isolation() -> None:
    store = AgentStudioStore()
    store.create_version(_version())
    assert store.get_version("demo", "version-1") is not None
    assert store.get_version("other-tenant", "version-1") is None
    assert store.get_version("demo", "missing") is None


def test_list_versions_returns_all_for_agent() -> None:
    store = AgentStudioStore()
    store.create_version(_version(1))
    store.create_version(_version(2, id="version-2"))
    assert len(store.list_versions("demo", "agent-store-test")) == 2


def test_update_version_status_raises_for_missing_version() -> None:
    store = AgentStudioStore()
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.update_version_status("demo", "missing", AgentVersionStatus.GATED)


def test_update_version_status_updates_in_place() -> None:
    store = AgentStudioStore()
    store.create_version(_version())
    updated = store.update_version_status("demo", "version-1", AgentVersionStatus.GATED)
    assert updated.status == AgentVersionStatus.GATED
    reloaded = store.get_version("demo", "version-1")
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.GATED


def test_attach_gate_report_raises_for_missing_version() -> None:
    store = AgentStudioStore()
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.attach_gate_report("demo", "missing", "report-1")


def test_attach_gate_report_updates_version() -> None:
    store = AgentStudioStore()
    store.create_version(_version())
    updated = store.attach_gate_report("demo", "version-1", "report-1")
    assert updated.gate_report_id == "report-1"


def test_lineage_edges_filtered_by_tenant_and_agent() -> None:
    store = AgentStudioStore()
    edge = LineageEdge(
        tenant_id="demo",
        child_logical_agent_id="agent-child",
        child_version_id="v-child",
        parent_logical_agent_id="agent-parent",
        parent_version_id="v-parent",
    )
    store.add_lineage_edge(edge)
    assert store.list_lineage("demo", "agent-child") == (edge,)
    assert store.list_lineage("demo", "agent-parent") == (edge,)
    assert store.list_lineage("demo", "unrelated-agent") == ()
    assert store.list_lineage("other-tenant", "agent-child") == ()


def test_gate_report_round_trip() -> None:
    store = AgentStudioStore()
    report = ReleaseGateReport(id="report-1", version_id="version-1", results=())
    store.save_gate_report(report)
    assert store.get_gate_report("report-1") == report
    assert store.get_gate_report("missing") is None


def _approval(kind: ApprovalKind = ApprovalKind.RELEASE_PROMOTION) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id="approval-1",
        version_id="version-1",
        tenant_id="demo",
        kind=kind,
        gated_action="promote_version",
        destination="prod",
        requested_by="user-1",
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key="key-1",
    )


def test_create_approval_is_idempotent_for_pending_requests() -> None:
    store = AgentStudioStore()
    first = store.create_approval(_approval())
    duplicate = _approval()
    second = store.create_approval(duplicate)
    assert second is first
    assert len(store.list_approvals("demo")) == 1


def test_find_pending_approval_returns_none_when_not_pending_or_wrong_tenant() -> None:
    store = AgentStudioStore()
    store.create_approval(_approval())
    assert store.find_pending_approval("other-tenant", "key-1") is None
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})
    store2 = AgentStudioStore()
    store2.create_approval(decided)
    assert store2.find_pending_approval("demo", "key-1") is None


def test_get_approval_enforces_tenant_isolation() -> None:
    store = AgentStudioStore()
    store.create_approval(_approval())
    assert store.get_approval("demo", "approval-1") is not None
    assert store.get_approval("other-tenant", "approval-1") is None
    assert store.get_approval("demo", "missing") is None


def test_save_approval_decision_raises_for_missing_record() -> None:
    store = AgentStudioStore()
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.save_approval_decision(_approval())


def test_save_approval_decision_raises_when_already_decided() -> None:
    store = AgentStudioStore()
    store.create_approval(_approval())
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})
    store.save_approval_decision(decided)
    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_approval_decision(decided)


def test_list_approvals_filters_by_version_id() -> None:
    store = AgentStudioStore()
    store.create_approval(_approval())
    other = _approval().model_copy(update={"id": "approval-2", "version_id": "version-2", "idempotency_key": "key-2"})
    store.create_approval(other)
    assert len(store.list_approvals("demo")) == 2
    assert len(store.list_approvals("demo", version_id="version-1")) == 1


def _deployment(**overrides: object) -> DeploymentRecord:
    base: dict[str, object] = dict(
        id="deployment-1",
        logical_agent_id="agent-store-test",
        tenant_id="demo",
        version_id="version-1",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by="user-1",
    )
    base.update(overrides)
    return DeploymentRecord(**base)  # type: ignore[arg-type]


def test_deployment_crud_round_trip() -> None:
    store = AgentStudioStore()
    store.create_deployment(_deployment())
    assert store.get_deployment("demo", "deployment-1") is not None
    assert store.get_deployment("other-tenant", "deployment-1") is None
    assert len(store.list_deployments("demo", "agent-store-test")) == 1
    updated = _deployment().model_copy(update={"trace_ref": "trace-1"})
    result = store.update_deployment(updated)
    assert result.trace_ref == "trace-1"


def test_update_deployment_raises_for_missing_record() -> None:
    store = AgentStudioStore()
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.update_deployment(_deployment())


def test_logical_agent_binding_round_trip() -> None:
    store = AgentStudioStore()
    binding = LogicalAgentBinding(
        logical_agent_id="agent-store-test",
        tenant_id="demo",
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id="version-1",
        updated_by="user-1",
    )
    store.set_binding(binding)
    assert store.get_binding("demo", "agent-store-test", DeploymentEnvironment.DEVELOPMENT) == binding
    assert store.get_binding("other-tenant", "agent-store-test", DeploymentEnvironment.DEVELOPMENT) is None


def test_tool_registration_round_trip_and_tenant_scoping() -> None:
    store = AgentStudioStore()
    registration = ToolRegistration(
        id="reg-1",
        tenant_id="demo",
        logical_agent_id="agent-store-test",
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by="user-1",
    )
    store.create_tool_registration(registration)
    assert store.list_tool_registrations("demo", "agent-store-test") == (registration,)
    assert store.list_tool_registrations("other-tenant", "agent-store-test") == ()
    assert store.list_tool_registrations("demo", "agent-other") == ()
