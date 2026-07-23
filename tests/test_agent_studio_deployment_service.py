# mypy: disable-error-code="import-untyped,attr-defined"
from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest

if "research_assistant_api.agent_studio.cosmos_store" not in sys.modules:
    cosmos_store_stub = types.ModuleType("research_assistant_api.agent_studio.cosmos_store")
    cosmos_store_stub.build_agent_studio_store = lambda *args, **kwargs: None
    sys.modules["research_assistant_api.agent_studio.cosmos_store"] = cosmos_store_stub

from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry, default_registry
from research_assistant_api.agent_studio.deployment_service import DeploymentService, DeploymentServiceError
from research_assistant_api.agent_studio.model_discovery import InMemoryModelDiscovery, UnavailableModelDiscovery
from research_assistant_api.agent_studio.models import (
    AGENT_STUDIO_PROTOCOL_VERSION,
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    CapabilityBinding,
    CapabilityDescriptorRef,
    CapabilityOperationRef,
    DeploymentEnvironment,
    HealthStatus,
    LogicalAgentBinding,
    ModelDeploymentRef,
    ReleaseStatus,
    RuntimeRequirements,
    RuntimeTarget,
    SchemaRef,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence
from research_assistant_api.agent_studio.release_service import manifest_hash
from research_assistant_api.agent_studio.runtime_selection import select_runtime
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

TENANT = "demo"
TEST_PROJECT_ID = "proj-1"
OTHER_PROJECT_ID = "proj-2"


def _scope(project_id: str = TEST_PROJECT_ID, tenant_id: str = TENANT) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, project_id=project_id)


class ReleaseServiceHarness:
    def __init__(
        self,
        store: AgentStudioStore,
        registry: CapabilityRegistry | None = None,
        *,
        model_discovery: InMemoryModelDiscovery | UnavailableModelDiscovery | None = None,
    ) -> None:
        self._store = store
        self._registry = registry or default_registry()
        self._model_discovery = model_discovery or UnavailableModelDiscovery()

    def create_agent(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        display_name: str,
        owner_kind: AgentOwnerKind,
        owner_id: str,
        requested_by: str,
        is_platform_owner: bool,
    ) -> AgentDraft:
        del is_platform_owner
        manifest = AgentManifest(
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            display_name=display_name,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )
        draft = AgentDraft(
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            manifest=manifest,
            updated_by=requested_by,
        )
        return self._store.save_draft(_scope(project_id, tenant_id), draft)

    def update_draft(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        manifest: AgentManifest,
        updated_by: str,
        actor_role: AgentRole,
    ) -> AgentDraft:
        del actor_role
        scope = _scope(project_id, tenant_id)
        existing = self._store.get_draft(scope, logical_agent_id)
        assert existing is not None
        updated = existing.model_copy(update={"manifest": manifest, "updated_by": updated_by})
        return self._store.save_draft(scope, updated)

    def _revalidate_model_deployment(self, manifest: AgentManifest) -> None:
        declared = manifest.model_deployment
        if declared is None:
            return
        live = self._model_discovery.list_deployed_models()
        assert any(
            ref.deployment_name == declared.deployment_name and ref.model_name == declared.model_name for ref in live
        )

    def cut_version(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        actor_id: str,
        actor_role: AgentRole,
    ) -> AgentVersion:
        del actor_role
        scope = _scope(project_id, tenant_id)
        draft = self._store.get_draft(scope, logical_agent_id)
        assert draft is not None
        self._revalidate_model_deployment(draft.manifest)
        previous_versions = self._store.list_versions(scope, logical_agent_id)
        parent_version_id = previous_versions[-1].id if previous_versions else None
        selection = select_runtime(draft.manifest, self._registry.as_mapping())
        capability_versions = {
            binding.descriptor_ref.id: binding.descriptor_ref.version for binding in draft.manifest.capabilities
        }
        return self._store.allocate_version(
            scope,
            logical_agent_id,
            lambda sequence: AgentVersion(
                id=f"{logical_agent_id}-v{sequence}",
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                project_id=project_id,
                sequence=sequence,
                manifest=draft.manifest,
                manifest_hash=manifest_hash(draft.manifest),
                created_by=actor_id,
                parent_version_id=parent_version_id,
                runtime_target=selection.target,
                runtime_selection_reasons=selection.reasons,
                model_deployment=draft.manifest.model_deployment,
                capability_versions=capability_versions,
                protocol_version=AGENT_STUDIO_PROTOCOL_VERSION,
            ),
        )

    def run_release_gates(
        self,
        *,
        tenant_id: str,
        project_id: str,
        version_id: str,
        evidence: GateEvidence,
    ) -> AgentRelease:
        del evidence
        scope = _scope(project_id, tenant_id)
        version = self._store.get_version(scope, version_id)
        assert version is not None
        previous = self._store.latest_release_for_version(scope, version_id)
        release = AgentRelease(
            id=f"{version.id}-gated",
            version_id=version_id,
            logical_agent_id=version.logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            manifest_hash=version.manifest_hash,
            status=ReleaseStatus.GATED,
            previous_release_id=previous.id if previous is not None else None,
            created_by=version.created_by,
            detail="Passed all applicable hard gates.",
        )
        return self._store.create_release(scope, release)


