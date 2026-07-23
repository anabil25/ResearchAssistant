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

from research_assistant_api.agent_studio.artifact_bundle_store import ArtifactBundleStore
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentRole,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    CapabilityBinding,
    CapabilityChangeKind,
    CapabilityChangeSummary,
    ManifestChangeSummary,
    ManifestFieldChangeKind,
    role_at_least,
    utc_now,
)
from research_assistant_api.agent_studio.release_service import AuthorizationError, manifest_hash
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


def diff_capability_bindings(
    before: AgentManifest, after: AgentManifest
) -> tuple[CapabilityChangeSummary, ...]:
    """Deterministic capability-binding diff, keyed by (descriptor_id, operation)."""

    def _key(binding: CapabilityBinding) -> tuple[str, str]:
        return (binding.descriptor_id, binding.operation)

    before_by_key = {_key(binding): binding for binding in before.capabilities}
    after_by_key = {_key(binding): binding for binding in after.capabilities}
    changes: list[CapabilityChangeSummary] = []
    for key in sorted(set(before_by_key) | set(after_by_key)):
        before_binding = before_by_key.get(key)
        after_binding = after_by_key.get(key)
        if before_binding == after_binding:
            continue
        descriptor_id, operation = key
        if before_binding is None:
            kind = CapabilityChangeKind.ATTACHED
        elif after_binding is None:
            kind = CapabilityChangeKind.DETACHED
        else:
            kind = CapabilityChangeKind.RECONFIGURED
        changes.append(
            CapabilityChangeSummary(
                descriptor_id=descriptor_id,
                operation=operation,
                kind=kind,
                before=before_binding,
                after=after_binding,
            )
        )
    return tuple(changes)


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
    ) -> None:
        self._store = store
        self._generator = generator
        self._bundle_store = bundle_store

    @staticmethod
    def _require_role(actor_role: AgentRole, minimum: AgentRole) -> None:
        if not role_at_least(actor_role, minimum):
            raise AuthorizationError(f"Role '{actor_role.value}' does not meet the minimum '{minimum.value}'.")

    def propose(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        message: str,
        base_etag: str,
        requested_by: str,
        actor_role: AgentRole,
    ) -> BuilderProposal:
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        draft = self._store.get_draft(tenant_id, logical_agent_id)
        if draft is None:
            raise BuilderNotFoundError(f"Agent '{logical_agent_id}' has no draft to propose changes against.")
        if draft.etag != base_etag:
            raise BuilderConcurrencyError(
                f"base_etag '{base_etag}' does not match the current draft etag; refresh and retry."
            )
        result = self._generator.propose(manifest=draft.manifest, message=message)
        after_manifest = result.after_manifest
        if after_manifest.logical_agent_id != logical_agent_id or after_manifest.tenant_id != tenant_id:
            raise BuilderServiceError(
                "Generated manifest logical_agent_id/tenant_id must match the target agent."
            )
        source_bundle_ref: str | None = None
        if result.source_bundle_content is not None:
            stored = self._bundle_store.put(
                tenant_id=tenant_id,
                logical_agent_id=logical_agent_id,
                content=result.source_bundle_content,
            )
            source_bundle_ref = stored.uri
        proposal = BuilderProposal(
            id=str(uuid4()),
            tenant_id=tenant_id,
            logical_agent_id=logical_agent_id,
            draft_base_etag=draft.etag,
            before_manifest=draft.manifest,
            after_manifest=after_manifest,
            before_manifest_hash=manifest_hash(draft.manifest),
            after_manifest_hash=manifest_hash(after_manifest),
            changes=diff_manifest_fields(draft.manifest, after_manifest),
            capability_changes=diff_capability_bindings(draft.manifest, after_manifest),
            validation_warnings=result.validation_warnings,
            source_bundle_ref=source_bundle_ref,
            provenance=BuilderProvenance(
                generator=result.generator,
                generator_version=result.generator_version,
                message=message,
                requested_by=requested_by,
            ),
        )
        return self._store.create_builder_proposal(proposal)

    def list_proposals(self, tenant_id: str, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        return self._store.list_builder_proposals(tenant_id, logical_agent_id)

    def get_proposal(self, tenant_id: str, logical_agent_id: str, proposal_id: str) -> BuilderProposal | None:
        proposal = self._store.get_builder_proposal(tenant_id, proposal_id)
        if proposal is None or proposal.logical_agent_id != logical_agent_id:
            return None
        return proposal

    def _require_pending(self, tenant_id: str, logical_agent_id: str, proposal_id: str) -> BuilderProposal:
        proposal = self.get_proposal(tenant_id, logical_agent_id, proposal_id)
        if proposal is None:
            raise BuilderNotFoundError(f"Proposal '{proposal_id}' was not found.")
        if proposal.state != BuilderProposalState.PENDING:
            raise BuilderServiceError(f"Proposal '{proposal_id}' has already been decided.")
        return proposal

    def apply(
        self,
        *,
        tenant_id: str,
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
        """
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        proposal = self._require_pending(tenant_id, logical_agent_id, proposal_id)
        draft = self._store.get_draft(tenant_id, logical_agent_id)
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
        new_etag = str(uuid4())
        updated_draft = draft.model_copy(
            update={
                "manifest": proposal.after_manifest,
                "updated_by": applied_by,
                "updated_at": utc_now(),
                "etag": new_etag,
            }
        )
        self._store.save_draft(updated_draft)
        decided = proposal.model_copy(
            update={
                "state": BuilderProposalState.APPLIED,
                "decided_by": applied_by,
                "decided_at": utc_now(),
                "applied_draft_etag": new_etag,
            }
        )
        self._store.save_builder_proposal_decision(decided)
        return updated_draft

    def reject(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        proposal_id: str,
        rejected_by: str,
        reason: str,
        actor_role: AgentRole,
    ) -> BuilderProposal:
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        proposal = self._require_pending(tenant_id, logical_agent_id, proposal_id)
        decided = proposal.model_copy(
            update={
                "state": BuilderProposalState.REJECTED,
                "decided_by": rejected_by,
                "decided_at": utc_now(),
                "rejection_reason": reason,
            }
        )
        return self._store.save_builder_proposal_decision(decided)
