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
    diff_capability_bindings,
    diff_manifest_fields,
)
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    BuilderProposalState,
    CapabilityBinding,
)
from research_assistant_api.agent_studio.release_service import AuthorizationError
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
    *, descriptor_id: str = "descriptor-a", operation: str = "search", **overrides: object
) -> CapabilityBinding:
    base: dict[str, object] = {
        "descriptor_id": descriptor_id,
        "operation": operation,
        "attached_by": USER_ID,
    }
    base.update(overrides)
    return CapabilityBinding(**base)  # type: ignore[arg-type]


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
) -> BuilderService:
    return BuilderService(
        store=store or AgentStudioStore(),
        generator=generator or InMemoryManifestProposalGenerator(
            lambda manifest, message: ProposedManifestChange(
                after_manifest=manifest.model_copy(update={"description": message}),
                generator="test-generator",
            )
        ),  # type: ignore[arg-type]
        bundle_store=bundle_store or InMemoryArtifactBundleStore(),  # type: ignore[arg-type]
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
    reconfigured_after = _binding(descriptor_id="descriptor-reconf", operation="op-reconf", config={"a": 2})

    before = _manifest().model_copy(update={"capabilities": (kept, removed, reconfigured_before)})
    after = _manifest().model_copy(update={"capabilities": (kept, added, reconfigured_after)})

    changes = {(c.descriptor_id, c.operation): c for c in diff_capability_bindings(before, after)}
    assert changes[("descriptor-removed", "op-removed")].kind.value == "detached"
    assert changes[("descriptor-added", "op-added")].kind.value == "attached"
    assert changes[("descriptor-reconf", "op-reconf")].kind.value == "reconfigured"
    assert ("descriptor-kept", "op-kept") not in changes


def test_diff_capability_bindings_no_changes_for_identical_sets() -> None:
    binding = _binding()
    before = _manifest().model_copy(update={"capabilities": (binding,)})
    after = _manifest().model_copy(update={"capabilities": (binding,)})
    assert diff_capability_bindings(before, after) == ()


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
