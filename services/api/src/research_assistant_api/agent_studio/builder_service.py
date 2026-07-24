"""Builder Agent backend contract: propose -> researcher review -> apply.

The conversational Builder Agent (owned by the harness's ``agents/**``
surface) never mutates a draft, attaches a connection, approves, or deploys
anything directly. It only ever produces a *stored proposal* here
(``BuilderService.propose``); a human researcher must explicitly ``apply``
(or ``reject``) it via a separate, optimistic-concurrency-guarded call.

Actual natural-language -> manifest generation is out of this module's
ownership (it belongs to the harness's conversational surface). This module
defines only the ``ManifestProposalGenerator`` interface the harness/agents
side implements, consumes it through that interface (mirroring
``model_discovery.ModelDiscovery``), and owns everything downstream of a
generated candidate manifest: validation, diffing, immutable storage,
concurrency-checked apply, and reject/history.

Production wiring (see ``app.py``) fails closed -- ``build_manifest_proposal_
generator`` returns ``UnavailableManifestProposalGenerator`` until a real
generator is wired -- rather than fabricating a fake successful proposal.
``InMemoryManifestProposalGenerator`` is test-only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from research_assistant_api.agent_studio.artifact_bundle_store import ArtifactBundleStore, draft_version_label
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentRole,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    CapabilityChangeKind,
    CapabilityChangeSummary,
    DelegationScope,
    ManifestChangeSummary,
    ManifestFieldChangeKind,
    MemoryScopeKind,
    ProposalRiskCategory,
    ProposalRiskEscalation,
    SanitizedCapabilityBinding,
    role_at_least,
    utc_now,
)
from research_assistant_api.agent_studio.release_service import (
    AuthorizationError,
    ReleaseService,
    ReleaseServiceError,
    manifest_hash,
)
from research_assistant_api.agent_studio.release_service import (
    DraftConflictError as ReleaseDraftConflictError,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore
from research_assistant_api.config import Settings


class BuilderServiceError(RuntimeError):
    pass


class BuilderNotFoundError(BuilderServiceError):
    pass


class BuilderConcurrencyError(BuilderServiceError):
    pass


class BuilderUnavailableError(BuilderServiceError):
    pass


# --------------------------------------------------------------------------
# Provider-owned generation interface
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedManifestChange:
    """A generator's proposed candidate manifest, prior to server-side diffing.

    ``after_manifest`` must already be a real, validated ``AgentManifest``
    instance (never a raw dict/patch) -- the generator is expected to
    construct or validate it as such before returning.
    """

    after_manifest: AgentManifest
    generator: str = "unknown"
    generator_version: str | None = None
    validation_warnings: tuple[str, ...] = field(default_factory=tuple)
    source_bundle_content: bytes | None = None


class ManifestProposalGenerator(Protocol):
    def propose(self, *, manifest: AgentManifest, message: str) -> ProposedManifestChange: ...


class UnavailableManifestProposalGenerator:
    """Explicit cloud-unavailable path: no Builder Agent generator configured."""

    def propose(self, *, manifest: AgentManifest, message: str) -> ProposedManifestChange:
        raise BuilderUnavailableError(
            "No Builder Agent manifest-proposal generator is configured; "
            "conversational proposal generation is unavailable."
        )


class InMemoryManifestProposalGenerator:
    """Test-only, deterministic generator driven by a caller-supplied callable.

    Must never be wired in a cloud/production path; production always uses
    ``UnavailableManifestProposalGenerator`` until a real generator (owned by
    the harness's integration session) is wired via ``build_manifest_
    proposal_generator``.
    """

    def __init__(self, transform: Callable[[AgentManifest, str], ProposedManifestChange]) -> None:
        self._transform = transform

    def propose(self, *, manifest: AgentManifest, message: str) -> ProposedManifestChange:
        return self._transform(manifest, message)


def build_manifest_proposal_generator(settings: Settings) -> ManifestProposalGenerator:
    """Production factory: never returns an in-memory/fake generator.

    There is currently no configured Builder Agent generation provider in
    this codebase (that integration is owned by the harness's ``agents/**``
    surface); this always returns the explicit cloud-unavailable path until
    one is wired, per the "no fake production success" requirement.
    """
    del settings  # no provider configuration exists yet; kept for interface symmetry
    return UnavailableManifestProposalGenerator()


# --------------------------------------------------------------------------
# Deterministic, server-computed diffing
# --------------------------------------------------------------------------

_DIFF_EXCLUDED_FIELDS = frozenset({"capabilities"})


def diff_manifest_fields(before: AgentManifest, after: AgentManifest) -> tuple[ManifestChangeSummary, ...]:
    """Deterministic, top-level field diff between two canonical manifests.

    ``capabilities`` is excluded here and reported separately by
    ``diff_capability_bindings`` since bindings have their own natural
    identity (``descriptor_id``/``operation``) rather than a single scalar.

    Both dumps always share an identical key set: ``model_dump(mode="json")``
    on an ``AgentManifest`` always includes every field declared on the
    (single, shared) class, regardless of whether it was explicitly set. So
    every differing field is always reported as ``MODIFIED`` -- ``ADDED``/
    ``REMOVED`` exist on ``ManifestFieldChangeKind`` only for a hypothetical
    future dict-shaped/partial diff source, not this whole-manifest diff.
    """
    before_dump = before.model_dump(mode="json")
    after_dump = after.model_dump(mode="json")
    changes: list[ManifestChangeSummary] = []
    for field_name in sorted(set(before_dump) | set(after_dump)):
        if field_name in _DIFF_EXCLUDED_FIELDS:
            continue
        before_value = before_dump.get(field_name)
        after_value = after_dump.get(field_name)
        if before_value == after_value:
            continue
        changes.append(
            ManifestChangeSummary(
                field=field_name,
                kind=ManifestFieldChangeKind.MODIFIED,
                before=before_value,
                after=after_value,
            )
        )
    return tuple(changes)


def diff_capability_bindings(before: AgentManifest, after: AgentManifest) -> tuple[CapabilityChangeSummary, ...]:
    """Deterministic capability-binding diff, keyed by ``binding_id``.

    ``binding_id`` (not ``(descriptor_id, operation)``) is the natural
    identity of a ``CapabilityBinding``: two distinct bindings can
    legitimately share the same descriptor+operation (e.g. attached against
    different discovered instances), and keying by that tuple would
    silently collapse a genuine detach+attach pair into a misreported
    "reconfigure". A binding is only ever reported as ``RECONFIGURED`` when
    the *same* ``binding_id`` appears on both sides with different content
    -- which only happens when the manifest-editing path explicitly
    preserves the identity of an existing binding (e.g. via
    ``model_copy(update={...})``) rather than re-attaching (which always
    mints a fresh ``binding_id``).
    """

    before_by_id = {binding.binding_id: binding for binding in before.capabilities}
    after_by_id = {binding.binding_id: binding for binding in after.capabilities}
    changes: list[CapabilityChangeSummary] = []
    for binding_id in sorted(set(before_by_id) | set(after_by_id)):
        before_binding = before_by_id.get(binding_id)
        after_binding = after_by_id.get(binding_id)
        if before_binding == after_binding:
            continue
        reference = after_binding if after_binding is not None else before_binding
        assert reference is not None  # at least one side must be present
        if before_binding is None:
            kind = CapabilityChangeKind.ATTACHED
        elif after_binding is None:
            kind = CapabilityChangeKind.DETACHED
        else:
            kind = CapabilityChangeKind.RECONFIGURED
        changes.append(
            CapabilityChangeSummary(
                binding_id=binding_id,
                descriptor_id=reference.descriptor_ref.id,
                operation=reference.operation_ref.id,
                kind=kind,
                before=SanitizedCapabilityBinding.from_binding(before_binding) if before_binding is not None else None,
                after=SanitizedCapabilityBinding.from_binding(after_binding) if after_binding is not None else None,
            )
        )
    return tuple(changes)


# Ordinal privilege ranking for ``DelegationScope``: strictly increasing with
# how much delegation authority the scope grants. Used so a scope *change* is
# only ever classified as a widening (escalation-worthy) when it moves to a
# strictly higher rank -- e.g. ``SPECIALIST_POOL`` -> ``NONE`` is a narrowing,
# not a widening, even though the enum values differ.
_DELEGATION_SCOPE_RANK: dict[DelegationScope, int] = {
    DelegationScope.NONE: 0,
    DelegationScope.SPECIALIST_POOL: 1,
    DelegationScope.ANY_RELEASED_AGENT: 2,
}

# For each ``RuntimeRequirements`` field, the boolean value that represents
# the *riskier* state. A runtime-requirement escalation is only warranted
# when a field flips *into* its riskier value (e.g. ``requires_custom_code``
# turning on); flipping *out* of it (narrowing, e.g. no longer requiring
# custom code) must never be misreported as an escalation, matching how
# destination-constraint removal and delegation-scope narrowing are already
# excluded above.
_RUNTIME_RISKIER_VALUE: dict[str, bool] = {
    "requires_custom_code": True,
    "requires_custom_orchestration_workflow": True,
    "requires_non_ga_tool": True,
    # Inverted: ``True`` is the safe default (restricted to project-deployed
    # models only), so the riskier state is turning this *off*.
    "uses_project_deployed_model_only": False,
}


def classify_risk_escalations(
    before: AgentManifest,
    after: AgentManifest,
    capability_changes: tuple[CapabilityChangeSummary, ...],
) -> tuple[ProposalRiskEscalation, ...]:
    """Deterministic semantic risk classification for a proposed change.

    Complements the raw field/binding diff with *why* a change matters:
    widened permission/destination scope on a capability binding, memory
    scopes moving from disabled/session-only to enabled/persistent, expanded
    specialist delegation, a runtime-requirement shift (e.g. now requiring
    custom code, or a non-GA tool), or a different declared model
    deployment. Every finding is derived from the already-materialized
    before/after manifests and capability-change list -- nothing here is
    inferred from free-text proposal content.
    """

    escalations: list[ProposalRiskEscalation] = []

    for change in capability_changes:
        if change.kind is CapabilityChangeKind.ATTACHED:
            escalations.append(
                ProposalRiskEscalation(
                    category=ProposalRiskCategory.PERMISSION_SCOPE,
                    detail=(
                        f"New capability attached: '{change.descriptor_id}.{change.operation}' "
                        "grants a permission this agent did not previously have."
                    ),
                    binding_id=change.binding_id,
                )
            )
            if change.after is not None and change.after.destination_constraints:
                escalations.append(
                    ProposalRiskEscalation(
                        category=ProposalRiskCategory.DESTINATION,
                        detail=(
                            f"New capability '{change.descriptor_id}.{change.operation}' can reach "
                            f"destinations: {', '.join(sorted(change.after.destination_constraints))}."
                        ),
                        binding_id=change.binding_id,
                    )
                )
        elif (
            change.kind is CapabilityChangeKind.RECONFIGURED and change.before is not None and change.after is not None
        ):
            if (
                change.before.connection_ref != change.after.connection_ref
                or change.before.policy_ref != change.after.policy_ref
            ):
                escalations.append(
                    ProposalRiskEscalation(
                        category=ProposalRiskCategory.PERMISSION_SCOPE,
                        detail=(
                            f"Capability '{change.descriptor_id}.{change.operation}' reconfigured its "
                            "connection/policy pin, changing what it is authorized to access."
                        ),
                        binding_id=change.binding_id,
                    )
                )
            if set(change.before.destination_constraints) != set(change.after.destination_constraints):
                added = set(change.after.destination_constraints) - set(change.before.destination_constraints)
                if added:
                    escalations.append(
                        ProposalRiskEscalation(
                            category=ProposalRiskCategory.DESTINATION,
                            detail=(
                                f"Capability '{change.descriptor_id}.{change.operation}' destination "
                                f"constraints changed (added: {', '.join(sorted(added))})."
                            ),
                            binding_id=change.binding_id,
                        )
                    )

    present_scope_kinds = {binding.kind for binding in (*before.memory_policy.scopes, *after.memory_policy.scopes)}
    for scope_kind in (kind for kind in MemoryScopeKind if kind in present_scope_kinds):
        before_scope = before.memory_policy.scope(scope_kind)
        after_scope = after.memory_policy.scope(scope_kind)
        before_enabled = before_scope is not None and before_scope.enabled
        after_enabled = after_scope is not None and after_scope.enabled
        before_persistent = before_scope is not None and before_scope.persistent
        after_persistent = after_scope is not None and after_scope.persistent
        if (not before_enabled and after_enabled) or (not before_persistent and after_persistent):
            escalations.append(
                ProposalRiskEscalation(
                    category=ProposalRiskCategory.MEMORY_POLICY,
                    detail=(
                        f"Memory scope '{scope_kind.value}' widened "
                        f"(enabled {before_enabled}->{after_enabled}, "
                        f"persistent {before_persistent}->{after_persistent})."
                    ),
                )
            )

    if before.specialist_policy != after.specialist_policy:
        before_ids = set(before.specialist_policy.allowed_specialist_logical_agent_ids)
        after_ids = set(after.specialist_policy.allowed_specialist_logical_agent_ids)
        widened = (
            _DELEGATION_SCOPE_RANK[after.specialist_policy.delegation_scope]
            > _DELEGATION_SCOPE_RANK[before.specialist_policy.delegation_scope]
            or after.specialist_policy.max_delegation_depth > before.specialist_policy.max_delegation_depth
            or bool(after_ids - before_ids)
        )
        if widened:
            escalations.append(
                ProposalRiskEscalation(
                    category=ProposalRiskCategory.SPECIALIST_POLICY,
                    detail=(
                        f"Specialist delegation policy widened: scope "
                        f"{before.specialist_policy.delegation_scope.value}->"
                        f"{after.specialist_policy.delegation_scope.value}, depth "
                        f"{before.specialist_policy.max_delegation_depth}->"
                        f"{after.specialist_policy.max_delegation_depth}, added agents: "
                        f"{', '.join(sorted(after_ids - before_ids)) or 'none'}."
                    ),
                )
            )

    if before.runtime_requirements != after.runtime_requirements:
        widened_fields = [
            field
            for field, riskier_value in _RUNTIME_RISKIER_VALUE.items()
            if getattr(before.runtime_requirements, field) != riskier_value
            and getattr(after.runtime_requirements, field) == riskier_value
        ]
        if widened_fields:
            escalations.append(
                ProposalRiskEscalation(
                    category=ProposalRiskCategory.RUNTIME,
                    detail=(
                        "Runtime requirements widened "
                        f"({', '.join(sorted(widened_fields))}): "
                        f"{before.runtime_requirements.model_dump(mode='json')} "
                        f"-> {after.runtime_requirements.model_dump(mode='json')}."
                    ),
                )
            )

    if before.model_deployment != after.model_deployment:
        escalations.append(
            ProposalRiskEscalation(
                category=ProposalRiskCategory.MODEL,
                detail=(
                    f"Declared model deployment changed: "
                    f"{before.model_deployment.model_dump(mode='json') if before.model_deployment else None} "
                    f"-> {after.model_deployment.model_dump(mode='json') if after.model_deployment else None}."
                ),
            )
        )

    return tuple(escalations)


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------


class BuilderService:
    """Propose/apply/reject/list stored manifest-change proposals.

    Every mutating method takes an explicit ``actor_role`` (resolved by the
    caller from ownership + identity, matching ``ReleaseService``'s
    convention) and enforces role-based authorization before doing anything.
    """

    def __init__(
        self,
        store: AgentStudioStore,
        generator: ManifestProposalGenerator,
        bundle_store: ArtifactBundleStore,
        release_service: ReleaseService,
    ) -> None:
        self._store = store
        self._generator = generator
        self._bundle_store = bundle_store
        self._release_service = release_service

    @staticmethod
    def _require_role(actor_role: AgentRole, minimum: AgentRole) -> None:
        if not role_at_least(actor_role, minimum):
            raise AuthorizationError(f"Role '{actor_role.value}' does not meet the minimum '{minimum.value}'.")

    def propose(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        message: str,
        base_etag: str,
        requested_by: str,
        actor_role: AgentRole,
    ) -> BuilderProposal:
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        draft = self._store.get_draft(scope, logical_agent_id)
        if draft is None:
            raise BuilderNotFoundError(f"Agent '{logical_agent_id}' has no draft to propose changes against.")
        if draft.etag != base_etag:
            raise BuilderConcurrencyError(
                f"base_etag '{base_etag}' does not match the current draft etag; refresh and retry."
            )
        result = self._generator.propose(manifest=draft.manifest, message=message)
        after_manifest = result.after_manifest
        if after_manifest.logical_agent_id != logical_agent_id or after_manifest.tenant_id != tenant_id:
            raise BuilderServiceError("Generated manifest logical_agent_id/tenant_id must match the target agent.")
        source_bundle_ref: str | None = None
        if result.source_bundle_content is not None:
            stored = self._bundle_store.put(
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=logical_agent_id,
                content=result.source_bundle_content,
                version_label=draft_version_label(draft.etag),
            )
            source_bundle_ref = stored.uri
        capability_changes = diff_capability_bindings(draft.manifest, after_manifest)
        proposal = BuilderProposal(
            id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            logical_agent_id=logical_agent_id,
            draft_base_etag=draft.etag,
            before_manifest=draft.manifest,
            after_manifest=after_manifest,
            before_manifest_hash=manifest_hash(draft.manifest),
            after_manifest_hash=manifest_hash(after_manifest),
            changes=diff_manifest_fields(draft.manifest, after_manifest),
            capability_changes=capability_changes,
            risk_escalations=classify_risk_escalations(draft.manifest, after_manifest, capability_changes),
            validation_warnings=result.validation_warnings,
            source_bundle_ref=source_bundle_ref,
            provenance=BuilderProvenance(
                generator=result.generator,
                generator_version=result.generator_version,
                message=message,
                requested_by=requested_by,
            ),
        )
        return self._store.create_builder_proposal(scope, proposal)

    def list_proposals(self, tenant_id: str, project_id: str, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        return self._store.list_builder_proposals(scope, logical_agent_id)

    def get_proposal(
        self, tenant_id: str, project_id: str, logical_agent_id: str, proposal_id: str
    ) -> BuilderProposal | None:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        proposal = self._store.get_builder_proposal(scope, proposal_id)
        if proposal is None or proposal.logical_agent_id != logical_agent_id:
            return None
        return proposal

    def _require_pending(
        self, tenant_id: str, project_id: str, logical_agent_id: str, proposal_id: str
    ) -> BuilderProposal:
        proposal = self.get_proposal(tenant_id, project_id, logical_agent_id, proposal_id)
        if proposal is None:
            raise BuilderNotFoundError(f"Proposal '{proposal_id}' was not found.")
        if proposal.state != BuilderProposalState.PENDING:
            raise BuilderServiceError(f"Proposal '{proposal_id}' has already been decided.")
        return proposal

    def apply(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        proposal_id: str,
        base_etag: str,
        applied_by: str,
        actor_role: AgentRole,
    ) -> AgentDraft:
        """Apply an already-validated, already-stored proposal's manifest.

        Never accepts a patch body: only ``base_etag`` is client-supplied.
        Two-part concurrency check: the caller's view of the draft must be
        current (``base_etag == draft.etag``) *and* the draft must be
        unchanged since the proposal was generated (``draft.etag ==
        proposal.draft_base_etag``) -- otherwise the proposal is stale even
        if the caller's ``base_etag`` happens to match the current draft.

        The actual write is delegated to ``ReleaseService.update_draft`` --
        never a raw store write -- so the same capability-binding freshness/
        provider-pin-drift revalidation every other draft save goes through
        also re-runs here. A capability instance can drift (reconfigured,
        deregistered, or gone non-GA/non-ACTIVE) in the window between
        ``propose()`` generating a candidate manifest and a reviewer calling
        ``apply()``; without this delegation a stale/fabricated binding
        captured at propose-time could otherwise be silently carried into
        the draft.
        """
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        proposal = self._require_pending(tenant_id, project_id, logical_agent_id, proposal_id)
        draft = self._store.get_draft(scope, logical_agent_id)
        if draft is None:
            raise BuilderNotFoundError(f"Agent '{logical_agent_id}' has no draft to apply this proposal to.")
        if draft.etag != base_etag:
            raise BuilderConcurrencyError(
                f"base_etag '{base_etag}' does not match the current draft etag; refresh and retry."
            )
        if draft.etag != proposal.draft_base_etag:
            raise BuilderConcurrencyError(
                f"Proposal '{proposal_id}' is stale: the draft changed since it was generated; regenerate it."
            )
        try:
            updated_draft = self._release_service.update_draft(
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=logical_agent_id,
                manifest=proposal.after_manifest,
                updated_by=applied_by,
                actor_role=actor_role,
                expected_etag=base_etag,
            )
        except ReleaseDraftConflictError as exc:
            raise BuilderConcurrencyError(
                f"base_etag '{base_etag}' no longer matches the current draft etag; refresh and retry."
            ) from exc
        except ReleaseServiceError as exc:
            raise BuilderServiceError(
                f"Applying proposal '{proposal_id}' failed manifest/capability-binding revalidation: {exc}"
            ) from exc
        decided = proposal.model_copy(
            update={
                "state": BuilderProposalState.APPLIED,
                "decided_by": applied_by,
                "decided_at": utc_now(),
                "applied_draft_etag": updated_draft.etag,
            }
        )
        self._store.save_builder_proposal_decision(scope, decided)
        return updated_draft

    def reject(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        proposal_id: str,
        rejected_by: str,
        reason: str,
        actor_role: AgentRole,
    ) -> BuilderProposal:
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        proposal = self._require_pending(tenant_id, project_id, logical_agent_id, proposal_id)
        decided = proposal.model_copy(
            update={
                "state": BuilderProposalState.REJECTED,
                "decided_by": rejected_by,
                "decided_at": utc_now(),
                "rejection_reason": reason,
            }
        )
        return self._store.save_builder_proposal_decision(scope, decided)
