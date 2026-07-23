# mypy: disable-error-code="attr-defined"
from __future__ import annotations

import sys
import types

import pytest

if "research_assistant_api.agent_studio.cosmos_store" not in sys.modules:
    cosmos_store_stub = types.ModuleType("research_assistant_api.agent_studio.cosmos_store")
    cosmos_store_stub.build_agent_studio_store = lambda *args, **kwargs: None
    sys.modules["research_assistant_api.agent_studio.cosmos_store"] = cosmos_store_stub

from research_assistant_api.agent_studio.artifact_bundle_store import (
    ArtifactBundleStoreError,
    InMemoryArtifactBundleStore,
)
from research_assistant_api.agent_studio.builder_service import (
    BuilderConcurrencyError,
    BuilderNotFoundError,
    BuilderService,
    BuilderServiceError,
    BuilderUnavailableError,
    InMemoryManifestProposalGenerator,
    ProposedManifestChange,
    UnavailableManifestProposalGenerator,
    build_manifest_proposal_generator,
    classify_risk_escalations,
    diff_capability_bindings,
    diff_manifest_fields,
)
from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry, seeded_test_registry
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    BuilderProposalState,
    CapabilityBinding,
    CapabilityChangeKind,
    CapabilityChangeSummary,
    CapabilityConnectionRef,
    CapabilityDescriptorRef,
    CapabilityInstance,
    CapabilityOperationRef,
    DelegationScope,
    InstanceReadiness,
    MemoryPolicy,
    MemoryScopeBinding,
    MemoryScopeKind,
    ModelDeploymentRef,
    ProposalRiskCategory,
    RuntimeRequirements,
    SpecialistPolicy,
)
from research_assistant_api.agent_studio.release_service import AuthorizationError, ReleaseService
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings

TENANT = "demo"
AGENT_ID = "agent-builder-test"
USER_ID = "user-1"
TEST_PROJECT_ID = "proj-1"
OTHER_PROJECT_ID = "proj-2"


def _manifest(*, display_name: str = "Builder Test Agent", **overrides: object) -> AgentManifest:
    base: dict[str, object] = {
        "logical_agent_id": AGENT_ID,
        "tenant_id": TENANT,
        "project_id": TEST_PROJECT_ID,
        "display_name": display_name,
        "owner_kind": AgentOwnerKind.USER,
        "owner_id": USER_ID,
    }
    base.update(overrides)
    return AgentManifest(**base)  # type: ignore[arg-type]


def _scope(project_id: str = TEST_PROJECT_ID) -> ScopeContext:
    return ScopeContext(tenant_id=TENANT, project_id=project_id)


def _binding(
    *, descriptor_id: str = "descriptor-a", operation: str = "search", config: dict[str, object] | None = None
) -> CapabilityBinding:
    return CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id=descriptor_id),
        operation_ref=CapabilityOperationRef(id=operation),
        attached_by=USER_ID,
        config=config or {},
    )


def _draft(
    *, manifest: AgentManifest | None = None, etag: str = "etag-1", project_id: str = TEST_PROJECT_ID
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=AGENT_ID,
        tenant_id=TENANT,
        project_id=project_id,
        manifest=manifest or _manifest(),
        updated_by=USER_ID,
        etag=etag,
    )


def _service(
    *,
    store: AgentStudioStore | None = None,
    generator: object | None = None,
    bundle_store: object | None = None,
    registry: CapabilityRegistry | None = None,
    release_service: ReleaseService | None = None,
) -> BuilderService:
    resolved_store = store or AgentStudioStore()
    resolved_release_service = release_service or ReleaseService(resolved_store, registry or seeded_test_registry())
    return BuilderService(
        store=resolved_store,
        generator=generator or InMemoryManifestProposalGenerator(
            lambda manifest, message: ProposedManifestChange(
                after_manifest=manifest.model_copy(update={"description": message}),
                generator="test-generator",
            )
        ),  # type: ignore[arg-type]
        bundle_store=bundle_store or InMemoryArtifactBundleStore(),  # type: ignore[arg-type]
        release_service=resolved_release_service,
    )


