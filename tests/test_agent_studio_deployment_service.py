from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_registry import default_registry
from research_assistant_api.agent_studio.deployment_service import DeploymentService, DeploymentServiceError
from research_assistant_api.agent_studio.models import (
    AgentOwnerKind,
    AgentRole,
    AgentVersion,
    DeploymentEnvironment,
    HealthStatus,
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


def _gated_version(
    release_service: ReleaseService, logical_agent_id: str = "agent-deploy-test", owner: str = "user-1"
) -> AgentVersion:
    release_service.create_agent(
        tenant_id="demo", logical_agent_id=logical_agent_id, display_name="Deploy Test Agent",
        owner_kind=AgentOwnerKind.USER, owner_id=owner, requested_by=owner, is_platform_owner=False,
    )
    version = release_service.cut_version(
        tenant_id="demo", logical_agent_id=logical_agent_id, actor_id=owner, actor_role=AgentRole.OWNER
    )
    release_service.run_release_gates(
        tenant_id="demo", version_id=version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    return version


def test_deploy_requires_contributor_role(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    with pytest.raises(DeploymentServiceError, match="cannot create deployments"):
        deployment_service.deploy(
            tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
            deployed_by="user-2", actor_role=AgentRole.VIEWER,
        )


def test_deploy_raises_for_unknown_version(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.deploy(
            tenant_id="demo", logical_agent_id="agent-deploy-test", version_id="missing",
            deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_version_belongs_to_different_agent(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.deploy(
            tenant_id="demo", logical_agent_id="agent-other", version_id=version.id,
            deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_status_not_deployable(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    release_service.create_agent(
        tenant_id="demo", logical_agent_id="agent-deploy-test", display_name="Deploy Test Agent",
        owner_kind=AgentOwnerKind.USER, owner_id="user-1", requested_by="user-1", is_platform_owner=False,
    )
    version = release_service.cut_version(
        tenant_id="demo", logical_agent_id="agent-deploy-test", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    with pytest.raises(DeploymentServiceError, match="must pass all hard gates"):
        deployment_service.deploy(
            tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
            deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_deploy_succeeds_and_sets_development_binding(
    release_service: ReleaseService, deployment_service: DeploymentService, store: AgentStudioStore
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER, trace_ref="trace-abc",
    )
    assert record.version_id == version.id
    assert record.trace_ref == "trace-abc"
    binding = store.get_binding("demo", "agent-deploy-test", DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == version.id


def test_deploy_raises_when_runtime_target_not_resolved(
    release_service: ReleaseService, deployment_service: DeploymentService, store: AgentStudioStore
) -> None:
    # Defensive branch: a version somehow reached a deployable status without
    # ever having its runtime_target resolved (e.g. corrupted/partial record).
    version = _gated_version(release_service)
    gated = store.get_version("demo", version.id)
    assert gated is not None
    unresolved = gated.model_copy(update={"runtime_target": None})
    store._versions[unresolved.id] = unresolved
    with pytest.raises(DeploymentServiceError, match="no runtime_target resolved"):
        deployment_service.deploy(
            tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=unresolved.id,
            deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_record_health_raises_for_missing_deployment(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.record_health(tenant_id="demo", deployment_id="missing", status=HealthStatus.HEALTHY)


def test_record_health_updates_deployment_health_and_trace(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    updated = deployment_service.record_health(
        tenant_id="demo", deployment_id=record.id, status=HealthStatus.DEGRADED,
        detail="latency spike", trace_ref="trace-xyz",
    )
    assert updated.health.status == HealthStatus.DEGRADED
    assert updated.health.detail == "latency spike"
    assert updated.trace_ref == "trace-xyz"


def test_record_health_without_trace_ref_leaves_existing_trace(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER, trace_ref="trace-original",
    )
    updated = deployment_service.record_health(
        tenant_id="demo", deployment_id=record.id, status=HealthStatus.HEALTHY
    )
    assert updated.trace_ref == "trace-original"


def test_rollback_requires_maintainer_role(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    with pytest.raises(DeploymentServiceError, match="cannot perform a rollback"):
        deployment_service.rollback(
            tenant_id="demo", logical_agent_id="agent-deploy-test", deployment_id=record.id,
            target_version_id=version.id, deployed_by="user-2", actor_role=AgentRole.CONTRIBUTOR,
        )


def test_rollback_raises_for_missing_deployment(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id="demo", logical_agent_id="agent-deploy-test", deployment_id="missing",
            target_version_id="v1", deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_rollback_raises_when_deployment_belongs_to_different_agent(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id="demo", logical_agent_id="agent-different", deployment_id=record.id,
            target_version_id=version.id, deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_rollback_raises_when_target_version_never_deployed(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    with pytest.raises(DeploymentServiceError, match="no prior deployment history"):
        deployment_service.rollback(
            tenant_id="demo", logical_agent_id="agent-deploy-test", deployment_id=record.id,
            target_version_id="version-never-deployed", deployed_by="user-1", actor_role=AgentRole.OWNER,
        )


def test_rollback_succeeds_to_previously_deployed_version(
    release_service: ReleaseService, deployment_service: DeploymentService, store: AgentStudioStore
) -> None:
    first_version = _gated_version(release_service)
    first_deploy = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=first_version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo", logical_agent_id="agent-deploy-test", actor_id="user-1", actor_role=AgentRole.OWNER
    )
    release_service.run_release_gates(
        tenant_id="demo", version_id=second_version.id,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    second_deploy = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=second_version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )

    rolled_back = deployment_service.rollback(
        tenant_id="demo", logical_agent_id="agent-deploy-test", deployment_id=second_deploy.id,
        target_version_id=first_version.id, deployed_by="maintainer-1", actor_role=AgentRole.MAINTAINER,
    )
    assert rolled_back.version_id == first_version.id
    assert rolled_back.rollback_of_deployment_id == second_deploy.id
    binding = store.get_binding("demo", "agent-deploy-test", DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == first_version.id
    assert first_deploy.id != second_deploy.id


def test_rollback_raises_when_target_version_missing_after_history_check(
    release_service: ReleaseService, deployment_service: DeploymentService, store: AgentStudioStore
) -> None:
    version = _gated_version(release_service)
    record = deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    # Simulate a deployment history entry pointing at a version_id that no
    # longer resolves (defensive branch: history says it was deployed, but
    # the version record itself is gone).
    from research_assistant_api.agent_studio.models import DeploymentRecord, RuntimeTarget

    ghost_deploy = DeploymentRecord(
        id="ghost-deploy",
        logical_agent_id="agent-deploy-test",
        tenant_id="demo",
        version_id="ghost-version",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by="user-1",
    )
    store.create_deployment(ghost_deploy)
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.rollback(
            tenant_id="demo", logical_agent_id="agent-deploy-test", deployment_id=record.id,
            target_version_id="ghost-version", deployed_by="maintainer-1", actor_role=AgentRole.MAINTAINER,
        )


def test_resolve_returns_none_when_no_binding(deployment_service: DeploymentService) -> None:
    assert deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-deploy-test") is None


def test_resolve_returns_bound_version(
    release_service: ReleaseService, deployment_service: DeploymentService
) -> None:
    version = _gated_version(release_service)
    deployment_service.deploy(
        tenant_id="demo", logical_agent_id="agent-deploy-test", version_id=version.id,
        deployed_by="user-1", actor_role=AgentRole.OWNER,
    )
    resolved = deployment_service.resolve(tenant_id="demo", logical_agent_id="agent-deploy-test")
    assert resolved is not None
    assert resolved.id == version.id
