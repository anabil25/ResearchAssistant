from __future__ import annotations

# ruff: noqa: E402
import sys
import types
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "services" / "api" / "src" / "research_assistant_api"
if "research_assistant_api" not in sys.modules:
    package = types.ModuleType("research_assistant_api")
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["research_assistant_api"] = package

import research_assistant_api.agent_studio.release_service as release_service_module
from research_assistant_api.agent_studio.approvals import ApprovalError, idempotency_key
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityAttachmentError,
    CapabilityRegistry,
    seeded_test_registry,
)
from research_assistant_api.agent_studio.model_discovery import (
    InMemoryModelDiscovery,
    UnavailableModelDiscovery,
)
from research_assistant_api.agent_studio.models import (
    AGENT_STUDIO_PROTOCOL_VERSION,
    HARNESS_RELEASE_LINK_SCHEMA_VERSION,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    AgentVisibility,
    ApprovalKind,
    ApprovalState,
    CapabilityDescriptor,
    CapabilityInstance,
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentRecord,
    EvaluationRecord,
    GateName,
    GateResult,
    GateStatus,
    HealthStatus,
    InstanceReadiness,
    ModelDeploymentRef,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    RuntimeRequirements,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    DraftConflictError,
    ReleasePromotionConflictError,
    ReleaseService,
    ReleaseServiceError,
    manifest_hash,
    resolve_actor_role,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

TEST_PROJECT_ID = "proj-1"
OTHER_PROJECT_ID = "proj-2"


def _scope(tenant_id: str = "demo", project_id: str = TEST_PROJECT_ID) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, project_id=project_id)


class SpyAllocateStore(AgentStudioStore):
    def __init__(self) -> None:
        super().__init__()
        self.allocate_calls: list[tuple[str, str, str]] = []
        self.built_version: AgentVersion | None = None

    def next_sequence(self, scope: ScopeContext, logical_agent_id: str) -> int:  # pragma: no cover - defensive
        raise AssertionError("cut_version should use allocate_version(), not next_sequence().")

    def allocate_version(
        self,
        scope: ScopeContext,
        logical_agent_id: str,
        builder: Callable[[int], AgentVersion],
    ) -> AgentVersion:
        self.allocate_calls.append((scope.tenant_id, scope.project_id, logical_agent_id))
        version = builder(7)
        self.built_version = version
        return self.create_version(scope, version)


@pytest.fixture
def service() -> ReleaseService:
    return ReleaseService(
        AgentStudioStore(),
        seeded_test_registry(),
        model_discovery=InMemoryModelDiscovery(
            (ModelDeploymentRef(deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"),)
        ),
    )


def _create_agent(
    service: ReleaseService,
    *,
    tenant_id: str = "demo",
    project_id: str = TEST_PROJECT_ID,
    logical_agent_id: str = "agent-one",
    owner_id: str = "user-1",
    requested_by: str | None = None,
    owner_kind: AgentOwnerKind = AgentOwnerKind.USER,
    is_platform_owner: bool = False,
) -> None:
    service.create_agent(
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        display_name=logical_agent_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        requested_by=requested_by or owner_id,
        is_platform_owner=is_platform_owner,
    )


def _gated_version(
    service: ReleaseService,
    *,
    tenant_id: str = "demo",
    project_id: str = TEST_PROJECT_ID,
    logical_agent_id: str = "agent-one",
    owner_id: str = "user-1",
    manifest_updates: dict[str, object] | None = None,
) -> AgentVersion:
    _create_agent(
        service,
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        owner_id=owner_id,
    )
    if manifest_updates is not None:
        draft = service._store.get_draft(_scope(tenant_id, project_id), logical_agent_id)
        assert draft is not None
        service.update_draft(
            tenant_id=tenant_id,
            project_id=project_id,
            logical_agent_id=logical_agent_id,
            manifest=draft.manifest.model_copy(update=manifest_updates),
            updated_by=owner_id,
            actor_role=AgentRole.OWNER,
            expected_etag=draft.etag,
        )
    version = service.cut_version(
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        actor_id=owner_id,
        actor_role=AgentRole.OWNER,
    )
    service.run_release_gates(
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version.id,
        actor_id=owner_id,
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    return version


def _release_statuses(service: ReleaseService, version: AgentVersion) -> list[ReleaseStatus]:
    return [
        release.status
        for release in service._store.list_releases_for_version(
            _scope(version.tenant_id, version.project_id),
            version.id,
        )
    ]


def _record_healthy_deployment(
    service: ReleaseService,
    version: AgentVersion,
    *,
    deployment_id: str = "deployment-1",
    health: HealthStatus = HealthStatus.HEALTHY,
    environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
) -> DeploymentRecord:
    scope = _scope(version.tenant_id, version.project_id)
    assert version.runtime_target is not None
    record = DeploymentRecord(
        id=deployment_id,
        logical_agent_id=version.logical_agent_id,
        tenant_id=version.tenant_id,
        project_id=version.project_id,
        version_id=version.id,
        environment=environment,
        runtime_target=version.runtime_target,
        deployed_by="deployer-1",
        health=DeploymentHealth(status=health, detail="smoke test"),
    )
    return service._store.create_deployment(scope, record)


def test_manifest_hash_is_deterministic() -> None:
    local_service = ReleaseService(AgentStudioStore(), seeded_test_registry())
    _create_agent(local_service, logical_agent_id="agent-hash")
    draft = local_service._store.get_draft(_scope(), "agent-hash")
    assert draft is not None

    assert manifest_hash(draft.manifest) == manifest_hash(draft.manifest)
    changed = draft.manifest.model_copy(update={"display_name": "Different"})
    assert manifest_hash(changed) != manifest_hash(draft.manifest)


def test_create_agent_user_owned_grants_owner_role(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        display_name="Agent One",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        requested_by="user-1",
        is_platform_owner=False,
    )

    assert draft.manifest.owner_kind is AgentOwnerKind.USER
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        principal_id="user-1",
    ) is AgentRole.OWNER


def test_create_agent_system_owned_requires_platform_owner(service: ReleaseService) -> None:
    with pytest.raises(AuthorizationError, match="platform owners"):
        service.create_agent(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-sys",
            display_name="System Agent",
            owner_kind=AgentOwnerKind.SYSTEM,
            owner_id="platform",
            requested_by="user-1",
            is_platform_owner=False,
        )


def test_create_agent_system_owned_succeeds_for_platform_owner(service: ReleaseService) -> None:
    draft = service.create_agent(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-sys",
        display_name="System Agent",
        owner_kind=AgentOwnerKind.SYSTEM,
        owner_id="platform",
        requested_by="admin-1",
        is_platform_owner=True,
    )

    assert draft.manifest.owner_kind is AgentOwnerKind.SYSTEM
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-sys",
        principal_id="platform",
    ) is AgentRole.OWNER


def test_create_agent_rejects_duplicate_logical_agent_id(service: ReleaseService) -> None:
    _create_agent(service)

    with pytest.raises(ReleaseServiceError, match="already exists"):
        _create_agent(service, owner_id="user-2")


def test_update_draft_requires_contributor_role(service: ReleaseService) -> None:
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"display_name": "Renamed"}),
            updated_by="user-2",
            actor_role=AgentRole.VIEWER,
            expected_etag=draft.etag,
        )