# --------------------------------------------------------------------------
# diff_manifest_fields / diff_capability_bindings
# --------------------------------------------------------------------------


def test_diff_manifest_fields_reports_no_changes_for_identical_manifests() -> None:
    manifest = _manifest()
    assert diff_manifest_fields(manifest, manifest) == ()


def test_diff_manifest_fields_reports_modified_scalar_field() -> None:
    before = _manifest(display_name="Before")
    after = before.model_copy(update={"display_name": "After", "description": "Now with a description."})

    changes = diff_manifest_fields(before, after)
    fields_changed = {change.field for change in changes}
    assert fields_changed == {"display_name", "description"}
    for change in changes:
        assert change.kind.value == "modified"


def test_diff_manifest_fields_excludes_capabilities() -> None:
    before = _manifest()
    after = before.model_copy(update={"capabilities": (_binding(),)})

    changes = diff_manifest_fields(before, after)
    assert all(change.field != "capabilities" for change in changes)


def test_diff_capability_bindings_detects_attach_detach_and_reconfigure() -> None:
    kept = _binding(descriptor_id="descriptor-kept", operation="op-kept", config={"a": 1})
    removed = _binding(descriptor_id="descriptor-removed", operation="op-removed")
    added = _binding(descriptor_id="descriptor-added", operation="op-added")
    reconfigured_before = _binding(descriptor_id="descriptor-reconf", operation="op-reconf", config={"a": 1})
    # A genuine in-place reconfiguration preserves the same binding_id --
    # only a detach+attach mints a fresh one.
    reconfigured_after = reconfigured_before.model_copy(update={"config": {"a": 2}})

    before = _manifest().model_copy(update={"capabilities": (kept, removed, reconfigured_before)})
    after = _manifest().model_copy(update={"capabilities": (kept, added, reconfigured_after)})

    changes = {c.binding_id: c for c in diff_capability_bindings(before, after)}
    assert changes[removed.binding_id].kind is CapabilityChangeKind.DETACHED
    assert changes[added.binding_id].kind is CapabilityChangeKind.ATTACHED
    assert changes[reconfigured_before.binding_id].kind is CapabilityChangeKind.RECONFIGURED
    assert kept.binding_id not in changes
    # descriptor_id/operation are still reported for readability.
    assert changes[removed.binding_id].descriptor_id == "descriptor-removed"
    assert changes[added.binding_id].operation == "op-added"
    assert changes[reconfigured_before.binding_id].descriptor_id == "descriptor-reconf"


def test_diff_capability_bindings_no_tuple_order_noise_for_reordered_unchanged_bindings() -> None:
    """Regression: comparing a manifest's ``capabilities`` after only a
    tuple-order shuffle (e.g. a round-trip that doesn't preserve order) must
    never report spurious changes -- the diff is keyed by ``binding_id``, not
    positional index."""
    binding_a = _binding(descriptor_id="descriptor-a", operation="op-a")
    binding_b = _binding(descriptor_id="descriptor-b", operation="op-b")
    before = _manifest().model_copy(update={"capabilities": (binding_a, binding_b)})
    after = _manifest().model_copy(update={"capabilities": (binding_b, binding_a)})
    assert diff_capability_bindings(before, after) == ()


