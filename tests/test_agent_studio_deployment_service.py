# mypy: disable-error-code=import-untyped
from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_registry import default_registry
from research_assistant_api.agent_studio.deployment_service import DeploymentService, DeploymentServiceError
from research_assistant_api.agent_studio.models import (
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    CapabilityBinding,
    DeploymentEnvironment,
    HealthStatus,
    LogicalAgentBinding,
    ReleaseStatus,
    RuntimeRequirements,
    RuntimeTarget,
    SchemaRef,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence
from research_assistant_api.agent_studio.release_service import ReleaseService
from research_assistant_api.agent_studio.store import AgentStudioStore


@pytest.fixture
def store() -> AgentStudioStore:
    return AgentStudioStore()


@pytest.fixture
def release_service(store: AgentStudioStore) -> ReleaseService:
    return ReleaseService(store, default_registry())


@pytest.fixture
def deployment_service(store: AgentStudioStore) -> DeploymentService:
    return DeploymentService(store)


def _create_agent(
    release_service: ReleaseService,
    *,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
) -> None:
    release_service.create_agent(
        tenant_id="demo",
        logical_agent_id=logical_agent_id,
        display_name="Deploy Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id=owner,
        requested_by=owner,
        is_platform_owner=False,
    )


def _cut_version(
    release_service: ReleaseService,
    *,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
) -> AgentVersion:
    _create_agent(release_service, logical_agent_id=logical_agent_id, owner=owner)
    return release_service.cut_version(
        tenant_id="demo",
        logical_agent_id=logical_agent_id,
        actor_id=owner,
        actor_role=AgentRole.OWNER,
    )


def _pass_gates(release_service: ReleaseService, version: AgentVersion) -> None:
    release_service.run_release_gates(
        tenant_id="demo",
        version_id=version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )


def _append_release(
    store: AgentStudioStore,
    version: AgentVersion,
    status: ReleaseStatus,
    *,
    created_by: str = "user-1",
) -> AgentRelease:
    previous = store.latest_release_for_version(version.tenant_id, version.id)
    release = AgentRelease(
        id=f"{version.id}-{status.value}-{len(store.list_releases_for_version(version.tenant_id, version.id)) + 1}",
        version_id=version.id,
        logical_agent_id=version.logical_agent_id,
        tenant_id=version.tenant_id,
        status=status,
        previous_release_id=previous.id if previous is not None else None,
        created_by=created_by,
        detail=f"Transitioned to {status.value}.",
    )
    return store.create_release(release)


def _version_with_latest_release(
    release_service: ReleaseService,
    store: AgentStudioStore,
    *,
    latest_status: ReleaseStatus,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
) -> tuple[AgentVersion, AgentRelease]:
    version = _cut_version(release_service, logical_agent_id=logical_agent_id, owner=owner)
    _pass_gates(release_service, version)
    release = store.latest_release_for_version(version.tenant_id, version.id)
    assert release is not None
    for next_status in {
        ReleaseStatus.GATED: (),
        ReleaseStatus.APPROVED: (ReleaseStatus.APPROVED,),
        ReleaseStatus.ACTIVE: (ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE),
        ReleaseStatus.DEPRECATED: (ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE, ReleaseStatus.DEPRECATED),
        ReleaseStatus.ROLLED_BACK: (ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE, ReleaseStatus.ROLLED_BACK),
    }[latest_status]:
        release = _append_release(store, version, next_status, created_by=owner)
    return version, release


@pytest.mark.parametrize(
    ("latest_status", "logical_agent_id"),
    [
        (ReleaseStatus.GATED, "agent-deploy-gated"),
        (ReleaseStatus.APPROVED, "agent-deploy-approved"),
        (ReleaseStatus.ACTIVE, "agent-deploy-active"),
    ],
)
def test_deploy_accepts_deployable_release_statuses(
    latest_status: ReleaseStatus,
    logical_agent_id: str,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, release = _version_with_latest_release(
        release_service,
        store,
        latest_status=latest_status,
        logical_agent_id=logical_agent_id,
    )

    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id=logical_agent_id,
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
        trace_ref="trace-abc",
    )

    assert record.logical_agent_id == logical_agent_id
    assert record.version_id == version.id
    assert record.environment == DeploymentEnvironment.DEVELOPMENT
    assert record.runtime_target == version.runtime_target
    assert record.trace_ref == "trace-abc"
    binding = store.get_binding("demo", logical_agent_id, DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == version.id

    resolved = deployment_service.resolve(tenant_id="demo", logical_agent_id=logical_agent_id)
    assert resolved is not None
    assert resolved.version_id == version.id
    assert resolved.release_id == release.id
    assert resolved.release_status is latest_status


def test_deploy_requires_contributor_role(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)

    with pytest.raises(DeploymentServiceError, match="cannot create deployments"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-2",
            actor_role=AgentRole.VIEWER,
        )


def test_deploy_raises_for_unknown_version(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            version_id="missing",
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_version_belongs_to_different_agent(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-other",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_version_has_no_release_record(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
) -> None:
    version = _cut_version(release_service)

    with pytest.raises(DeploymentServiceError, match="release status 'none'"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


@pytest.mark.parametrize("latest_status", [ReleaseStatus.DEPRECATED, ReleaseStatus.ROLLED_BACK])
def test_deploy_rejects_non_deployable_release_statuses(
    latest_status: ReleaseStatus,
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=latest_status)

    with pytest.raises(DeploymentServiceError, match=rf"release status '{latest_status.value}'"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_runtime_target_not_resolved(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    store._versions[version.id] = version.model_copy(update={"runtime_target": None})

    with pytest.raises(DeploymentServiceError, match="no runtime_target resolved"):
        deployment_service.deploy(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_record_health_raises_for_missing_deployment(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.record_health(tenant_id="demo", deployment_id="missing", status=HealthStatus.HEALTHY)


def test_record_health_updates_deployment_health_and_trace(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    updated = deployment_service.record_health(
        tenant_id="demo",
        deployment_id=record.id,
        status=HealthStatus.DEGRADED,
        detail="latency spike",
        trace_ref="trace-xyz",
    )

    assert updated.health.status is HealthStatus.DEGRADED
    assert updated.health.detail == "latency spike"
    assert updated.trace_ref == "trace-xyz"
    persisted = store.get_deployment("demo", record.id)
    assert persisted is not None
    assert persisted.health.status is HealthStatus.DEGRADED


def test_record_health_without_trace_ref_leaves_existing_trace(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
        trace_ref="trace-original",
    )

    updated = deployment_service.record_health(
        tenant_id="demo",
        deployment_id=record.id,
        status=HealthStatus.HEALTHY,
    )

    assert updated.trace_ref == "trace-original"
    assert updated.health.detail == ""


def test_rollback_requires_maintainer_role(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="cannot perform a rollback"):
        deployment_service.rollback(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            deployment_id=record.id,
            target_version_id=version.id,
            deployed_by="user-2",
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_rollback_raises_for_missing_deployment(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            deployment_id="missing",
            target_version_id="v1",
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_raises_when_deployment_belongs_to_different_agent(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id="demo",
            logical_agent_id="agent-different",
            deployment_id=record.id,
            target_version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_raises_when_target_version_never_deployed(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="no prior deployment history"):
        deployment_service.rollback(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            deployment_id=record.id,
            target_version_id="version-never-deployed",
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_succeeds_to_previously_deployed_version(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    first_version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-rollback-success",
    )
    first_deploy = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-rollback-success",
        version_id=first_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-rollback-success",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, second_version)
    second_deploy = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-rollback-success",
        version_id=second_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    rolled_back = deployment_service.rollback(
        tenant_id="demo",
        logical_agent_id="agent-rollback-success",
        deployment_id=second_deploy.id,
        target_version_id=first_version.id,
        deployed_by="maintainer-1",
        actor_role=AgentRole.MAINTAINER,
    )

    assert rolled_back.version_id == first_version.id
    assert rolled_back.rollback_of_deployment_id == second_deploy.id
    assert rolled_back.runtime_target == first_version.runtime_target
    binding = store.get_binding("demo", "agent-rollback-success", DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == first_version.id
    assert first_deploy.id != second_deploy.id


def test_rollback_uses_failing_runtime_when_target_version_runtime_missing(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-rollback-fallback")
    first_version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, first_version)
    first_deploy = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        version_id=first_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    draft = store.get_draft("demo", "agent-rollback-fallback")
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        manifest=draft.manifest.model_copy(
            update={"runtime_requirements": RuntimeRequirements(requires_custom_code=True)}
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, second_version)
    second_deploy = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        version_id=second_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert first_deploy.runtime_target is RuntimeTarget.MANAGED_FOUNDRY
    assert second_deploy.runtime_target is RuntimeTarget.CUSTOM_HOSTED
    store._versions[first_version.id] = first_version.model_copy(update={"runtime_target": None})

    rolled_back = deployment_service.rollback(
        tenant_id="demo",
        logical_agent_id="agent-rollback-fallback",
        deployment_id=second_deploy.id,
        target_version_id=first_version.id,
        deployed_by="maintainer-1",
        actor_role=AgentRole.MAINTAINER,
    )

    assert rolled_back.version_id == first_version.id
    assert rolled_back.runtime_target is RuntimeTarget.CUSTOM_HOSTED


def test_rollback_raises_when_target_version_missing_after_history_check(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    ghost_deploy = record.model_copy(update={"id": "ghost-deploy", "version_id": "ghost-version"})
    store.create_deployment(ghost_deploy)

    with pytest.raises(DeploymentServiceError, match=r"Version 'ghost-version' not found\."):
        deployment_service.rollback(
            tenant_id="demo",
            logical_agent_id="agent-deploy-test",
            deployment_id=record.id,
            target_version_id="ghost-version",
            deployed_by="maintainer-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_resolve_returns_full_contract_for_bound_version(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-resolve-contract")
    draft = store.get_draft("demo", "agent-resolve-contract")
    assert draft is not None
    input_schema = SchemaRef(ref="schema://input", digest="sha256:input")
    output_schema = SchemaRef(ref="schema://output", digest="sha256:output")
    release_service.update_draft(
        tenant_id="demo",
        logical_agent_id="agent-resolve-contract",
        manifest=draft.manifest.model_copy(
            update={
                "capabilities": (
                    CapabilityBinding(
                        descriptor_id="foundry.web_search",
                        operation="search",
                        attached_by="user-1",
                    ),
                ),
                "input_schema_ref": input_schema,
                "output_schema_ref": output_schema,
            }
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-resolve-contract",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, version)
    _append_release(store, version, ReleaseStatus.APPROVED)
    active_release = _append_release(store, version, ReleaseStatus.ACTIVE)
    deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-resolve-contract",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    resolved = deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-resolve-contract")

    assert resolved is not None
    assert resolved.logical_agent_id == "agent-resolve-contract"
    assert resolved.tenant_id == "demo"
    assert resolved.environment is DeploymentEnvironment.DEVELOPMENT
    assert resolved.version_id == version.id
    assert resolved.release_id == active_release.id
    assert resolved.release_status is ReleaseStatus.ACTIVE
    assert resolved.manifest_hash == version.manifest_hash
    assert resolved.runtime_target == version.runtime_target
    assert resolved.capability_versions == {"foundry.web_search": "1"}
    assert resolved.input_schema_ref == input_schema
    assert resolved.output_schema_ref == output_schema
    assert resolved.package_version == version.package_version
    assert resolved.protocol_version == version.protocol_version


def test_resolve_returns_none_when_no_binding(deployment_service: DeploymentService) -> None:
    assert deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-deploy-test") is None


def test_resolve_returns_none_when_binding_points_to_missing_version(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-resolve-missing-version")
    store.set_binding(
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-missing-version",
            tenant_id="demo",
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id="missing-version",
            updated_by="user-1",
        )
    )

    assert (
        deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-resolve-missing-version") is None
    )


def test_resolve_returns_none_when_bound_version_has_no_release(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version = _cut_version(release_service, logical_agent_id="agent-resolve-no-release")
    store.set_binding(
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-no-release",
            tenant_id="demo",
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=version.id,
            updated_by="user-1",
        )
    )

    assert deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-resolve-no-release") is None


def test_resolve_returns_none_when_bound_version_has_no_runtime_target(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-resolve-no-runtime",
    )
    store._versions[version.id] = version.model_copy(update={"runtime_target": None})
    store.set_binding(
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-no-runtime",
            tenant_id="demo",
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=version.id,
            updated_by="user-1",
        )
    )

    assert deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-resolve-no-runtime") is None


def test_contract_for_version_returns_full_contract_without_binding(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, release = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.APPROVED,
        logical_agent_id="agent-contract-for-version",
    )

    contract = deployment_service.contract_for_version(tenant_id="demo", version_id=version.id)

    assert contract is not None
    assert contract.version_id == version.id
    assert contract.release_id == release.id
    assert contract.release_status is ReleaseStatus.APPROVED
    assert contract.environment is DeploymentEnvironment.DEVELOPMENT


def test_contract_for_version_returns_none_when_version_missing(
    deployment_service: DeploymentService,
) -> None:
    assert deployment_service.contract_for_version(tenant_id="demo", version_id="missing") is None


def test_contract_for_version_returns_none_when_version_has_no_release(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
) -> None:
    version = _cut_version(release_service, logical_agent_id="agent-contract-no-release")

    assert deployment_service.contract_for_version(tenant_id="demo", version_id=version.id) is None


def test_contract_for_version_returns_none_when_version_has_no_runtime_target(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-contract-no-runtime",
    )
    store._versions[version.id] = version.model_copy(update={"runtime_target": None})

    assert deployment_service.contract_for_version(tenant_id="demo", version_id=version.id) is None


def test_catalog_returns_only_resolvable_bound_agents(
    release_service: ReleaseService,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    bound_version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-catalog-bound",
    )
    deployment_service.deploy(
        tenant_id="demo",
        logical_agent_id="agent-catalog-bound",
        version_id=bound_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    _create_agent(release_service, logical_agent_id="agent-catalog-unbound")

    unresolved_version = _cut_version(release_service, logical_agent_id="agent-catalog-no-release")
    store.set_binding(
        LogicalAgentBinding(
            logical_agent_id="agent-catalog-no-release",
            tenant_id="demo",
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=unresolved_version.id,
            updated_by="user-1",
        )
    )

    contracts = deployment_service.catalog(tenant_id="demo")

    assert [contract.logical_agent_id for contract in contracts] == ["agent-catalog-bound"]
    assert contracts[0].version_id == bound_version.id


def test_runtime_target_migration_requires_cutting_a_new_version(
    release_service: ReleaseService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-runtime-migration")
    first_version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-runtime-migration",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    assert first_version.runtime_target is RuntimeTarget.MANAGED_FOUNDRY

    draft = store.get_draft("demo", "agent-runtime-migration")
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        logical_agent_id="agent-runtime-migration",
        manifest=draft.manifest.model_copy(
            update={"runtime_requirements": RuntimeRequirements(requires_custom_code=True)}
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        logical_agent_id="agent-runtime-migration",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert second_version.id != first_version.id
    assert second_version.sequence == first_version.sequence + 1
    assert first_version.runtime_target is RuntimeTarget.MANAGED_FOUNDRY
    assert store.get_version("demo", first_version.id) == first_version
    assert second_version.runtime_target is RuntimeTarget.CUSTOM_HOSTED