def test_update_draft_rejects_mismatched_manifest_identity(service: ReleaseService) -> None:
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None

    with pytest.raises(ReleaseServiceError, match="must match"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"logical_agent_id": "agent-other"}),
            updated_by="user-1",
            actor_role=AgentRole.OWNER,
            expected_etag=draft.etag,
        )


def test_update_draft_raises_when_no_draft_exists(service: ReleaseService) -> None:
    manifest = AgentManifest(
        logical_agent_id="agent-missing",
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        display_name="Missing",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
    )

    with pytest.raises(ReleaseServiceError, match="has no draft"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-missing",
            manifest=manifest,
            updated_by="user-1",
            actor_role=AgentRole.OWNER,
            expected_etag="irrelevant-etag",
        )


def test_update_draft_succeeds_with_contributor_role(service: ReleaseService) -> None:
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None

    updated = service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        manifest=draft.manifest.model_copy(update={"display_name": "Renamed Agent"}),
        updated_by="user-1",
        actor_role=AgentRole.CONTRIBUTOR,
        expected_etag=draft.etag,
    )

    assert updated.manifest.display_name == "Renamed Agent"
    # Regression: each successful update must mint a fresh etag so callers
    # can detect concurrent modification; it must never be reused verbatim.
    assert updated.etag != draft.etag


def test_update_draft_rejects_stale_capability_binding(service: ReleaseService) -> None:
    """Finding #4 regression: a client cannot PUT a fabricated/stale binding.

    ``update_draft`` re-resolves every binding on the incoming manifest
    against the *live* registry and rejects the save outright if any
    binding's pinned descriptor digest has drifted -- the stale binding
    must never even reach the draft store, let alone a cut version.
    """
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None
    binding = service._registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")
    tampered = binding.model_copy(
        update={"descriptor_ref": binding.descriptor_ref.model_copy(update={"digest": "sha256:tampered"})}
    )

    with pytest.raises(ReleaseServiceError, match="stale"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"capabilities": (tampered,)}),
            updated_by="user-1",
            actor_role=AgentRole.CONTRIBUTOR,
            expected_etag=draft.etag,
        )
    # Confirm the stale manifest was never persisted.
    unchanged = service._store.get_draft(_scope(), "agent-one")
    assert unchanged is not None
    assert unchanged.manifest.capabilities == ()


def test_update_draft_accepts_fresh_capability_binding(service: ReleaseService) -> None:
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None
    binding = service._registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")

    updated = service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        manifest=draft.manifest.model_copy(update={"capabilities": (binding,)}),
        updated_by="user-1",
        actor_role=AgentRole.CONTRIBUTOR,
        expected_etag=draft.etag,
    )

    assert updated.manifest.capabilities == (binding,)


def test_update_draft_rejects_mismatched_expected_etag(service: ReleaseService) -> None:
    """Finding #6 regression: ``update_draft`` requires ``expected_etag`` and

    rejects the write outright if it no longer matches the currently stored
    draft's etag -- optimistic concurrency must actually be enforced, not
    merely documented on the ``etag`` field.
    """
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None

    with pytest.raises(DraftConflictError, match="concurrently"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"display_name": "Racing Edit"}),
            updated_by="user-1",
            actor_role=AgentRole.CONTRIBUTOR,
            expected_etag="stale-etag-from-an-earlier-read",
        )
    # Confirm the rejected write was never persisted.
    unchanged = service._store.get_draft(_scope(), "agent-one")
    assert unchanged is not None
    assert unchanged.manifest.display_name == draft.manifest.display_name
    assert unchanged.etag == draft.etag


def test_update_draft_second_concurrent_editor_is_rejected_after_first_succeeds(service: ReleaseService) -> None:
    """Two editors read the same draft; the first save succeeds and rotates

    the etag, so the second editor's save (still holding the old etag) must
    be rejected rather than silently clobbering the first editor's change.
    """
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None
    shared_base_etag = draft.etag

    first = service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        manifest=draft.manifest.model_copy(update={"display_name": "First Editor's Change"}),
        updated_by="editor-1",
        actor_role=AgentRole.CONTRIBUTOR,
        expected_etag=shared_base_etag,
    )
    assert first.etag != shared_base_etag

    with pytest.raises(DraftConflictError, match="concurrently"):
        service.update_draft(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            manifest=draft.manifest.model_copy(update={"display_name": "Second Editor's Change"}),
            updated_by="editor-2",
            actor_role=AgentRole.CONTRIBUTOR,
            expected_etag=shared_base_etag,
        )
    current = service._store.get_draft(_scope(), "agent-one")
    assert current is not None
    assert current.manifest.display_name == "First Editor's Change"


def test_fork_rejects_unknown_source_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.fork(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            source_logical_agent_id="agent-parent",
            source_version_id="missing-version",
            new_logical_agent_id="agent-fork",
            requested_by="user-2",
        )


def test_fork_rejects_cross_tenant_source_version(service: ReleaseService) -> None:
    _create_agent(service, tenant_id="tenant-a", logical_agent_id="agent-parent")
    version = service.cut_version(
        tenant_id="tenant-a",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ReleaseServiceError, match="not found"):
        service.fork(
            tenant_id="tenant-b",
            project_id=TEST_PROJECT_ID,
            source_logical_agent_id="agent-parent",
            source_version_id=version.id,
            new_logical_agent_id="agent-fork",
            requested_by="user-2",
        )


def test_fork_rejects_cross_project_source_version(service: ReleaseService) -> None:
    _create_agent(service, project_id=TEST_PROJECT_ID, logical_agent_id="agent-parent")
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ReleaseServiceError, match="not found"):
        service.fork(
            tenant_id="demo",
            project_id=OTHER_PROJECT_ID,
            source_logical_agent_id="agent-parent",
            source_version_id=version.id,
            new_logical_agent_id="agent-fork",
            requested_by="user-2",
        )


