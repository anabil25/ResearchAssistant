"""Metadata store for the Agent Studio platform.

``AgentStudioStore`` is the in-memory base implementation (used directly in
tests and as the superclass ``CosmosAgentStudioStore`` overrides). Every
read/write method takes an explicit, non-optional ``ScopeContext`` (never a
bare ``tenant_id``) and partitions strictly on ``scope.scope_key``, so
cross-tenant *and* cross-project access are structurally impossible rather
than merely policy-enforced. System-owned agents are still scope-partitioned,
under ``scope.PLATFORM_PROJECT_ID`` by convention (see ``scope.py``); sharing
across tenants/projects is an explicit higher-level concern, never a
store-level default.

Every record's own ``(tenant_id, project_id)`` is validated against the
``ScopeContext`` used to address it on writes (``_require_scope_match``), and
point reads/lookups-by-id treat a scope mismatch identically to "not found" —
this store never leaks the *existence* of a record outside its scope, only
whether operations succeed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalState,
    BuilderProposal,
    BuilderProposalState,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    StudioApprovalRecord,
    ToolRegistrationSpec,
    role_at_least,
)
from research_assistant_api.agent_studio.scope import ScopeContext

_ROLE_PRECEDENCE: tuple[AgentRole, ...] = (
    AgentRole.OWNER,
    AgentRole.MAINTAINER,
    AgentRole.CONTRIBUTOR,
    AgentRole.VIEWER,
)


class AgentStudioStoreError(RuntimeError):
    pass


class DraftConflictError(AgentStudioStoreError):
    """Raised when a draft save's ``expected_etag`` no longer matches the

    currently stored draft's ``etag`` -- another writer saved a change in
    between the caller's read and this write. Callers must re-fetch the
    latest draft and retry rather than silently clobbering the concurrent
    edit.
    """


class AgentStudioStore:
    """In-memory Agent Studio metadata store."""

    persistence = "in-memory"

    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], AgentDraft] = {}
        self._ownership: dict[tuple[str, str], list[OwnershipGrant]] = {}
        self._versions: dict[str, AgentVersion] = {}
        self._versions_by_agent: dict[tuple[str, str], list[str]] = {}
        self._version_lock = threading.Lock()
        self._lineage: list[LineageEdge] = []
        self._gate_reports: dict[tuple[str, str], ReleaseGateReport] = {}
        self._releases: dict[str, AgentRelease] = {}
        self._releases_by_version: dict[str, list[str]] = {}
        self._approvals: dict[str, StudioApprovalRecord] = {}
        self._deployments: dict[str, DeploymentRecord] = {}
        self._deployments_by_agent: dict[tuple[str, str], list[str]] = {}
        self._bindings: dict[tuple[str, str, str], LogicalAgentBinding] = {}
        self._tool_registrations: dict[str, ToolRegistrationSpec] = {}
        self._tool_registrations_by_agent: dict[tuple[str, str], list[str]] = {}
        self._builder_proposals: dict[str, BuilderProposal] = {}
        self._builder_proposals_by_agent: dict[tuple[str, str], list[str]] = {}

    @staticmethod
    def _require_scope_match(scope: ScopeContext, tenant_id: str, project_id: str) -> None:
        """Defense-in-depth: reject persisting a record under a ``ScopeContext``
        whose ``(tenant_id, project_id)`` disagrees with the record's own
        declared scope fields. A caller-side bug that constructs a record for
        one project but addresses the store with another project's
        ``ScopeContext`` fails loudly here instead of silently mis-filing the
        record into the wrong partition."""
        if tenant_id != scope.tenant_id or project_id != scope.project_id:
            raise AgentStudioStoreError(
                f"Record scope (tenant={tenant_id!r}, project={project_id!r}) does not match "
                f"requested scope (tenant={scope.tenant_id!r}, project={scope.project_id!r})."
            )

    # -- Drafts -----------------------------------------------------------

    def save_draft(self, scope: ScopeContext, draft: AgentDraft, *, expected_etag: str | None = None) -> AgentDraft:
        self._require_scope_match(scope, draft.tenant_id, draft.project_id)
        key = (scope.scope_key, draft.logical_agent_id)
        if expected_etag is not None:
            current = self._drafts.get(key)
            if current is None or current.etag != expected_etag:
                raise DraftConflictError(
                    f"Draft '{draft.logical_agent_id}' was modified concurrently; the supplied "
                    "etag no longer matches the stored draft. Re-fetch and retry."
                )
        self._drafts[key] = draft
        return draft

    def get_draft(self, scope: ScopeContext, logical_agent_id: str) -> AgentDraft | None:
        return self._drafts.get((scope.scope_key, logical_agent_id))

    def list_drafts(self, scope: ScopeContext) -> tuple[AgentDraft, ...]:
        return tuple(draft for (key, _), draft in self._drafts.items() if key == scope.scope_key)

    # -- Ownership ----------------------------------------------------------

    def grant_ownership(self, scope: ScopeContext, grant: OwnershipGrant) -> OwnershipGrant:
        self._require_scope_match(scope, grant.tenant_id, grant.project_id)
        self._ownership.setdefault((scope.scope_key, grant.logical_agent_id), []).append(grant)
        return grant

    def role_for(self, scope: ScopeContext, logical_agent_id: str, principal_id: str) -> AgentRole | None:
        """Resolve ``principal_id``'s effective role on this agent within ``scope``.

        Only grants whose own ``(tenant_id, project_id)`` matches ``scope``
        exactly are considered — a grant scoped to a different project (even
        within the same tenant) never counts, closing the cross-project
        privilege-leak this store previously tolerated via an optional
        ``project_id``.
        """
        grants = self._ownership.get((scope.scope_key, logical_agent_id), [])
        roles = {grant.role for grant in grants if grant.principal_id == principal_id}
        if not roles:
            return None
        for candidate in _ROLE_PRECEDENCE:
            if candidate in roles:
                return candidate
        return None

    def list_ownership(self, scope: ScopeContext, logical_agent_id: str) -> tuple[OwnershipGrant, ...]:
        return tuple(self._ownership.get((scope.scope_key, logical_agent_id), []))

    # -- Versions -------------------------------------------------------

    def next_sequence(self, scope: ScopeContext, logical_agent_id: str) -> int:
        """Advisory next-sequence read (e.g. for UI/preview display).

        Not atomic on its own when called separately from persistence — use
        ``allocate_version`` to actually cut a version, which holds a single
        lock across sequence computation *and* persistence.
        """
        return len(self._versions_by_agent.get((scope.scope_key, logical_agent_id), [])) + 1

    def allocate_version(
        self,
        scope: ScopeContext,
        logical_agent_id: str,
        builder: Callable[[int], AgentVersion],
    ) -> AgentVersion:
        """Atomically reserve the next sequence number and persist the version.

        ``builder`` is invoked with the reserved sequence *inside* the lock
        and must return a fully-constructed, immutable ``AgentVersion`` with
        that exact sequence. This closes the TOCTOU gap a separate
        ``next_sequence()`` + ``create_version()`` call pair would have: two
        concurrent cuts for the same agent can never be assigned the same
        sequence number.
        """
        with self._version_lock:
            key = (scope.scope_key, logical_agent_id)
            sequence = len(self._versions_by_agent.get(key, [])) + 1
            version = builder(sequence)
            self._require_scope_match(scope, version.tenant_id, version.project_id)
            if version.sequence != sequence:
                raise AgentStudioStoreError(
                    f"Builder returned sequence {version.sequence}, expected atomically-reserved {sequence}."
                )
            if version.id in self._versions:
                raise AgentStudioStoreError(f"Version '{version.id}' already exists; versions are immutable.")
            self._versions[version.id] = version
            self._versions_by_agent.setdefault(key, []).append(version.id)
            return version

    def create_version(self, scope: ScopeContext, version: AgentVersion) -> AgentVersion:
        """Direct persistence path for an already sequence-assigned version.

        Used by tests, by ``CosmosAgentStudioStore`` read-through caching,
        and by any migration/backfill code that already has a fully formed
        ``AgentVersion``. Real cut flows should prefer ``allocate_version``
        for atomic sequence allocation.
        """
        self._require_scope_match(scope, version.tenant_id, version.project_id)
        with self._version_lock:
            if version.id in self._versions:
                raise AgentStudioStoreError(f"Version '{version.id}' already exists; versions are immutable.")
            self._versions[version.id] = version
            self._versions_by_agent.setdefault((scope.scope_key, version.logical_agent_id), []).append(version.id)
            return version

    def get_version(self, scope: ScopeContext, version_id: str) -> AgentVersion | None:
        version = self._versions.get(version_id)
        if version is None or version.tenant_id != scope.tenant_id or version.project_id != scope.project_id:
            return None
        return version

    def list_versions(self, scope: ScopeContext, logical_agent_id: str) -> tuple[AgentVersion, ...]:
        ids = self._versions_by_agent.get((scope.scope_key, logical_agent_id), [])
        return tuple(self._versions[version_id] for version_id in ids)

    # -- Lineage --------------------------------------------------------

    def add_lineage_edge(self, scope: ScopeContext, edge: LineageEdge) -> LineageEdge:
        self._require_scope_match(scope, edge.tenant_id, edge.project_id)
        self._lineage.append(edge)
        return edge

    def list_lineage(self, scope: ScopeContext, logical_agent_id: str) -> tuple[LineageEdge, ...]:
        return tuple(
            edge
            for edge in self._lineage
            if edge.tenant_id == scope.tenant_id
            and edge.project_id == scope.project_id
            and (edge.child_logical_agent_id == logical_agent_id or edge.parent_logical_agent_id == logical_agent_id)
        )

    # -- Gate reports -----------------------------------------------------
    # Gate reports are project-scoped exactly like every other document:
    # partitioned by ``scope.scope_key`` and validated against the report's
    # own declared ``tenant_id``/``project_id`` on write, so a report can
    # never be filed under (or later read back from) the wrong partition.

    def save_gate_report(self, scope: ScopeContext, report: ReleaseGateReport) -> ReleaseGateReport:
        self._require_scope_match(scope, report.tenant_id, report.project_id)
        self._gate_reports[(scope.scope_key, report.id)] = report
        return report

    def get_gate_report(self, scope: ScopeContext, report_id: str) -> ReleaseGateReport | None:
        return self._gate_reports.get((scope.scope_key, report_id))

    # -- Releases (append-only lifecycle for an immutable version) ---------

    def create_release(self, scope: ScopeContext, release: AgentRelease) -> AgentRelease:
        """Append a new lifecycle transition for a version.

        Never mutates an existing ``AgentRelease``; every transition
        (gated/approved/active/deprecated/rolled_back) is a brand-new record
        chained via ``previous_release_id``.
        """
        self._require_scope_match(scope, release.tenant_id, release.project_id)
        self._releases[release.id] = release
        self._releases_by_version.setdefault(release.version_id, []).append(release.id)
        return release

    def get_release(self, scope: ScopeContext, release_id: str) -> AgentRelease | None:
        release = self._releases.get(release_id)
        if release is None or release.tenant_id != scope.tenant_id or release.project_id != scope.project_id:
            return None
        return release

    def list_releases_for_version(self, scope: ScopeContext, version_id: str) -> tuple[AgentRelease, ...]:
        ids = self._releases_by_version.get(version_id, [])
        return tuple(
            release
            for release in (self._releases[i] for i in ids)
            if release.tenant_id == scope.tenant_id and release.project_id == scope.project_id
        )

    def latest_release_for_version(self, scope: ScopeContext, version_id: str) -> AgentRelease | None:
        """The most recent lifecycle transition for a version, or ``None``
        if the version has never had a release cut (i.e. it has not yet
        passed hard gates)."""
        releases = self.list_releases_for_version(scope, version_id)
        return releases[-1] if releases else None

    # -- Approvals ------------------------------------------------------

    def find_pending_approval(self, scope: ScopeContext, idempotency_key: str) -> StudioApprovalRecord | None:
        return next(
            (
                approval
                for approval in self._approvals.values()
                if approval.tenant_id == scope.tenant_id
                and approval.project_id == scope.project_id
                and approval.idempotency_key == idempotency_key
                and approval.state == ApprovalState.PENDING
            ),
            None,
        )

    def create_approval(self, scope: ScopeContext, record: StudioApprovalRecord) -> StudioApprovalRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        existing = self.find_pending_approval(scope, record.idempotency_key)
        if existing is not None:
            return existing
        self._approvals[record.id] = record
        return record

    def get_approval(self, scope: ScopeContext, approval_id: str) -> StudioApprovalRecord | None:
        record = self._approvals.get(approval_id)
        if record is None or record.tenant_id != scope.tenant_id or record.project_id != scope.project_id:
            return None
        return record

    def save_approval_decision(self, scope: ScopeContext, record: StudioApprovalRecord) -> StudioApprovalRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        current = self._approvals.get(record.id)
        if current is None:
            raise AgentStudioStoreError(f"Approval '{record.id}' not found.")
        if current.state != ApprovalState.PENDING:
            raise AgentStudioStoreError(f"Approval '{record.id}' has already been decided.")
        self._approvals[record.id] = record
        return record

    def list_approvals(self, scope: ScopeContext, version_id: str | None = None) -> tuple[StudioApprovalRecord, ...]:
        return tuple(
            approval
            for approval in self._approvals.values()
            if approval.tenant_id == scope.tenant_id
            and approval.project_id == scope.project_id
            and (version_id is None or approval.version_id == version_id)
        )

    # -- Deployments ------------------------------------------------------

    def create_deployment(self, scope: ScopeContext, record: DeploymentRecord) -> DeploymentRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        self._deployments[record.id] = record
        self._deployments_by_agent.setdefault((scope.scope_key, record.logical_agent_id), []).append(record.id)
        return record

    def get_deployment(self, scope: ScopeContext, deployment_id: str) -> DeploymentRecord | None:
        record = self._deployments.get(deployment_id)
        if record is None or record.tenant_id != scope.tenant_id or record.project_id != scope.project_id:
            return None
        return record

    def list_deployments(self, scope: ScopeContext, logical_agent_id: str) -> tuple[DeploymentRecord, ...]:
        ids = self._deployments_by_agent.get((scope.scope_key, logical_agent_id), [])
        return tuple(self._deployments[deployment_id] for deployment_id in ids)

    def update_deployment(self, scope: ScopeContext, record: DeploymentRecord) -> DeploymentRecord:
        self._require_scope_match(scope, record.tenant_id, record.project_id)
        if record.id not in self._deployments:
            raise AgentStudioStoreError(f"Deployment '{record.id}' not found.")
        self._deployments[record.id] = record
        return record

    # -- Logical agent bindings ----------------------------------------

    def set_binding(self, scope: ScopeContext, binding: LogicalAgentBinding) -> LogicalAgentBinding:
        self._require_scope_match(scope, binding.tenant_id, binding.project_id)
        self._bindings[(scope.scope_key, binding.logical_agent_id, binding.environment.value)] = binding
        return binding

    def get_binding(
        self,
        scope: ScopeContext,
        logical_agent_id: str,
        environment: DeploymentEnvironment,
    ) -> LogicalAgentBinding | None:
        return self._bindings.get((scope.scope_key, logical_agent_id, environment.value))

    # -- Tool registrations (runtime handler wiring) -----------------------

    def create_tool_registration(
        self, scope: ScopeContext, registration: ToolRegistrationSpec
    ) -> ToolRegistrationSpec:
        self._require_scope_match(scope, registration.tenant_id, registration.project_id)
        self._tool_registrations[registration.id] = registration
        self._tool_registrations_by_agent.setdefault(
            (scope.scope_key, registration.logical_agent_id), []
        ).append(registration.id)
        return registration

    def list_tool_registrations(self, scope: ScopeContext, logical_agent_id: str) -> tuple[ToolRegistrationSpec, ...]:
        ids = self._tool_registrations_by_agent.get((scope.scope_key, logical_agent_id), [])
        return tuple(self._tool_registrations[registration_id] for registration_id in ids)

    # -- Builder proposals --------------------------------------------------

    def create_builder_proposal(self, scope: ScopeContext, proposal: BuilderProposal) -> BuilderProposal:
        self._require_scope_match(scope, proposal.tenant_id, proposal.project_id)
        self._builder_proposals[proposal.id] = proposal
        self._builder_proposals_by_agent.setdefault(
            (scope.scope_key, proposal.logical_agent_id), []
        ).append(proposal.id)
        return proposal

    def get_builder_proposal(self, scope: ScopeContext, proposal_id: str) -> BuilderProposal | None:
        proposal = self._builder_proposals.get(proposal_id)
        if proposal is None or proposal.tenant_id != scope.tenant_id or proposal.project_id != scope.project_id:
            return None
        return proposal

    def list_builder_proposals(self, scope: ScopeContext, logical_agent_id: str) -> tuple[BuilderProposal, ...]:
        ids = self._builder_proposals_by_agent.get((scope.scope_key, logical_agent_id), [])
        return tuple(self._builder_proposals[proposal_id] for proposal_id in ids)

    def save_builder_proposal_decision(self, scope: ScopeContext, proposal: BuilderProposal) -> BuilderProposal:
        self._require_scope_match(scope, proposal.tenant_id, proposal.project_id)
        current = self._builder_proposals.get(proposal.id)
        if current is None:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' not found.")
        if current.state != BuilderProposalState.PENDING:
            raise AgentStudioStoreError(f"Proposal '{proposal.id}' has already been decided.")
        self._builder_proposals[proposal.id] = proposal
        return proposal


__all__ = [
    "AgentStudioStore",
    "AgentStudioStoreError",
    "role_at_least",
]