def test_diff_capability_bindings_distinguishes_same_descriptor_operation_different_instance() -> None:
    """Regression: two distinct bindings sharing a descriptor+operation (e.g.
    attached against different discovered instances) must be reported as a
    genuine detach+attach pair, never collapsed into a single misleading
    'reconfigure' -- this is the collision the former (descriptor_id,
    operation) key introduced."""
    removed = _binding(descriptor_id="descriptor-shared", operation="op-shared", config={"instance": "a"})
    added = _binding(descriptor_id="descriptor-shared", operation="op-shared", config={"instance": "b"})
    before = _manifest().model_copy(update={"capabilities": (removed,)})
    after = _manifest().model_copy(update={"capabilities": (added,)})

    changes = diff_capability_bindings(before, after)
    kinds_by_binding_id = {c.binding_id: c.kind for c in changes}
    assert kinds_by_binding_id == {
        removed.binding_id: CapabilityChangeKind.DETACHED,
        added.binding_id: CapabilityChangeKind.ATTACHED,
    }


def test_diff_capability_bindings_no_changes_for_identical_sets() -> None:
    binding = _binding()
    before = _manifest().model_copy(update={"capabilities": (binding,)})
    after = _manifest().model_copy(update={"capabilities": (binding,)})
    assert diff_capability_bindings(before, after) == ()


# --------------------------------------------------------------------------
# classify_risk_escalations
# --------------------------------------------------------------------------


def test_classify_risk_escalations_reports_nothing_for_identical_manifests() -> None:
    manifest = _manifest()
    assert classify_risk_escalations(manifest, manifest, diff_capability_bindings(manifest, manifest)) == ()


def test_classify_risk_escalations_flags_new_capability_attachment_and_its_destinations() -> None:
    before = _manifest()
    added = CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id="descriptor-added"),
        operation_ref=CapabilityOperationRef(id="op-added"),
        attached_by=USER_ID,
        destination_constraints=("https://example.test/webhook",),
    )
    after = before.model_copy(update={"capabilities": (added,)})
    capability_changes = diff_capability_bindings(before, after)

    escalations = classify_risk_escalations(before, after, capability_changes)
    categories = {e.category for e in escalations}
    assert ProposalRiskCategory.PERMISSION_SCOPE in categories
    assert ProposalRiskCategory.DESTINATION in categories
    assert all(e.binding_id == added.binding_id for e in escalations)


def test_classify_risk_escalations_flags_destination_and_connection_widening_on_reconfigure() -> None:
    before_binding = _binding(descriptor_id="descriptor-reconf", operation="op-reconf")
    after_binding = before_binding.model_copy(
        update={
            "destination_constraints": ("https://example.test/new-destination",),
            "connection_ref": CapabilityConnectionRef(id="new-connection"),
        }
    )
    before = _manifest().model_copy(update={"capabilities": (before_binding,)})
    after = _manifest().model_copy(update={"capabilities": (after_binding,)})
    capability_changes = diff_capability_bindings(before, after)

    escalations = classify_risk_escalations(before, after, capability_changes)
    categories = {e.category for e in escalations}
    assert ProposalRiskCategory.PERMISSION_SCOPE in categories
    assert ProposalRiskCategory.DESTINATION in categories