def test_fork_succeeds_and_creates_private_user_owned_draft(service: ReleaseService) -> None:
    _create_agent(
        service,
        logical_agent_id="agent-parent",
        owner_kind=AgentOwnerKind.SYSTEM,
        owner_id="platform",
        requested_by="admin-1",
        is_platform_owner=True,
    )
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="admin-1",
        actor_role=AgentRole.OWNER,
    )

    forked = service.fork(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        source_logical_agent_id="agent-parent",
        source_version_id=version.id,
        new_logical_agent_id="agent-fork",
        requested_by="researcher-1",
    )

    assert forked.manifest.owner_kind is AgentOwnerKind.USER
    assert forked.manifest.owner_id == "researcher-1"
    assert forked.manifest.visibility is AgentVisibility.PRIVATE
    assert forked.based_on_version_id == version.id
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-fork",
        principal_id="researcher-1",
    ) is AgentRole.OWNER


def test_fork_rejects_duplicate_new_logical_agent_id(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-parent")
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    _create_agent(service, logical_agent_id="agent-fork", owner_id="user-2")

    with pytest.raises(ReleaseServiceError, match="already exists"):
        service.fork(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            source_logical_agent_id="agent-parent",
            source_version_id=version.id,
            new_logical_agent_id="agent-fork",
            requested_by="user-2",
        )


def test_cut_version_requires_contributor_role(service: ReleaseService) -> None:
    _create_agent(service)

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.cut_version(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            actor_id="user-2",
            actor_role=AgentRole.VIEWER,
        )


def test_cut_version_raises_when_no_draft_exists(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="has no draft"):
        service.cut_version(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-missing",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_cut_version_rejects_cross_project_draft(service: ReleaseService) -> None:
    _create_agent(service, project_id=TEST_PROJECT_ID, logical_agent_id="agent-scoped")

    with pytest.raises(ReleaseServiceError, match="has no draft"):
        service.cut_version(
            tenant_id="demo",
            project_id=OTHER_PROJECT_ID,
            logical_agent_id="agent-scoped",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_cut_version_hard_fails_when_a_capability_binding_goes_stale_before_cut(service: ReleaseService) -> None:
    """Finding #4 regression: cutting an immutable version must re-resolve

    every binding against the live registry, not just trust what
    ``update_draft`` accepted earlier. A descriptor removed from the
    catalog between draft-save and cut must hard-block the cut.
    """
    _create_agent(service, logical_agent_id="agent-binding-cut")
    draft = service._store.get_draft(_scope(), "agent-binding-cut")
    assert draft is not None
    binding = service._registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")
    service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-binding-cut",
        manifest=draft.manifest.model_copy(update={"capabilities": (binding,)}),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
        expected_etag=draft.etag,
    )
    # Simulate the descriptor disappearing from the live catalog between the
    # (successful) draft save and the cut -- the previously-fresh binding is
    # now stale and must hard-block the cut.
    service._registry = CapabilityRegistry(descriptors=())

    with pytest.raises(ReleaseServiceError, match="stale"):
        service.cut_version(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-binding-cut",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_cut_version_uses_allocate_version_builder_and_freezes_fields() -> None:
    store = SpyAllocateStore()
    service = ReleaseService(
        store,
        seeded_test_registry(),
        model_discovery=InMemoryModelDiscovery(
            (ModelDeploymentRef(deployment_name="gpt-4o-mini", model_name="gpt-4o-mini", model_format="openai"),)
        ),
    )
    _create_agent(service, logical_agent_id="agent-cut-fields")
    draft = store.get_draft(_scope(), "agent-cut-fields")
    assert draft is not None
    binding = service._registry.attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
    )
    updated_manifest = draft.manifest.model_copy(
        update={
            "model_deployment": ModelDeploymentRef(
                deployment_name="gpt-4o-mini",
                model_name="gpt-4o-mini",
                model_format="openai",
            ),
            "capabilities": (binding,),
        }
    )
    service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cut-fields",
        manifest=updated_manifest,
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
        expected_etag=draft.etag,
    )

    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cut-fields",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert store.allocate_calls == [("demo", TEST_PROJECT_ID, "agent-cut-fields")]
    assert store.built_version == version
    assert version.sequence == 7
    assert version.manifest == updated_manifest
    assert version.manifest_hash == manifest_hash(updated_manifest)
    assert version.runtime_target is RuntimeTarget.MANAGED_FOUNDRY
    assert version.model_deployment == updated_manifest.model_deployment
    assert len(version.capability_versions) == 1
    assert version.capability_versions[0].descriptor_ref.id == "foundry.web_search"
    assert version.capability_versions[0].operation_ref.id == "search"
    assert version.capability_versions[0].binding_id == binding.binding_id
    assert version.artifact_metadata.package_versions
    assert version.artifact_metadata.lock_digest is not None
    assert version.artifact_metadata.lock_digest.startswith("sha256:")
    assert version.protocol_version == AGENT_STUDIO_PROTOCOL_VERSION


def test_cut_version_capability_versions_preserves_distinct_bindings_for_same_descriptor(
    service: ReleaseService,
) -> None:
    """Finding #8 regression: two bindings attaching the *same* descriptor

    via different discovered instances must both survive onto
    ``AgentVersion.capability_versions`` as distinct ``CapabilityVersionPin``
    entries. The former ``dict[str, str]`` keyed only by
    ``descriptor_ref.id`` silently collapsed this case to a single,
    last-write-wins entry -- a future workflow compiler (or any other
    consumer) must instead see the exact, non-lossy pinned list.
    """
    _create_agent(service, logical_agent_id="agent-multi-binding")
    draft = service._store.get_draft(_scope(), "agent-multi-binding")
    assert draft is not None

    instance_a = service._registry.register_instance(
        CapabilityInstance(
            id="instance-a",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            descriptor_id="foundry.web_search",
            readiness=InstanceReadiness.READY,
            registered_by="user-1",
        )
    )
    instance_b = service._registry.register_instance(
        CapabilityInstance(
            id="instance-b",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            descriptor_id="foundry.web_search",
            readiness=InstanceReadiness.READY,
            registered_by="user-1",
        )
    )
    binding_a = service._registry.attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance_a.id,
    )
    binding_b = service._registry.attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance_b.id,
    )
    assert binding_a.binding_id != binding_b.binding_id

    service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-multi-binding",
        manifest=draft.manifest.model_copy(update={"capabilities": (binding_a, binding_b)}),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
        expected_etag=draft.etag,
    )

    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-multi-binding",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert len(version.capability_versions) == 2
    pinned_binding_ids = {pin.binding_id for pin in version.capability_versions}
    assert pinned_binding_ids == {binding_a.binding_id, binding_b.binding_id}
    pinned_instance_ids = {pin.instance_ref.id for pin in version.capability_versions if pin.instance_ref is not None}
    assert pinned_instance_ids == {"instance-a", "instance-b"}