@pytest.fixture
def store() -> AgentStudioStore:
    return AgentStudioStore()


@pytest.fixture
def release_service(store: AgentStudioStore) -> ReleaseServiceHarness:
    return ReleaseServiceHarness(store)


@pytest.fixture
def deployment_service(store: AgentStudioStore) -> DeploymentService:
    return DeploymentService(store)


def _create_agent(
    release_service: ReleaseServiceHarness,
    *,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
    project_id: str = TEST_PROJECT_ID,
) -> None:
    release_service.create_agent(
        tenant_id=TENANT,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        display_name="Deploy Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id=owner,
        requested_by=owner,
        is_platform_owner=False,
    )


def _cut_version(
    release_service: ReleaseServiceHarness,
    *,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
    project_id: str = TEST_PROJECT_ID,
) -> AgentVersion:
    _create_agent(release_service, logical_agent_id=logical_agent_id, owner=owner, project_id=project_id)
    return release_service.cut_version(
        tenant_id=TENANT,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        actor_id=owner,
        actor_role=AgentRole.OWNER,
    )


def _pass_gates(
    release_service: ReleaseServiceHarness, version: AgentVersion, *, project_id: str = TEST_PROJECT_ID
) -> None:
    release_service.run_release_gates(
        tenant_id=TENANT,
        project_id=project_id,
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
    scope = _scope(version.project_id, version.tenant_id)
    previous = store.latest_release_for_version(scope, version.id)
    release = AgentRelease(
        id=f"{version.id}-{status.value}-{len(store.list_releases_for_version(scope, version.id)) + 1}",
        version_id=version.id,
        logical_agent_id=version.logical_agent_id,
        tenant_id=version.tenant_id,
        project_id=version.project_id,
        manifest_hash=version.manifest_hash,
        status=status,
        previous_release_id=previous.id if previous is not None else None,
        created_by=created_by,
        detail=f"Transitioned to {status.value}.",
    )
    return store.create_release(scope, release)


def _version_with_latest_release(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
    *,
    latest_status: ReleaseStatus,
    logical_agent_id: str = "agent-deploy-test",
    owner: str = "user-1",
    project_id: str = TEST_PROJECT_ID,
) -> tuple[AgentVersion, AgentRelease]:
    version = _cut_version(release_service, logical_agent_id=logical_agent_id, owner=owner, project_id=project_id)
    _pass_gates(release_service, version, project_id=project_id)
    release = store.latest_release_for_version(_scope(version.project_id, version.tenant_id), version.id)
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
    release_service: ReleaseServiceHarness,
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
        project_id=TEST_PROJECT_ID,
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
    binding = store.get_binding(_scope(), logical_agent_id, DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == version.id

    resolved = deployment_service.resolve(
        tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id=logical_agent_id
    )
    assert resolved is not None
    assert resolved.version_id == version.id
    assert resolved.release_id == release.id
    assert resolved.release_status is latest_status


def test_deploy_and_record_health_are_cross_project_isolated(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-cross-project-deploy",
    )
    record = deployment_service.deploy(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-deploy",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert store.get_deployment(_scope(OTHER_PROJECT_ID), record.id) is None

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.deploy(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            logical_agent_id="agent-cross-project-deploy",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )

    with pytest.raises(DeploymentServiceError, match=r"Deployment .* not found"):
        deployment_service.record_health(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            deployment_id=record.id,
            status=HealthStatus.HEALTHY,
        )


def test_deploy_requires_contributor_role(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)

    with pytest.raises(DeploymentServiceError, match="cannot create deployments"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-2",
            actor_role=AgentRole.VIEWER,
        )


def test_deploy_raises_for_unknown_version(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            version_id="missing",
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_version_belongs_to_different_agent(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-other",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_version_has_no_release_record(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
) -> None:
    version = _cut_version(release_service)

    with pytest.raises(DeploymentServiceError, match="release status 'none'"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


@pytest.mark.parametrize("latest_status", [ReleaseStatus.DEPRECATED, ReleaseStatus.ROLLED_BACK])
def test_deploy_rejects_non_deployable_release_statuses(
    latest_status: ReleaseStatus,
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=latest_status)

    with pytest.raises(DeploymentServiceError, match=rf"release status '{latest_status.value}'"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_runtime_target_not_resolved(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    store._versions[version.id] = version.model_copy(update={"runtime_target": None})

    with pytest.raises(DeploymentServiceError, match="no runtime_target resolved"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_record_health_raises_for_missing_deployment(deployment_service: DeploymentService) -> None:
    with pytest.raises(DeploymentServiceError, match="not found"):
        deployment_service.record_health(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            deployment_id="missing",
            status=HealthStatus.HEALTHY,
        )


def test_record_health_updates_deployment_health_and_trace(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    updated = deployment_service.record_health(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        deployment_id=record.id,
        status=HealthStatus.DEGRADED,
        detail="latency spike",
        trace_ref="trace-xyz",
    )

    assert updated.health.status is HealthStatus.DEGRADED
    assert updated.health.detail == "latency spike"
    assert updated.trace_ref == "trace-xyz"
    persisted = store.get_deployment(_scope(), record.id)
    assert persisted is not None
    assert persisted.health.status is HealthStatus.DEGRADED


def test_record_health_without_trace_ref_leaves_existing_trace(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
        trace_ref="trace-original",
    )

    updated = deployment_service.record_health(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        deployment_id=record.id,
        status=HealthStatus.HEALTHY,
    )

    assert updated.trace_ref == "trace-original"
    assert updated.health.detail == ""


def test_rollback_requires_maintainer_role(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="cannot perform a rollback"):
        deployment_service.rollback(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
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
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            deployment_id="missing",
            target_version_id="v1",
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_raises_when_deployment_belongs_to_different_agent(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-different",
            deployment_id=record.id,
            target_version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_raises_when_target_version_never_deployed(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="no prior deployment history"):
        deployment_service.rollback(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            deployment_id=record.id,
            target_version_id="version-never-deployed",
            deployed_by="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_succeeds_to_previously_deployed_version(
    release_service: ReleaseServiceHarness,
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
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-success",
        version_id=first_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-success",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, second_version)
    second_deploy = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-success",
        version_id=second_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    rolled_back = deployment_service.rollback(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-success",
        deployment_id=second_deploy.id,
        target_version_id=first_version.id,
        deployed_by="maintainer-1",
        actor_role=AgentRole.MAINTAINER,
    )

    assert rolled_back.version_id == first_version.id
    assert rolled_back.rollback_of_deployment_id == second_deploy.id
    assert rolled_back.runtime_target == first_version.runtime_target
    binding = store.get_binding(_scope(), "agent-rollback-success", DeploymentEnvironment.DEVELOPMENT)
    assert binding is not None
    assert binding.resolved_version_id == first_version.id
    assert first_deploy.id != second_deploy.id


def test_rollback_uses_failing_runtime_when_target_version_runtime_missing(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-rollback-fallback")
    first_version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-fallback",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, first_version)
    first_deploy = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-fallback",
        version_id=first_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    draft = store.get_draft(_scope(), "agent-rollback-fallback")
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-fallback",
        manifest=draft.manifest.model_copy(
            update={"runtime_requirements": RuntimeRequirements(requires_custom_code=True)}
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-fallback",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, second_version)
    second_deploy = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
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
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-rollback-fallback",
        deployment_id=second_deploy.id,
        target_version_id=first_version.id,
        deployed_by="maintainer-1",
        actor_role=AgentRole.MAINTAINER,
    )

    assert rolled_back.version_id == first_version.id
    assert rolled_back.runtime_target is RuntimeTarget.CUSTOM_HOSTED


def test_rollback_raises_when_target_version_missing_after_history_check(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(release_service, store, latest_status=ReleaseStatus.GATED)
    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-test",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    ghost_deploy = record.model_copy(update={"id": "ghost-deploy", "version_id": "ghost-version"})
    store.create_deployment(_scope(), ghost_deploy)

    with pytest.raises(DeploymentServiceError, match=r"Version 'ghost-version' not found\."):
        deployment_service.rollback(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-test",
            deployment_id=record.id,
            target_version_id="ghost-version",
            deployed_by="maintainer-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_rollback_is_cross_project_isolated(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    first_version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-cross-project-rollback",
    )
    deployment_service.deploy(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-rollback",
        version_id=first_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-rollback",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, second_version)
    second_deploy = deployment_service.deploy(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-rollback",
        version_id=second_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(DeploymentServiceError, match="not found for agent"):
        deployment_service.rollback(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            logical_agent_id="agent-cross-project-rollback",
            deployment_id=second_deploy.id,
            target_version_id=first_version.id,
            deployed_by="maintainer-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_resolve_returns_full_contract_for_bound_version(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-resolve-contract")
    draft = store.get_draft(_scope(), "agent-resolve-contract")
    assert draft is not None
    input_schema = SchemaRef(ref="schema://input", digest="sha256:input")
    output_schema = SchemaRef(ref="schema://output", digest="sha256:output")
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-resolve-contract",
        manifest=draft.manifest.model_copy(
            update={
                "capabilities": (
                    CapabilityBinding(
                        provider_contract_version="agent-studio.capability-registry.v1",
                        descriptor_ref=CapabilityDescriptorRef(id="foundry.web_search"),
                        operation_ref=CapabilityOperationRef(id="search"),
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
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-resolve-contract",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _pass_gates(release_service, version)
    _append_release(store, version, ReleaseStatus.APPROVED)
    active_release = _append_release(store, version, ReleaseStatus.ACTIVE)
    deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-resolve-contract",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    resolved = deployment_service.resolve(
        tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id="agent-resolve-contract"
    )

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
    assert resolved.artifact_metadata == version.artifact_metadata
    assert resolved.protocol_version == version.protocol_version


def test_resolve_returns_none_when_no_binding(deployment_service: DeploymentService) -> None:
    assert (
        deployment_service.resolve(
            tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id="agent-deploy-test"
        )
        is None
    )


def test_resolve_returns_none_when_binding_points_to_missing_version(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-resolve-missing-version")
    store.set_binding(
        _scope(),
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-missing-version",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id="missing-version",
            updated_by="user-1",
        )
    )

    assert (
        deployment_service.resolve(
            tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id="agent-resolve-missing-version"
        )
        is None
    )


def test_resolve_returns_none_when_bound_version_has_no_release(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version = _cut_version(release_service, logical_agent_id="agent-resolve-no-release")
    store.set_binding(
        _scope(),
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-no-release",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=version.id,
            updated_by="user-1",
        )
    )

    assert (
        deployment_service.resolve(
            tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id="agent-resolve-no-release"
        )
        is None
    )


def test_resolve_returns_none_when_bound_version_has_no_runtime_target(
    release_service: ReleaseServiceHarness,
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
        _scope(),
        LogicalAgentBinding(
            logical_agent_id="agent-resolve-no-runtime",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=version.id,
            updated_by="user-1",
        )
    )

    assert (
        deployment_service.resolve(
            tenant_id="demo", project_id=TEST_PROJECT_ID, logical_agent_id="agent-resolve-no-runtime"
        )
        is None
    )


def test_resolve_is_cross_project_isolated(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-cross-project-resolve",
    )
    deployment_service.deploy(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-resolve",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert (
        deployment_service.resolve(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            logical_agent_id="agent-cross-project-resolve",
        )
        is None
    )


def test_contract_for_version_returns_full_contract_without_binding(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, release = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.APPROVED,
        logical_agent_id="agent-contract-for-version",
    )

    contract = deployment_service.contract_for_version(
        tenant_id="demo", project_id=TEST_PROJECT_ID, version_id=version.id
    )

    assert contract is not None
    assert contract.version_id == version.id
    assert contract.release_id == release.id
    assert contract.release_status is ReleaseStatus.APPROVED
    assert contract.environment is DeploymentEnvironment.DEVELOPMENT


def test_contract_for_version_returns_none_when_version_missing(
    deployment_service: DeploymentService,
) -> None:
    assert (
        deployment_service.contract_for_version(tenant_id="demo", project_id=TEST_PROJECT_ID, version_id="missing")
        is None
    )


def test_contract_for_version_returns_none_when_version_has_no_release(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
) -> None:
    version = _cut_version(release_service, logical_agent_id="agent-contract-no-release")

    assert (
        deployment_service.contract_for_version(
            tenant_id="demo", project_id=TEST_PROJECT_ID, version_id=version.id
        )
        is None
    )


def test_contract_for_version_returns_none_when_version_has_no_runtime_target(
    release_service: ReleaseServiceHarness,
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

    assert (
        deployment_service.contract_for_version(
            tenant_id="demo", project_id=TEST_PROJECT_ID, version_id=version.id
        )
        is None
    )


def test_contract_for_version_is_cross_project_isolated(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.APPROVED,
        logical_agent_id="agent-cross-project-contract",
    )

    assert (
        deployment_service.contract_for_version(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            version_id=version.id,
        )
        is None
    )


def test_catalog_returns_only_resolvable_bound_agents(
    release_service: ReleaseServiceHarness,
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
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-catalog-bound",
        version_id=bound_version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    _create_agent(release_service, logical_agent_id="agent-catalog-unbound")

    unresolved_version = _cut_version(release_service, logical_agent_id="agent-catalog-no-release")
    store.set_binding(
        _scope(),
        LogicalAgentBinding(
            logical_agent_id="agent-catalog-no-release",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            environment=DeploymentEnvironment.DEVELOPMENT,
            resolved_version_id=unresolved_version.id,
            updated_by="user-1",
        )
    )

    contracts = deployment_service.catalog(tenant_id="demo", project_id=TEST_PROJECT_ID)

    assert [contract.logical_agent_id for contract in contracts] == ["agent-catalog-bound"]
    assert contracts[0].version_id == bound_version.id


def test_catalog_is_cross_project_isolated(
    release_service: ReleaseServiceHarness,
    deployment_service: DeploymentService,
    store: AgentStudioStore,
) -> None:
    version, _ = _version_with_latest_release(
        release_service,
        store,
        latest_status=ReleaseStatus.GATED,
        logical_agent_id="agent-cross-project-catalog",
    )
    deployment_service.deploy(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-catalog",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    _create_agent(release_service, logical_agent_id="agent-other-project-only", project_id=OTHER_PROJECT_ID)

    assert deployment_service.catalog(tenant_id=TENANT, project_id=OTHER_PROJECT_ID) == ()


def test_runtime_target_migration_requires_cutting_a_new_version(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-runtime-migration")
    first_version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-runtime-migration",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    assert first_version.runtime_target is RuntimeTarget.MANAGED_FOUNDRY

    draft = store.get_draft(_scope(), "agent-runtime-migration")
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-runtime-migration",
        manifest=draft.manifest.model_copy(
            update={"runtime_requirements": RuntimeRequirements(requires_custom_code=True)}
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    second_version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-runtime-migration",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert second_version.id != first_version.id
    assert second_version.sequence == first_version.sequence + 1
    assert first_version.runtime_target is RuntimeTarget.MANAGED_FOUNDRY
    assert store.get_version(_scope(), first_version.id) == first_version
    assert second_version.runtime_target is RuntimeTarget.CUSTOM_HOSTED


# -- Deploy-time revalidation: capability-operation approvals ---------------
#
# ``DeploymentService`` independently re-checks approval-gated capability
# bindings at deploy time (not just at cut/gate time), since an approval can
# be revoked/expired or the manifest content can otherwise diverge between
# cut and deploy. These tests bypass ``run_release_gates`` (via
# ``_append_release`` directly) so each scenario can freely control whether
# a matching approval exists in the store, independent of the cut-time gate.


def _capability_gated_version(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
    *,
    logical_agent_id: str,
    owner: str = "user-1",
) -> AgentVersion:
    _create_agent(release_service, logical_agent_id=logical_agent_id, owner=owner)
    binding = release_service._registry.attach(
        descriptor_id="foundry.azure_functions",
        operation="invoke",
        attached_by=owner,
        connection_ref="conn-azure-functions",
        policy_ref="policy.capability-approval.write-irreversible.v1",
    )
    draft = release_service._store.get_draft(_scope(), logical_agent_id)
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id=logical_agent_id,
        manifest=draft.manifest.model_copy(update={"capabilities": (binding,)}),
        updated_by=owner,
        actor_role=AgentRole.OWNER,
    )
    version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id=logical_agent_id,
        actor_id=owner,
        actor_role=AgentRole.OWNER,
    )
    _append_release(store, version, ReleaseStatus.GATED, created_by=owner)
    return version


def test_deploy_raises_when_capability_approval_is_missing(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    version = _capability_gated_version(release_service, store, logical_agent_id="agent-deploy-approval-missing")
    deployment_service = DeploymentService(store, capability_registry=default_registry())

    with pytest.raises(DeploymentServiceError, match="requires approval but no approved record was found"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-approval-missing",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_capability_approval_content_hash_mismatches(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    version = _capability_gated_version(release_service, store, logical_agent_id="agent-deploy-approval-stale-hash")
    store.create_approval(
        _scope(),
        StudioApprovalRecord(
            id="approval-stale-hash",
            version_id=version.id,
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            kind=ApprovalKind.CAPABILITY_OPERATION,
            state=ApprovalState.APPROVED,
            gated_action="attach_capability_operation",
            destination="foundry.azure_functions.invoke",
            requested_by="user-2",
            evidence_summary="Reviewed.",
            risk="medium",
            idempotency_key="idem-stale-hash",
            content_hash="sha256:not-this-version",
        )
    )
    deployment_service = DeploymentService(store, capability_registry=default_registry())

    with pytest.raises(DeploymentServiceError, match="bound to a different manifest content hash"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-approval-stale-hash",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_raises_when_capability_approval_has_expired(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    version = _capability_gated_version(release_service, store, logical_agent_id="agent-deploy-approval-expired")
    store.create_approval(
        _scope(),
        StudioApprovalRecord(
            id="approval-expired",
            version_id=version.id,
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            kind=ApprovalKind.CAPABILITY_OPERATION,
            state=ApprovalState.APPROVED,
            gated_action="attach_capability_operation",
            destination="foundry.azure_functions.invoke",
            requested_by="user-2",
            evidence_summary="Reviewed.",
            risk="medium",
            idempotency_key="idem-expired",
            content_hash=version.manifest_hash,
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
    )
    deployment_service = DeploymentService(store, capability_registry=default_registry())

    with pytest.raises(DeploymentServiceError, match="approval has expired"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-approval-expired",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_succeeds_when_capability_approval_is_valid(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    version = _capability_gated_version(release_service, store, logical_agent_id="agent-deploy-approval-valid")
    store.create_approval(
        _scope(),
        StudioApprovalRecord(
            id="approval-valid",
            version_id=version.id,
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            kind=ApprovalKind.CAPABILITY_OPERATION,
            state=ApprovalState.APPROVED,
            gated_action="attach_capability_operation",
            destination="foundry.azure_functions.invoke",
            requested_by="user-2",
            evidence_summary="Reviewed.",
            risk="medium",
            idempotency_key="idem-valid",
            content_hash=version.manifest_hash,
        )
    )
    deployment_service = DeploymentService(store, capability_registry=default_registry())

    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-approval-valid",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert record.version_id == version.id


def test_deploy_skips_capability_bindings_whose_descriptor_is_unknown_to_the_registry(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    """The registry passed to ``DeploymentService`` may not (yet) know about
    a descriptor referenced by an already-cut version's binding (e.g. a
    descriptor retired from the catalog after the version was cut); this
    must be skipped rather than treated as an approval failure."""
    version = _capability_gated_version(release_service, store, logical_agent_id="agent-deploy-unknown-descriptor")
    deployment_service = DeploymentService(store, capability_registry=CapabilityRegistry(descriptors=()))

    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-unknown-descriptor",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert record.version_id == version.id


def test_deploy_skips_capability_bindings_whose_operation_does_not_require_approval(
    release_service: ReleaseServiceHarness,
    store: AgentStudioStore,
) -> None:
    _create_agent(release_service, logical_agent_id="agent-deploy-no-approval-needed")
    binding = release_service._registry.attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
    )
    draft = store.get_draft(_scope(), "agent-deploy-no-approval-needed")
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-no-approval-needed",
        manifest=draft.manifest.model_copy(update={"capabilities": (binding,)}),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
    )
    version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-no-approval-needed",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _append_release(store, version, ReleaseStatus.GATED, created_by="user-1")
    deployment_service = DeploymentService(store, capability_registry=default_registry())

    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-no-approval-needed",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert record.version_id == version.id


# -- Deploy-time revalidation: model deployment ------------------------------
#
# Like the capability-approval recheck above, ``DeploymentService`` also
# revalidates a declared ``model_deployment`` against *live* discovery again
# at deploy time, independent of whatever discovery was wired into the
# ``ReleaseService`` that cut the version — a deployment name can go stale
# between cut and deploy.


def _model_deployment_gated_version(
    store: AgentStudioStore,
    *,
    logical_agent_id: str,
    discovery_models: tuple[ModelDeploymentRef, ...],
    owner: str = "user-1",
) -> AgentVersion:
    release_service = ReleaseServiceHarness(
        store,
        default_registry(),
        model_discovery=InMemoryModelDiscovery(discovery_models),
    )
    _create_agent(release_service, logical_agent_id=logical_agent_id, owner=owner)
    draft = store.get_draft(_scope(), logical_agent_id)
    assert draft is not None
    release_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id=logical_agent_id,
        manifest=draft.manifest.model_copy(
            update={
                "model_deployment": ModelDeploymentRef(
                    deployment_name="gpt-4o-mini",
                    model_name="gpt-4o-mini",
                    model_format="openai",
                )
            }
        ),
        updated_by=owner,
        actor_role=AgentRole.OWNER,
    )
    version = release_service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id=logical_agent_id,
        actor_id=owner,
        actor_role=AgentRole.OWNER,
    )
    _append_release(store, version, ReleaseStatus.GATED, created_by=owner)
    return version


def test_deploy_hard_fails_when_model_discovery_unavailable_at_deploy_time(store: AgentStudioStore) -> None:
    version = _model_deployment_gated_version(
        store,
        logical_agent_id="agent-deploy-model-unavailable",
        discovery_models=(
            ModelDeploymentRef(deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"),
        ),
    )
    deployment_service = DeploymentService(store, model_discovery=UnavailableModelDiscovery())

    with pytest.raises(DeploymentServiceError, match="Cannot revalidate model deployment"):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-model-unavailable",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_hard_fails_when_declared_deployment_not_live_at_deploy_time(store: AgentStudioStore) -> None:
    version = _model_deployment_gated_version(
        store,
        logical_agent_id="agent-deploy-model-stale",
        discovery_models=(
            ModelDeploymentRef(deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"),
        ),
    )
    deployment_service = DeploymentService(store, model_discovery=InMemoryModelDiscovery(()))

    with pytest.raises(
        DeploymentServiceError,
        match="is missing, stale, or unavailable; deployment smoke-check cannot proceed",
    ):
        deployment_service.deploy(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-deploy-model-stale",
            version_id=version.id,
            deployed_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_deploy_succeeds_when_declared_deployment_still_live_at_deploy_time(store: AgentStudioStore) -> None:
    matching = (ModelDeploymentRef(deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"),)
    version = _model_deployment_gated_version(
        store,
        logical_agent_id="agent-deploy-model-live",
        discovery_models=matching,
    )
    deployment_service = DeploymentService(store, model_discovery=InMemoryModelDiscovery(matching))

    record = deployment_service.deploy(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-deploy-model-live",
        version_id=version.id,
        deployed_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert record.version_id == version.id