def test_classify_risk_escalations_ignores_detached_capability_and_unrelated_reconfigure_fields() -> None:
    """Branch coverage: a ``DETACHED`` change must fall through the
    ATTACHED/RECONFIGURED classification entirely (no escalation), a
    reconfigure that only widens ``destination_constraints`` (connection/
    policy unchanged) must not also emit a spurious PERMISSION_SCOPE
    escalation, and a reconfigure that only changes ``connection_ref``
    (destinations unchanged) must not also emit a spurious DESTINATION
    escalation."""
    detached_binding = _binding(descriptor_id="descriptor-detached", operation="op-detached")
    detached_change = CapabilityChangeSummary(
        binding_id=detached_binding.binding_id,
        descriptor_id=detached_binding.descriptor_ref.id,
        operation=detached_binding.operation_ref.id,
        kind=CapabilityChangeKind.DETACHED,
        before=detached_binding,
        after=None,
    )

    destination_only_before = _binding(descriptor_id="descriptor-dest-only", operation="op-dest-only")
    destination_only_after = destination_only_before.model_copy(
        update={"destination_constraints": ("https://example.test/only-destination",)}
    )
    destination_only_change = CapabilityChangeSummary(
        binding_id=destination_only_before.binding_id,
        descriptor_id=destination_only_before.descriptor_ref.id,
        operation=destination_only_before.operation_ref.id,
        kind=CapabilityChangeKind.RECONFIGURED,
        before=destination_only_before,
        after=destination_only_after,
    )

    connection_only_before = _binding(descriptor_id="descriptor-conn-only", operation="op-conn-only")
    connection_only_after = connection_only_before.model_copy(
        update={"connection_ref": CapabilityConnectionRef(id="only-connection")}
    )
    connection_only_change = CapabilityChangeSummary(
        binding_id=connection_only_before.binding_id,
        descriptor_id=connection_only_before.descriptor_ref.id,
        operation=connection_only_before.operation_ref.id,
        kind=CapabilityChangeKind.RECONFIGURED,
        before=connection_only_before,
        after=connection_only_after,
    )

    manifest = _manifest()
    escalations = classify_risk_escalations(
        manifest, manifest, (detached_change, destination_only_change, connection_only_change)
    )

    by_binding_id: dict[str | None, set[ProposalRiskCategory]] = {}
    for escalation in escalations:
        by_binding_id.setdefault(escalation.binding_id, set()).add(escalation.category)

    assert detached_binding.binding_id not in by_binding_id
    assert by_binding_id[destination_only_before.binding_id] == {ProposalRiskCategory.DESTINATION}
    assert by_binding_id[connection_only_before.binding_id] == {ProposalRiskCategory.PERMISSION_SCOPE}


def test_classify_risk_escalations_flags_memory_scope_widening() -> None:
    before = _manifest(
        memory_policy=MemoryPolicy(
            scopes=(MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION, enabled=True, persistent=False),)
        )
    )
    after = before.model_copy(
        update={
            "memory_policy": MemoryPolicy(
                scopes=(
                    MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION, enabled=True, persistent=False),
                    MemoryScopeBinding(kind=MemoryScopeKind.USER, enabled=True, persistent=True),
                )
            )
        }
    )

    escalations = classify_risk_escalations(before, after, ())
    memory_escalations = [e for e in escalations if e.category is ProposalRiskCategory.MEMORY_POLICY]
    assert len(memory_escalations) == 1
    assert "user" in memory_escalations[0].detail


def test_classify_risk_escalations_ignores_memory_scope_that_stays_disabled() -> None:
    before = _manifest()
    after = before.model_copy(
        update={
            "memory_policy": MemoryPolicy(
                scopes=(MemoryScopeBinding(kind=MemoryScopeKind.PROJECT, enabled=False, persistent=False),)
            )
        }
    )
    escalations = classify_risk_escalations(before, after, ())
    assert not any(e.category is ProposalRiskCategory.MEMORY_POLICY for e in escalations)


def test_classify_risk_escalations_flags_specialist_policy_widening() -> None:
    before = _manifest(specialist_policy=SpecialistPolicy(delegation_scope=DelegationScope.NONE))
    after = before.model_copy(
        update={
            "specialist_policy": SpecialistPolicy(
                delegation_scope=DelegationScope.SPECIALIST_POOL,
                allowed_specialist_logical_agent_ids=("specialist-1",),
                max_delegation_depth=1,
            )
        }
    )
    escalations = classify_risk_escalations(before, after, ())
    assert any(e.category is ProposalRiskCategory.SPECIALIST_POLICY for e in escalations)


def test_classify_risk_escalations_ignores_specialist_policy_narrowing() -> None:
    before = _manifest(
        specialist_policy=SpecialistPolicy(
            delegation_scope=DelegationScope.SPECIALIST_POOL,
            allowed_specialist_logical_agent_ids=("specialist-1",),
            max_delegation_depth=2,
        )
    )
    after = before.model_copy(
        update={
            "specialist_policy": SpecialistPolicy(
                delegation_scope=DelegationScope.SPECIALIST_POOL,
                allowed_specialist_logical_agent_ids=(),
                max_delegation_depth=1,
            )
        }
    )
    escalations = classify_risk_escalations(before, after, ())
    assert not any(e.category is ProposalRiskCategory.SPECIALIST_POLICY for e in escalations)