def test_cut_version_sets_sequence_parent_and_fork_lineage_once(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-parent")
    first = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    second = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-parent",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert first.sequence == 1
    assert first.parent_version_id is None
    assert second.sequence == 2
    assert second.parent_version_id == first.id

    service.fork(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        source_logical_agent_id="agent-parent",
        source_version_id=first.id,
        new_logical_agent_id="agent-fork",
        requested_by="user-2",
    )
    fork_v1 = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-fork",
        actor_id="user-2",
        actor_role=AgentRole.OWNER,
    )
    fork_v2 = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-fork",
        actor_id="user-2",
        actor_role=AgentRole.OWNER,
    )

    lineage = service._store.list_lineage(_scope(), "agent-fork")
    assert fork_v1.fork_of_version_id == first.id
    assert fork_v2.fork_of_version_id is None
    assert len(lineage) == 1
    assert lineage[0].parent_version_id == first.id
    assert lineage[0].child_version_id == fork_v1.id


def test_cut_version_skips_lineage_when_fork_source_is_missing(service: ReleaseService) -> None:
    _create_agent(service)
    draft = service._store.get_draft(_scope(), "agent-one")
    assert draft is not None
    service._store.save_draft(_scope(), draft.model_copy(update={"based_on_version_id": "ghost-version"}))

    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert version.fork_of_version_id == "ghost-version"
    assert service._store.list_lineage(_scope(), "agent-one") == ()


def test_run_release_gates_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.run_release_gates(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id="missing",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence=GateEvidence(),
        )


def test_run_release_gates_rejects_cross_project_version(service: ReleaseService) -> None:
    _create_agent(service, project_id=TEST_PROJECT_ID, logical_agent_id="agent-cross-project-version")
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-cross-project-version",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ReleaseServiceError, match="not found"):
        service.run_release_gates(
            tenant_id="demo",
            project_id=OTHER_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        )


def test_run_release_gates_requires_contributor_role(service: ReleaseService) -> None:
    version = _gated_version(service)

    with pytest.raises(AuthorizationError):
        service.run_release_gates(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="viewer-1",
            actor_role=AgentRole.VIEWER,
            evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        )


def test_run_release_gates_threads_runtime_target_and_records_advisory_evaluations(
    monkeypatch: pytest.MonkeyPatch,
    service: ReleaseService,
) -> None:
    version = _gated_version(
        service,
        logical_agent_id="agent-runtime",
        manifest_updates={
            "runtime_requirements": RuntimeRequirements(requires_custom_code=True),
            "evaluation_suite_refs": ("eval-suite-1",),
        },
    )
    service._store._releases.clear()
    service._store._releases_by_version.clear()
    calls: list[RuntimeTarget | None] = []

    def fake_run_gates(
        *,
        version_id: str,
        report_id: str,
        manifest: AgentManifest,
        manifest_hash: str,
        capability_catalog: Mapping[str, CapabilityDescriptor],
        capability_registry: CapabilityRegistry,
        evidence: GateEvidence,
        runtime_target: RuntimeTarget | None,
        capability_approvals: tuple[StudioApprovalRecord, ...] = (),
        revoked_approval_ids: frozenset[str] = frozenset(),
    ) -> ReleaseGateReport:
        calls.append(runtime_target)
        idx = len(calls)
        return ReleaseGateReport(
            id=f"report-{idx}",
            version_id=version_id,
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            results=(GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED),),
            evaluations=(
                EvaluationRecord(
                    id=f"eval-{idx}",
                    version_id=version_id,
                    evaluator="suite",
                    score=0.0,
                    summary="Advisory evaluation warning.",
                ),
            ),
        )

    monkeypatch.setattr(release_service_module, "run_gates", fake_run_gates)

    first = service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )
    second = service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )

    releases = service._store.list_releases_for_version(_scope(), version.id)
    assert calls == [RuntimeTarget.CUSTOM_HOSTED, RuntimeTarget.CUSTOM_HOSTED]
    assert first.evaluations[0].summary == "Advisory evaluation warning."
    assert [release.status for release in releases] == [ReleaseStatus.GATED, ReleaseStatus.GATED]
    assert releases[0].previous_release_id is None
    assert releases[1].previous_release_id == releases[0].id
    assert releases[0].gate_report_id == first.id
    assert releases[1].gate_report_id == second.id


def test_run_release_gates_failed_report_creates_no_release(
    monkeypatch: pytest.MonkeyPatch,
    service: ReleaseService,
) -> None:
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    def fake_run_gates(
        *,
        version_id: str,
        report_id: str,
        manifest: AgentManifest,
        manifest_hash: str,
        capability_catalog: Mapping[str, CapabilityDescriptor],
        capability_registry: CapabilityRegistry,
        evidence: GateEvidence,
        runtime_target: RuntimeTarget | None,
        capability_approvals: tuple[StudioApprovalRecord, ...] = (),
        revoked_approval_ids: frozenset[str] = frozenset(),
    ) -> ReleaseGateReport:
        return ReleaseGateReport(
            id="report-fail",
            version_id=version_id,
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            results=(GateResult(name=GateName.TEST, status=GateStatus.FAILED, detail="Tests failed."),),
        )

    monkeypatch.setattr(release_service_module, "run_gates", fake_run_gates)

    report = service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(),
    )

    assert not report.passed
    assert service._store.get_gate_report(_scope(), "report-fail") == report
    assert service._store.list_releases_for_version(_scope(), version.id) == ()


def test_run_release_gates_records_actual_actor_as_created_by(service: ReleaseService) -> None:
    """The GATED release must attribute the actor who ran the gates, never
    the original version author -- otherwise a maintainer running gates on
    someone else's version would silently misattribute the audit trail."""
    _create_agent(service, owner_id="author-1")
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="author-1",
        actor_role=AgentRole.OWNER,
    )
    service._store.grant_ownership(
        _scope(),
        OwnershipGrant(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-one",
            principal_id="maintainer-1",
            role=AgentRole.MAINTAINER,
            granted_by="author-1",
        ),
    )

    report = service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="maintainer-1",
        actor_role=AgentRole.MAINTAINER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )

    assert report.passed
    releases = service._store.list_releases_for_version(_scope(), version.id)
    assert len(releases) == 1
    assert releases[0].created_by == "maintainer-1"
    assert version.created_by == "author-1"