def test_classify_risk_escalations_flags_runtime_requirements_change() -> None:
    before = _manifest(runtime_requirements=RuntimeRequirements())
    after = before.model_copy(update={"runtime_requirements": RuntimeRequirements(requires_custom_code=True)})
    escalations = classify_risk_escalations(before, after, ())
    assert any(e.category is ProposalRiskCategory.RUNTIME for e in escalations)


def test_classify_risk_escalations_flags_model_deployment_change() -> None:
    before = _manifest()
    after = before.model_copy(
        update={
            "model_deployment": ModelDeploymentRef(
                deployment_name="dep-1", model_name="model-1", model_format="openai"
            )
        }
    )
    escalations = classify_risk_escalations(before, after, ())
    assert any(e.category is ProposalRiskCategory.MODEL for e in escalations)


# --------------------------------------------------------------------------
# Generator protocol implementations
# --------------------------------------------------------------------------


def test_unavailable_generator_raises_builder_unavailable_error() -> None:
    generator = UnavailableManifestProposalGenerator()
    with pytest.raises(BuilderUnavailableError, match="unavailable"):
        generator.propose(manifest=_manifest(), message="hello")


def test_in_memory_generator_delegates_to_transform() -> None:
    manifest = _manifest()
    generator = InMemoryManifestProposalGenerator(
        lambda m, message: ProposedManifestChange(after_manifest=m.model_copy(update={"description": message}))
    )
    result = generator.propose(manifest=manifest, message="Add a tool.")
    assert result.after_manifest.description == "Add a tool."


def test_build_manifest_proposal_generator_always_returns_unavailable() -> None:
    generator = build_manifest_proposal_generator(Settings())
    assert isinstance(generator, UnavailableManifestProposalGenerator)


# --------------------------------------------------------------------------
# BuilderService.propose
# --------------------------------------------------------------------------


def test_propose_requires_contributor_role() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.VIEWER,
        )


def test_propose_raises_when_no_draft_exists() -> None:
    service = _service()

    with pytest.raises(BuilderNotFoundError, match="no draft"):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_propose_raises_concurrency_error_on_stale_base_etag() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft(etag="current-etag"))
    service = _service(store=store)

    with pytest.raises(BuilderConcurrencyError, match="does not match"):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="stale-etag",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_propose_raises_when_generator_unavailable() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store, generator=UnavailableManifestProposalGenerator())

    with pytest.raises(BuilderUnavailableError):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_propose_rejects_generator_output_with_mismatched_identity() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    generator = InMemoryManifestProposalGenerator(
        lambda manifest, message: ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"logical_agent_id": "agent-someone-else"})
        )
    )
    service = _service(store=store, generator=generator)

    with pytest.raises(BuilderServiceError, match="must match"):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_propose_succeeds_and_stores_diffs_and_provenance() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="Add a description.",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert proposal.state == BuilderProposalState.PENDING
    assert proposal.tenant_id == TENANT
    assert proposal.project_id == TEST_PROJECT_ID
    assert proposal.logical_agent_id == AGENT_ID
    assert proposal.draft_base_etag == "etag-1"
    assert proposal.provenance.message == "Add a description."
    assert proposal.provenance.requested_by == USER_ID
    assert proposal.provenance.generator == "test-generator"
    assert proposal.after_manifest.description == "Add a description."
    assert any(change.field == "description" for change in proposal.changes)
    assert proposal.source_bundle_ref is None
    assert store.get_builder_proposal(_scope(), proposal.id) == proposal
    assert proposal.risk_escalations == ()