# --- harness release linkage (harness blocker #1: signed release linkage) --


def test_run_release_gates_omits_harness_linkage_by_default(service: ReleaseService) -> None:
    version = _gated_version(service)

    release = service._store.latest_release_for_version(_scope(), version.id)

    assert release is not None
    assert release.harness_release_id is None
    assert release.harness_manifest_digest is None
    assert release.harness_link_schema_version is None


def test_run_release_gates_persists_harness_release_linkage_when_supplied(service: ReleaseService) -> None:
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        harness_release_id="harness-release-xyz",
        harness_manifest_digest="sha256:" + "c" * 64,
    )

    release = service._store.latest_release_for_version(_scope(), version.id)
    assert release is not None
    assert release.harness_release_id == "harness-release-xyz"
    assert release.harness_manifest_digest == "sha256:" + "c" * 64
    assert release.harness_link_schema_version == HARNESS_RELEASE_LINK_SCHEMA_VERSION
    # Never asserted equal to this package's own manifest_hash.
    assert release.harness_manifest_digest != release.manifest_hash


def test_run_release_gates_rejects_harness_release_id_without_manifest_digest(service: ReleaseService) -> None:
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ValueError, match="must be supplied together"):
        service.run_release_gates(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
            harness_release_id="harness-release-xyz",
        )


def test_run_release_gates_rejects_harness_manifest_digest_without_release_id(service: ReleaseService) -> None:
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ValueError, match="must be supplied together"):
        service.run_release_gates(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
            harness_manifest_digest="sha256:" + "c" * 64,
        )


def test_harness_release_linkage_propagates_through_promote_and_activate(service: ReleaseService) -> None:
    """The harness identity established at gate-run time must survive every
    later governance transition of the same version (promote, activate) --
    it identifies the one immutable version those transitions all govern,
    not any single lifecycle snapshot of it."""

    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        harness_release_id="harness-release-xyz",
        harness_manifest_digest="sha256:" + "c" * 64,
    )

    service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )
    _record_healthy_deployment(service, version)
    active = service.activate_release(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-3",
        actor_role=AgentRole.MAINTAINER,
    )

    releases = service._store.list_releases_for_version(_scope(), version.id)
    assert [r.status for r in releases] == [ReleaseStatus.GATED, ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE]
    for release in releases:
        assert release.harness_release_id == "harness-release-xyz"
        assert release.harness_manifest_digest == "sha256:" + "c" * 64
        assert release.harness_link_schema_version == HARNESS_RELEASE_LINK_SCHEMA_VERSION
    assert active.harness_release_id == "harness-release-xyz"


def test_run_release_gates_raises_promotion_conflict_when_a_concurrent_gate_wins_the_race(
    service: ReleaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers running gates for the same never-yet-released version can
    both read ``latest_release_for_version() is None`` before either writes.
    Only one may win the race to create the first (root) GATED release; the
    loser must see a ``ReleasePromotionConflictError``, never a silent
    sibling release."""
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    scope = _scope()
    # Simulate the concurrent winner: it already created the root GATED
    # release (previous_release_id=None) for this version.
    service._store.create_release(
        scope,
        AgentRelease(
            id="winner-release",
            version_id=version.id,
            logical_agent_id=version.logical_agent_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            manifest_hash=version.manifest_hash,
            status=ReleaseStatus.GATED,
            gate_report_id="winner-report",
            previous_release_id=None,
            created_by="other-user",
            detail="Concurrent winner.",
        ),
    )
    # Freeze this call's view of "current predecessor" at the stale,
    # pre-race value (None) so it computes the exact same
    # previous_release_id the concurrent winner already claimed above --
    # this reproduces the actual race window under test, not a mocked-away
    # code path.
    monkeypatch.setattr(service._store, "latest_release_for_version", lambda *a, **k: None)

    with pytest.raises(ReleasePromotionConflictError, match="concurrently gated/released"):
        service.run_release_gates(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
        )
    # Only the winner's release exists; the loser never persisted a sibling.
    assert [r.id for r in service._store.list_releases_for_version(scope, version.id)] == ["winner-release"]


def test_request_promotion_requires_contributor_role(service: ReleaseService) -> None:
    version = _gated_version(service)

    with pytest.raises(AuthorizationError):
        service.request_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="viewer-1",
            actor_role=AgentRole.VIEWER,
            destination="production",
            evidence_summary="evidence",
        )


def test_request_promotion_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.request_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id="missing",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            destination="production",
            evidence_summary="evidence",
        )


def test_request_promotion_raises_when_version_has_not_been_gated(service: ReleaseService) -> None:
    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    with pytest.raises(ReleaseServiceError, match="status 'none'"):
        service.request_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            destination="production",
            evidence_summary="evidence",
        )


def test_request_promotion_auto_promotes_and_blocks_repromotion_once_approved(service: ReleaseService) -> None:
    version = _gated_version(service)

    result = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )

    releases = service._store.list_releases_for_version(_scope(), version.id)
    assert result == version
    assert service._store.list_approvals(_scope(), version.id) == ()
    # Promotion stops at APPROVED: ACTIVE requires a separate, explicit
    # ``activate_release`` call gated on deploy + healthy smoke evidence —
    # it is never an automatic side effect of approval.
    assert [release.status for release in releases] == [
        ReleaseStatus.GATED,
        ReleaseStatus.APPROVED,
    ]
    assert releases[1].previous_release_id == releases[0].id
    assert releases[1].manifest_hash == version.manifest_hash

    with pytest.raises(ReleaseServiceError, match="status 'approved'"):
        service.request_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
            destination="production",
            evidence_summary="Try again.",
        )


def test_request_promotion_raises_promotion_conflict_when_a_concurrent_promotion_wins_the_race(
    service: ReleaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two approvers deciding separate approval records for the same GATED
    version can both read the same GATED predecessor before either has
    transitioned it. Only one may win the race to create the APPROVED
    release; the loser must see a ``ReleasePromotionConflictError``."""
    version = _gated_version(service)
    scope = _scope()
    gated = service._store.latest_release_for_version(scope, version.id)
    assert gated is not None
    # Simulate the concurrent winner: another approval already promoted the
    # exact same GATED predecessor to APPROVED.
    service._store.create_release(
        scope,
        AgentRelease(
            id="winner-approved-release",
            version_id=version.id,
            logical_agent_id=version.logical_agent_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            manifest_hash=version.manifest_hash,
            status=ReleaseStatus.APPROVED,
            gate_report_id=gated.gate_report_id,
            approval_id="other-approval",
            previous_release_id=gated.id,
            created_by="other-user",
            detail="Concurrent winner.",
        ),
    )
    # Freeze this call's view of "current predecessor" at the stale,
    # pre-race GATED value so it computes the exact same
    # previous_release_id the concurrent winner already claimed above.
    monkeypatch.setattr(service._store, "latest_release_for_version", lambda *a, **k: gated)

    with pytest.raises(ReleasePromotionConflictError, match="concurrently promoted"):
        service.request_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
            destination="production",
            evidence_summary="All hard gates green.",
        )
    # Only the winner's APPROVED release exists; the loser never persisted a
    # sibling successor, and no approval record was left dangling either.
    releases = service._store.list_releases_for_version(scope, version.id)
    assert [r.id for r in releases] == [gated.id, "winner-approved-release"]
    assert service._store.list_approvals(scope, version.id) == ()


def test_activate_release_requires_maintainer_role(service: ReleaseService) -> None:
    version = _gated_version(service)
    service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )
    _record_healthy_deployment(service, version)

    with pytest.raises(AuthorizationError):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-2",
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_activate_release_blocked_when_not_approved(service: ReleaseService) -> None:
    version = _gated_version(service)

    with pytest.raises(ReleaseServiceError, match="must be APPROVED"):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_activate_release_blocked_without_deployment(service: ReleaseService) -> None:
    version = _gated_version(service)
    service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )

    with pytest.raises(ReleaseServiceError, match="no successful deployment"):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_activate_release_blocked_without_healthy_smoke(service: ReleaseService) -> None:
    version = _gated_version(service)
    service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )
    _record_healthy_deployment(service, version, health=HealthStatus.UNHEALTHY)

    with pytest.raises(ReleaseServiceError, match="no successful deployment"):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_activate_release_succeeds_after_healthy_deployment(service: ReleaseService) -> None:
    version = _gated_version(service)
    approved = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )
    deployment = _record_healthy_deployment(service, version)
    approved_release = service._store.latest_release_for_version(_scope(), version.id)
    assert approved_release is not None

    active = service.activate_release(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-3",
        actor_role=AgentRole.MAINTAINER,
    )

    assert active.status is ReleaseStatus.ACTIVE
    assert active.deployment_id == deployment.id
    assert active.previous_release_id == approved_release.id
    assert active.manifest_hash == version.manifest_hash
    assert active.gate_report_id == approved_release.gate_report_id
    assert _release_statuses(service, version) == [
        ReleaseStatus.GATED,
        ReleaseStatus.APPROVED,
        ReleaseStatus.ACTIVE,
    ]
    _ = approved