def test_propose_populates_risk_escalations_for_capability_attachment() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    added = CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id="descriptor-added"),
        operation_ref=CapabilityOperationRef(id="op-added"),
        attached_by=USER_ID,
    )
    generator = InMemoryManifestProposalGenerator(
        lambda manifest, message: ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"capabilities": (added,)})
        )
    )
    service = _service(store=store, generator=generator)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="Attach a capability.",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert any(e.category is ProposalRiskCategory.PERMISSION_SCOPE for e in proposal.risk_escalations)
    assert any(c.binding_id == added.binding_id for c in proposal.capability_changes)


def test_propose_stores_source_bundle_content_via_bundle_store() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    bundle_store = InMemoryArtifactBundleStore()
    generator = InMemoryManifestProposalGenerator(
        lambda manifest, message: ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"description": message}),
            source_bundle_content=b"generated code bytes",
        )
    )
    service = _service(store=store, generator=generator, bundle_store=bundle_store)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="Add a tool.",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert proposal.source_bundle_ref is not None
    assert proposal.source_bundle_ref.startswith("memory://")
    stored_key = proposal.source_bundle_ref.removeprefix("memory://")
    assert bundle_store.items[stored_key] == b"generated code bytes"


def test_propose_propagates_bundle_store_failure() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    generator = InMemoryManifestProposalGenerator(
        lambda manifest, message: ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"description": message}),
            source_bundle_content=b"generated code bytes",
        )
    )

    class FailingBundleStore:
        def put(self, **kwargs: object) -> object:
            raise ArtifactBundleStoreError("no storage endpoint is configured")

    service = _service(store=store, generator=generator, bundle_store=FailingBundleStore())

    with pytest.raises(ArtifactBundleStoreError):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="Add a tool.",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_propose_is_cross_project_isolated() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(OTHER_PROJECT_ID), _draft(project_id=OTHER_PROJECT_ID))
    service = _service(store=store)

    with pytest.raises(BuilderNotFoundError, match="no draft"):
        service.propose(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            message="hello",
            base_etag="etag-1",
            requested_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


# --------------------------------------------------------------------------
# BuilderService.list_proposals / get_proposal
# --------------------------------------------------------------------------


def test_list_and_get_proposal() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="hello",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert service.list_proposals(TENANT, TEST_PROJECT_ID, AGENT_ID) == (proposal,)
    assert service.get_proposal(TENANT, TEST_PROJECT_ID, AGENT_ID, proposal.id) == proposal


def test_get_proposal_returns_none_for_unknown_id() -> None:
    service = _service()
    assert service.get_proposal(TENANT, TEST_PROJECT_ID, AGENT_ID, "missing") is None


def test_get_proposal_returns_none_when_fetched_via_wrong_agent_id() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="hello",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert service.get_proposal(TENANT, TEST_PROJECT_ID, "agent-someone-else", proposal.id) is None


def test_list_and_get_proposal_are_cross_project_isolated() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    proposal = service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="hello",
        base_etag="etag-1",
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert service.list_proposals(TENANT, OTHER_PROJECT_ID, AGENT_ID) == ()
    assert service.get_proposal(TENANT, OTHER_PROJECT_ID, AGENT_ID, proposal.id) is None


# --------------------------------------------------------------------------
# BuilderService.apply
# --------------------------------------------------------------------------


def _proposed(service: BuilderService, *, base_etag: str = "etag-1") -> object:
    return service.propose(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        message="Add a description.",
        base_etag=base_etag,
        requested_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )


def test_apply_requires_contributor_role() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.VIEWER,
        )