def test_activate_release_raises_promotion_conflict_when_a_concurrent_activation_wins_the_race(
    service: ReleaseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two activation requests for the same APPROVED release (e.g. retried
    after a slow response) can both read the same APPROVED predecessor
    before either has transitioned it. Only one may win the race to create
    the ACTIVE release; the loser must see a ``ReleasePromotionConflictError``,
    never a second sibling ACTIVE release for the same predecessor."""
    version = _gated_version(service)
    service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-1",
        actor_role=AgentRole.MAINTAINER,
        destination="production",
        evidence_summary="All hard gates green.",
    )
    _record_healthy_deployment(service, version)
    scope = _scope()
    approved = service._store.latest_release_for_version(scope, version.id)
    assert approved is not None and approved.status is ReleaseStatus.APPROVED
    # Simulate the concurrent winner: another activation request already
    # transitioned the exact same APPROVED predecessor to ACTIVE.
    service._store.create_release(
        scope,
        AgentRelease(
            id="winner-active-release",
            version_id=version.id,
            logical_agent_id=version.logical_agent_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            manifest_hash=version.manifest_hash,
            status=ReleaseStatus.ACTIVE,
            gate_report_id=approved.gate_report_id,
            approval_id=approved.approval_id,
            deployment_id="winner-deployment",
            previous_release_id=approved.id,
            created_by="other-user",
            detail="Concurrent winner.",
        ),
    )
    # Freeze this call's view of "current predecessor" at the stale,
    # pre-race APPROVED value so it computes the exact same
    # previous_release_id the concurrent winner already claimed above.
    monkeypatch.setattr(service._store, "latest_release_for_version", lambda *a, **k: approved)

    with pytest.raises(ReleasePromotionConflictError, match="concurrently activated"):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            actor_id="user-3",
            actor_role=AgentRole.MAINTAINER,
        )
    # Only the winner's ACTIVE release exists; the loser never persisted a
    # sibling ACTIVE successor for the same APPROVED predecessor.
    releases = service._store.list_releases_for_version(scope, version.id)
    assert [r.id for r in releases[-2:]] == [approved.id, "winner-active-release"]
    assert _release_statuses(service, version) == [
        ReleaseStatus.GATED,
        ReleaseStatus.APPROVED,
        ReleaseStatus.ACTIVE,
    ]


def test_activate_release_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.activate_release(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id="missing",
            actor_id="user-1",
            actor_role=AgentRole.MAINTAINER,
        )


def test_request_promotion_creates_idempotent_context_bound_approval(service: ReleaseService) -> None:
    version = _gated_version(service)

    first = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready for review.",
        risk="low",
        environment=DeploymentEnvironment.DEVELOPMENT,
        permissions_policy_ref="perm-policy-v1",
        destination_policy_ref="dest-policy-v1",
    )
    second = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready for review.",
        risk="low",
        environment=DeploymentEnvironment.DEVELOPMENT,
        permissions_policy_ref="perm-policy-v1",
        destination_policy_ref="dest-policy-v1",
    )

    assert first == second
    assert isinstance(first, StudioApprovalRecord)
    assert first.kind is ApprovalKind.RELEASE_PROMOTION
    assert first.state is ApprovalState.PENDING
    assert first.content_hash == version.manifest_hash
    assert first.environment is DeploymentEnvironment.DEVELOPMENT
    assert first.permissions_policy_ref == "perm-policy-v1"
    assert first.destination_policy_ref == "dest-policy-v1"
    assert first.expires_at is not None
    assert first.idempotency_key == idempotency_key(
        kind=ApprovalKind.RELEASE_PROMOTION,
        version_id=version.id,
        requested_by="user-2",
        destination="production",
    )


def test_request_promotion_uses_fork_promotion_kind_for_forked_versions(service: ReleaseService) -> None:
    parent = _gated_version(service, logical_agent_id="agent-parent")
    service.fork(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        source_logical_agent_id="agent-parent",
        source_version_id=parent.id,
        new_logical_agent_id="agent-fork",
        requested_by="user-2",
    )
    fork_version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-fork",
        actor_id="user-2",
        actor_role=AgentRole.OWNER,
    )
    service.run_release_gates(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=fork_version.id,
        actor_id="user-2",
        actor_role=AgentRole.OWNER,
        evidence=GateEvidence(build_succeeded=True, tests_passed=True, smoke_passed=True),
    )

    approval = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=fork_version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Forked agent ready.",
    )

    assert isinstance(approval, StudioApprovalRecord)
    assert approval.kind is ApprovalKind.FORK_PROMOTION


def test_decide_promotion_raises_for_missing_approval(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.decide_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id="missing",
            approver_id="approver-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_promotion_approves_and_promotes(service: ReleaseService) -> None:
    version = _gated_version(service)
    request = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready.",
    )

    decided = service.decide_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        approval_id=request.id,
        approver_id="maintainer-1",
        approver_role=AgentRole.MAINTAINER,
        approve=True,
        rationale="LGTM",
    )

    stored = service._store.get_approval(_scope(), request.id)
    releases = service._store.list_releases_for_version(_scope(), version.id)
    assert decided.state is ApprovalState.APPROVED
    assert stored == decided
    assert stored is not None and stored.approver_id == "maintainer-1"
    assert stored.rationale == "LGTM"
    # decide_promotion stops at APPROVED; ACTIVE requires activate_release().
    assert [release.status for release in releases] == [
        ReleaseStatus.GATED,
        ReleaseStatus.APPROVED,
    ]
    assert releases[1].previous_release_id == releases[0].id
    assert releases[1].approval_id == decided.id


def test_decide_promotion_rejection_does_not_promote(service: ReleaseService) -> None:
    version = _gated_version(service)
    request = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready.",
    )

    decided = service.decide_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        approval_id=request.id,
        approver_id="maintainer-1",
        approver_role=AgentRole.MAINTAINER,
        approve=False,
    )

    assert decided.state is ApprovalState.REJECTED
    assert _release_statuses(service, version) == [ReleaseStatus.GATED]


def test_promote_internal_validates_version_and_gated_release(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service._promote(_scope(), "missing")

    _create_agent(service)
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )
    with pytest.raises(ReleaseServiceError, match="no gated release"):
        service._promote(_scope(), version.id)


def test_decide_promotion_bubbles_up_expired_approval_error(service: ReleaseService) -> None:
    version = _gated_version(service)
    request = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready.",
    )
    assert isinstance(request, StudioApprovalRecord)
    service._store._approvals[request.id] = request.model_copy(
        update={"expires_at": datetime(2026, 1, 1, tzinfo=UTC)}
    )

    with pytest.raises(ApprovalError, match="expired at"):
        service.decide_promotion(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id=request.id,
            approver_id="maintainer-1",
            approver_role=AgentRole.MAINTAINER,
            approve=True,
        )


def test_build_scoped_approval_request_rejects_admin_escalation_without_requested_role() -> None:
    with pytest.raises(ApprovalError, match="must specify requested_role"):
        release_service_module._build_scoped_approval_request(
            approval_id="approval-1",
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id="agent-one",
            kind=ApprovalKind.ADMIN_ESCALATION,
            gated_action="grant_role",
            destination="agent-one",
            requested_by="user-2",
            evidence_summary="Needs owner grant.",
            risk="high",
        )


def test_request_role_escalation_creates_pending_approval(service: ReleaseService) -> None:
    _create_agent(service)

    record = service.request_role_escalation(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        requested_by="user-2",
        requested_role=AgentRole.MAINTAINER,
        evidence_summary="Needs maintainer access.",
    )

    assert record.state is ApprovalState.PENDING
    assert record.kind is ApprovalKind.ADMIN_ESCALATION
    assert record.requested_role is AgentRole.MAINTAINER


def test_decide_role_escalation_raises_for_missing_approval(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="not found"):
        service.decide_role_escalation(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id="missing",
            approver_id="owner-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_role_escalation_rejects_non_escalation_approval(service: ReleaseService) -> None:
    version = _gated_version(service)
    promotion = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready.",
    )

    with pytest.raises(ReleaseServiceError, match="not an admin escalation"):
        service.decide_role_escalation(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id=promotion.id,
            approver_id="owner-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_role_escalation_approved_grants_role(service: ReleaseService) -> None:
    _create_agent(service)
    record = service.request_role_escalation(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        requested_by="user-2",
        requested_role=AgentRole.MAINTAINER,
        evidence_summary="Needs maintainer access.",
    )

    decided = service.decide_role_escalation(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        approval_id=record.id,
        approver_id="user-1",
        approver_role=AgentRole.OWNER,
        approve=True,
        rationale="approved",
    )

    grants = service._store.list_ownership(_scope(), "agent-one")
    assert decided.state is ApprovalState.APPROVED
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        principal_id="user-2",
    ) is AgentRole.MAINTAINER
    assert grants[-1].principal_id == "user-2"
    assert grants[-1].granted_by == "user-1"


def test_decide_role_escalation_rejected_does_not_grant_role(service: ReleaseService) -> None:
    _create_agent(service)
    record = service.request_role_escalation(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        requested_by="user-2",
        requested_role=AgentRole.MAINTAINER,
        evidence_summary="Needs maintainer access.",
    )

    decided = service.decide_role_escalation(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        approval_id=record.id,
        approver_id="user-1",
        approver_role=AgentRole.OWNER,
        approve=False,
    )

    assert decided.state is ApprovalState.REJECTED
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-one",
        principal_id="user-2",
    ) is AgentRole.VIEWER


def test_resolve_actor_role_rejects_cross_project_grant(service: ReleaseService) -> None:
    _create_agent(service, project_id=TEST_PROJECT_ID, logical_agent_id="agent-scope-role")

    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=OTHER_PROJECT_ID,
        logical_agent_id="agent-scope-role",
        principal_id="user-1",
    ) is AgentRole.VIEWER


def test_resolve_actor_role_defaults_to_viewer_for_unknown_principal(service: ReleaseService) -> None:
    assert resolve_actor_role(
        service._store,
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-unknown",
        principal_id="ghost",
    ) is AgentRole.VIEWER


def test_register_tool_requires_contributor_role(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-tool")

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.register_tool(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-tool",
            descriptor_id="foundry.web_search",
            operation="search",
            kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
            handler_ref="builtin://web_search",
            registered_by="user-2",
            actor_role=AgentRole.VIEWER,
        )


def test_register_tool_rejects_non_ga_operation(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-tool-nonga")

    with pytest.raises(CapabilityAttachmentError, match="Cannot attach"):
        service.register_tool(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id="agent-tool-nonga",
            descriptor_id="foundry.memory",
            operation="recall",
            kind=ToolRegistrationKind.CUSTOM_HANDLER,
            handler_ref="custom://memory",
            registered_by="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_register_tool_succeeds_and_lists_registrations(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-tool-ok")

    registration = service.register_tool(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-tool-ok",
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by="user-1",
        actor_role=AgentRole.OWNER,
    )

    assert registration.descriptor_id == "foundry.web_search"
    assert service.list_tool_registrations("demo", TEST_PROJECT_ID, "agent-tool-ok") == (registration,)
    assert service.list_tool_registrations("demo", TEST_PROJECT_ID, "agent-tool-other") == ()


def _service_with_model_deployment_manifest(
    *,
    logical_agent_id: str,
    model_discovery: InMemoryModelDiscovery | UnavailableModelDiscovery,
) -> tuple[ReleaseService, str]:
    """Build a service with the given (fake or unavailable) model discovery
    and a draft manifest that declares a ``model_deployment``, ready for
    ``cut_version`` to trigger ``_revalidate_model_deployment``."""
    local_service = ReleaseService(AgentStudioStore(), seeded_test_registry(), model_discovery=model_discovery)
    _create_agent(local_service, logical_agent_id=logical_agent_id)
    draft = local_service._store.get_draft(_scope(), logical_agent_id)
    assert draft is not None
    local_service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id=logical_agent_id,
        manifest=draft.manifest.model_copy(
            update={
                "model_deployment": ModelDeploymentRef(
                    deployment_name="gpt-4o-mini",
                    model_name="gpt-4o-mini",
                    model_format="openai",
                ),
            }
        ),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
        expected_etag=draft.etag,
    )
    return local_service, logical_agent_id


def test_cut_version_hard_fails_when_model_discovery_is_unavailable() -> None:
    local_service, logical_agent_id = _service_with_model_deployment_manifest(
        logical_agent_id="agent-model-unavailable",
        model_discovery=UnavailableModelDiscovery(),
    )

    with pytest.raises(ReleaseServiceError, match="Cannot revalidate model deployment"):
        local_service.cut_version(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id=logical_agent_id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_cut_version_hard_fails_when_declared_deployment_is_not_live() -> None:
    local_service, logical_agent_id = _service_with_model_deployment_manifest(
        logical_agent_id="agent-model-missing",
        model_discovery=InMemoryModelDiscovery(()),
    )

    with pytest.raises(ReleaseServiceError, match="was not found among the project's live deployed models"):
        local_service.cut_version(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            logical_agent_id=logical_agent_id,
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
        )


def test_request_capability_approval_raises_for_missing_version(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="Version 'missing' not found"):
        service.request_capability_approval(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id="missing",
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence_summary="n/a",
        )


def test_request_capability_approval_raises_when_binding_absent_from_version(service: ReleaseService) -> None:
    version = _gated_version(service, logical_agent_id="agent-no-binding")

    with pytest.raises(ReleaseServiceError, match="has no capability binding for"):
        service.request_capability_approval(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            version_id=version.id,
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            actor_id="user-1",
            actor_role=AgentRole.OWNER,
            evidence_summary="n/a",
        )


def test_decide_capability_approval_raises_for_missing_approval(service: ReleaseService) -> None:
    with pytest.raises(ReleaseServiceError, match="Approval 'missing' not found"):
        service.decide_capability_approval(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id="missing",
            approver_id="user-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_decide_capability_approval_rejects_non_capability_operation_approval(service: ReleaseService) -> None:
    version = _gated_version(service, logical_agent_id="agent-wrong-kind")
    promotion_record = service.request_promotion(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        destination="production",
        evidence_summary="Ready to ship.",
    )
    assert isinstance(promotion_record, StudioApprovalRecord)

    with pytest.raises(ReleaseServiceError, match="is not a capability-operation approval"):
        service.decide_capability_approval(
            tenant_id="demo",
            project_id=TEST_PROJECT_ID,
            approval_id=promotion_record.id,
            approver_id="user-1",
            approver_role=AgentRole.OWNER,
            approve=True,
        )


def test_request_and_decide_capability_approval_happy_path(service: ReleaseService) -> None:
    _create_agent(service, logical_agent_id="agent-capability-approval-service")
    binding = service._registry.attach(
        descriptor_id="foundry.azure_functions",
        operation="invoke",
        attached_by="user-1",
        connection_ref="conn-azure-functions",
        policy_ref="policy.capability-approval.write-irreversible.v1",
    )
    draft = service._store.get_draft(_scope(), "agent-capability-approval-service")
    assert draft is not None
    service.update_draft(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-capability-approval-service",
        manifest=draft.manifest.model_copy(update={"capabilities": (binding,)}),
        updated_by="user-1",
        actor_role=AgentRole.OWNER,
        expected_etag=draft.etag,
    )
    version = service.cut_version(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        logical_agent_id="agent-capability-approval-service",
        actor_id="user-1",
        actor_role=AgentRole.OWNER,
    )

    requested = service.request_capability_approval(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        version_id=version.id,
        descriptor_id="foundry.azure_functions",
        operation="invoke",
        actor_id="user-2",
        actor_role=AgentRole.CONTRIBUTOR,
        evidence_summary="Reviewed the destination and scopes.",
    )

    assert requested.kind is ApprovalKind.CAPABILITY_OPERATION
    assert requested.state is ApprovalState.PENDING
    assert requested.content_hash == version.manifest_hash
    assert requested.destination == "foundry.azure_functions.invoke"

    decided = service.decide_capability_approval(
        tenant_id="demo",
        project_id=TEST_PROJECT_ID,
        approval_id=requested.id,
        approver_id="user-1",
        approver_role=AgentRole.OWNER,
        approve=True,
        rationale="Looks safe.",
    )

    assert decided.state is ApprovalState.APPROVED
    assert decided.id == requested.id