def test_apply_raises_not_found_for_unknown_proposal() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)

    with pytest.raises(BuilderNotFoundError, match="not found"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id="missing-proposal",
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_raises_when_already_decided() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    service.apply(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        proposal_id=proposal.id,
        base_etag="etag-1",
        applied_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    with pytest.raises(BuilderServiceError, match="already been decided"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_raises_concurrency_error_when_caller_base_etag_is_stale() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    # The draft moves on (e.g. a manual edit) after the proposal was generated.
    current_draft = store.get_draft(_scope(), AGENT_ID)
    assert current_draft is not None
    store.save_draft(_scope(), current_draft.model_copy(update={"etag": "moved-on-etag"}))

    with pytest.raises(BuilderConcurrencyError, match="does not match"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_raises_concurrency_error_when_proposal_itself_is_stale() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    # Draft moves on to a new etag, and the caller's own view moves with it --
    # but the *proposal* was generated against the old etag, so it is stale
    # even though the caller's base_etag now matches the current draft.
    current_draft = store.get_draft(_scope(), AGENT_ID)
    assert current_draft is not None
    store.save_draft(_scope(), current_draft.model_copy(update={"etag": "moved-on-etag"}))

    with pytest.raises(BuilderConcurrencyError, match="stale"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="moved-on-etag",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_raises_concurrency_error_when_store_save_draft_races_after_app_level_checks_pass() -> None:
    """Review finding #6: even when ``apply()``'s own app-level etag checks
    pass, a true cross-instance race window between its ``get_draft`` read
    and its ``save_draft`` write must still be caught -- this exercises the
    store-level ``DraftConflictError`` -> ``BuilderConcurrencyError`` path
    that ``apply()``'s own pre-checks alone cannot close."""

    class RacyStore(AgentStudioStore):
        """A store double that simulates another process winning a write
        race in the gap between ``apply()``'s draft read and its own
        ``save_draft`` call, even though ``apply()``'s pre-checks passed."""

        def __init__(self) -> None:
            super().__init__()
            self.racing = False

        def save_draft(
            self,
            scope: ScopeContext,
            draft: AgentDraft,
            *,
            expected_etag: str | None = None,
        ) -> AgentDraft:
            if self.racing:
                key = (scope.scope_key, draft.logical_agent_id)
                current = self._drafts[key]
                self._drafts[key] = current.model_copy(update={"etag": "raced-out-by-another-instance"})
            return super().save_draft(scope, draft, expected_etag=expected_etag)

    store = RacyStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    store.racing = True
    with pytest.raises(BuilderConcurrencyError, match="no longer matches"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_raises_not_found_when_draft_deleted_after_proposal() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)
    del store._drafts[(_scope().scope_key, AGENT_ID)]

    with pytest.raises(BuilderNotFoundError, match="no draft"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_apply_succeeds_and_updates_draft_and_proposal() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    updated_draft = service.apply(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        proposal_id=proposal.id,
        base_etag="etag-1",
        applied_by=USER_ID,
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert updated_draft.manifest.description == "Add a description."
    assert updated_draft.etag != "etag-1"
    assert updated_draft.updated_by == USER_ID

    decided = service.get_proposal(TENANT, TEST_PROJECT_ID, AGENT_ID, proposal.id)
    assert decided is not None
    assert decided.state == BuilderProposalState.APPLIED
    assert decided.decided_by == USER_ID
    assert decided.applied_draft_etag == updated_draft.etag


def test_apply_is_cross_project_isolated() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    with pytest.raises(BuilderNotFoundError, match="not found"):
        service.apply(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )

    pending = service.get_proposal(TENANT, TEST_PROJECT_ID, AGENT_ID, proposal.id)
    assert pending is not None
    assert pending.state is BuilderProposalState.PENDING


def test_apply_fails_when_capability_instance_drifts_between_propose_and_apply() -> None:
    """Regression: a capability instance can be reconfigured (or otherwise
    go stale) in the window between ``propose()`` generating a candidate
    manifest and a reviewer calling ``apply()``. Because ``apply()``
    delegates the manifest write to ``ReleaseService.update_draft()``
    (rather than a raw store write), this drift must be caught and hard-fail
    ``apply()`` even though the proposal itself was generated validly."""
    registry = seeded_test_registry()
    instance = registry.register_instance(
        CapabilityInstance(
            id="instance-drift",
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            descriptor_id="foundry.web_search",
            readiness=InstanceReadiness.READY,
            registered_by=USER_ID,
        )
    )
    binding = registry.attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by=USER_ID,
        instance_id=instance.id,
    )

    store = AgentStudioStore()
    store.save_draft(_scope(), _draft(manifest=_manifest().model_copy(update={"capabilities": (binding,)})))
    release_service = ReleaseService(store, registry)
    generator = InMemoryManifestProposalGenerator(
        lambda manifest, message: ProposedManifestChange(
            after_manifest=manifest.model_copy(update={"description": message})
        )
    )
    service = _service(store=store, generator=generator, registry=registry, release_service=release_service)
    proposal = _proposed(service)

    # The instance is reconfigured (its config fingerprint changes) after the
    # proposal was generated but before it is applied.
    registry.register_instance(instance.model_copy(update={"config_fingerprint": "changed-config"}))

    with pytest.raises(BuilderServiceError, match="stale"):
        service.apply(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            base_etag="etag-1",
            applied_by=USER_ID,
            actor_role=AgentRole.CONTRIBUTOR,
        )

    # The draft itself must remain untouched by the failed apply.
    unchanged_draft = store.get_draft(_scope(), AGENT_ID)
    assert unchanged_draft is not None
    assert unchanged_draft.etag == "etag-1"


# --------------------------------------------------------------------------
# BuilderService.reject
# --------------------------------------------------------------------------


def test_reject_requires_contributor_role() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    with pytest.raises(AuthorizationError, match="does not meet the minimum"):
        service.reject(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            rejected_by=USER_ID,
            reason="Not needed.",
            actor_role=AgentRole.VIEWER,
        )


def test_reject_raises_not_found_for_unknown_proposal() -> None:
    service = _service()

    with pytest.raises(BuilderNotFoundError, match="not found"):
        service.reject(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id="missing-proposal",
            rejected_by=USER_ID,
            reason="Not needed.",
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_reject_raises_when_already_decided() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    service.reject(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        proposal_id=proposal.id,
        rejected_by=USER_ID,
        reason="Not needed.",
        actor_role=AgentRole.CONTRIBUTOR,
    )

    with pytest.raises(BuilderServiceError, match="already been decided"):
        service.reject(
            tenant_id=TENANT,
            project_id=TEST_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            rejected_by=USER_ID,
            reason="Not needed.",
            actor_role=AgentRole.CONTRIBUTOR,
        )


def test_reject_succeeds_and_records_reason() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    decided = service.reject(
        tenant_id=TENANT,
        project_id=TEST_PROJECT_ID,
        logical_agent_id=AGENT_ID,
        proposal_id=proposal.id,
        rejected_by=USER_ID,
        reason="Behavior change too risky.",
        actor_role=AgentRole.CONTRIBUTOR,
    )

    assert decided.state == BuilderProposalState.REJECTED
    assert decided.decided_by == USER_ID
    assert decided.rejection_reason == "Behavior change too risky."

    # The original draft must be untouched by a rejection.
    draft = store.get_draft(_scope(), AGENT_ID)
    assert draft is not None
    assert draft.etag == "etag-1"


def test_reject_is_cross_project_isolated() -> None:
    store = AgentStudioStore()
    store.save_draft(_scope(), _draft())
    service = _service(store=store)
    proposal = _proposed(service)

    with pytest.raises(BuilderNotFoundError, match="not found"):
        service.reject(
            tenant_id=TENANT,
            project_id=OTHER_PROJECT_ID,
            logical_agent_id=AGENT_ID,
            proposal_id=proposal.id,
            rejected_by=USER_ID,
            reason="Not needed.",
            actor_role=AgentRole.CONTRIBUTOR,
        )

    pending = service.get_proposal(TENANT, TEST_PROJECT_ID, AGENT_ID, proposal.id)
    assert pending is not None
    assert pending.state is BuilderProposalState.PENDING
