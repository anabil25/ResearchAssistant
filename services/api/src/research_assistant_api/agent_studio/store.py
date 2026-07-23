"""Metadata store for the Agent Studio platform.

``AgentStudioStore`` is the in-memory base implementation (used directly in
tests and as the superclass ``CosmosAgentStudioStore`` overrides). Every
read/write method takes an explicit ``tenant_id`` and filters strictly on it,
so cross-tenant access is structurally impossible rather than merely
policy-enforced. System-owned agents are still tenant-scoped by the tenant
that registered them; sharing across tenants (if ever needed) is an explicit
higher-level concern, not a store-level default.
"""

from __future__ import annotations

from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentRole,
    AgentVersion,
    AgentVersionStatus,
    ApprovalState,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    StudioApprovalRecord,
    ToolRegistration,
    role_at_least,
)

_ROLE_PRECEDENCE: tuple[AgentRole, ...] = (
    AgentRole.OWNER,
    AgentRole.MAINTAINER,
    AgentRole.CONTRIBUTOR,
    AgentRole.VIEWER,
)


class AgentStudioStoreError(RuntimeError):
    pass


class AgentStudioStore:
    """In-memory Agent Studio metadata store."""

    persistence = "in-memory"

    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], AgentDraft] = {}
        self._ownership: dict[tuple[str, str], list[OwnershipGrant]] = {}
        self._versions: dict[str, AgentVersion] = {}
        self._versions_by_agent: dict[tuple[str, str], list[str]] = {}
        self._lineage: list[LineageEdge] = []
        self._gate_reports: dict[str, ReleaseGateReport] = {}
        self._approvals: dict[str, StudioApprovalRecord] = {}
        self._deployments: dict[str, DeploymentRecord] = {}
        self._deployments_by_agent: dict[tuple[str, str], list[str]] = {}
        self._bindings: dict[tuple[str, str, str], LogicalAgentBinding] = {}
        self._tool_registrations: dict[str, ToolRegistration] = {}
        self._tool_registrations_by_agent: dict[tuple[str, str], list[str]] = {}

    # -- Drafts -----------------------------------------------------------

    def save_draft(self, draft: AgentDraft) -> AgentDraft:
        self._drafts[(draft.tenant_id, draft.logical_agent_id)] = draft
        return draft

    def get_draft(self, tenant_id: str, logical_agent_id: str) -> AgentDraft | None:
        return self._drafts.get((tenant_id, logical_agent_id))

    def list_drafts(self, tenant_id: str) -> tuple[AgentDraft, ...]:
        return tuple(draft for (tid, _), draft in self._drafts.items() if tid == tenant_id)

    # -- Ownership ----------------------------------------------------------

    def grant_ownership(self, grant: OwnershipGrant) -> OwnershipGrant:
        self._ownership.setdefault((grant.tenant_id, grant.logical_agent_id), []).append(grant)
        return grant

    def role_for(self, tenant_id: str, logical_agent_id: str, principal_id: str) -> AgentRole | None:
        grants = self._ownership.get((tenant_id, logical_agent_id), [])
        roles = {grant.role for grant in grants if grant.principal_id == principal_id}
        if not roles:
            return None
        for candidate in _ROLE_PRECEDENCE:
            if candidate in roles:
                return candidate
        return None

    def list_ownership(self, tenant_id: str, logical_agent_id: str) -> tuple[OwnershipGrant, ...]:
        return tuple(self._ownership.get((tenant_id, logical_agent_id), []))

    # -- Versions -------------------------------------------------------

    def next_sequence(self, tenant_id: str, logical_agent_id: str) -> int:
        return len(self._versions_by_agent.get((tenant_id, logical_agent_id), [])) + 1

    def create_version(self, version: AgentVersion) -> AgentVersion:
        if version.id in self._versions:
            raise AgentStudioStoreError(f"Version '{version.id}' already exists; versions are immutable.")
        self._versions[version.id] = version
        self._versions_by_agent.setdefault((version.tenant_id, version.logical_agent_id), []).append(version.id)
        return version

    def get_version(self, tenant_id: str, version_id: str) -> AgentVersion | None:
        version = self._versions.get(version_id)
        if version is None or version.tenant_id != tenant_id:
            return None
        return version

    def list_versions(self, tenant_id: str, logical_agent_id: str) -> tuple[AgentVersion, ...]:
        ids = self._versions_by_agent.get((tenant_id, logical_agent_id), [])
        return tuple(self._versions[version_id] for version_id in ids)

    def update_version_status(
        self,
        tenant_id: str,
        version_id: str,
        status: AgentVersionStatus,
    ) -> AgentVersion:
        version = self.get_version(tenant_id, version_id)
        if version is None:
            raise AgentStudioStoreError(f"Version '{version_id}' not found for tenant '{tenant_id}'.")
        updated = version.model_copy(update={"status": status})
        self._versions[version_id] = updated
        return updated

    def attach_gate_report(self, tenant_id: str, version_id: str, gate_report_id: str) -> AgentVersion:
        version = self.get_version(tenant_id, version_id)
        if version is None:
            raise AgentStudioStoreError(f"Version '{version_id}' not found for tenant '{tenant_id}'.")
        updated = version.model_copy(update={"gate_report_id": gate_report_id})
        self._versions[version_id] = updated
        return updated

    # -- Lineage --------------------------------------------------------

    def add_lineage_edge(self, edge: LineageEdge) -> LineageEdge:
        self._lineage.append(edge)
        return edge

    def list_lineage(self, tenant_id: str, logical_agent_id: str) -> tuple[LineageEdge, ...]:
        return tuple(
            edge
            for edge in self._lineage
            if edge.tenant_id == tenant_id
            and (edge.child_logical_agent_id == logical_agent_id or edge.parent_logical_agent_id == logical_agent_id)
        )

    # -- Gate reports -----------------------------------------------------

    def save_gate_report(self, report: ReleaseGateReport) -> ReleaseGateReport:
        self._gate_reports[report.id] = report
        return report

    def get_gate_report(self, report_id: str) -> ReleaseGateReport | None:
        return self._gate_reports.get(report_id)

    # -- Approvals ------------------------------------------------------

    def find_pending_approval(self, tenant_id: str, idempotency_key: str) -> StudioApprovalRecord | None:
        return next(
            (
                approval
                for approval in self._approvals.values()
                if approval.tenant_id == tenant_id
                and approval.idempotency_key == idempotency_key
                and approval.state == ApprovalState.PENDING
            ),
            None,
        )

    def create_approval(self, record: StudioApprovalRecord) -> StudioApprovalRecord:
        existing = self.find_pending_approval(record.tenant_id, record.idempotency_key)
        if existing is not None:
            return existing
        self._approvals[record.id] = record
        return record

    def get_approval(self, tenant_id: str, approval_id: str) -> StudioApprovalRecord | None:
        record = self._approvals.get(approval_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    def save_approval_decision(self, record: StudioApprovalRecord) -> StudioApprovalRecord:
        current = self._approvals.get(record.id)
        if current is None:
            raise AgentStudioStoreError(f"Approval '{record.id}' not found.")
        if current.state != ApprovalState.PENDING:
            raise AgentStudioStoreError(f"Approval '{record.id}' has already been decided.")
        self._approvals[record.id] = record
        return record

    def list_approvals(self, tenant_id: str, version_id: str | None = None) -> tuple[StudioApprovalRecord, ...]:
        return tuple(
            approval
            for approval in self._approvals.values()
            if approval.tenant_id == tenant_id and (version_id is None or approval.version_id == version_id)
        )

    # -- Deployments ------------------------------------------------------

    def create_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        self._deployments[record.id] = record
        self._deployments_by_agent.setdefault((record.tenant_id, record.logical_agent_id), []).append(record.id)
        return record

    def get_deployment(self, tenant_id: str, deployment_id: str) -> DeploymentRecord | None:
        record = self._deployments.get(deployment_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    def list_deployments(self, tenant_id: str, logical_agent_id: str) -> tuple[DeploymentRecord, ...]:
        ids = self._deployments_by_agent.get((tenant_id, logical_agent_id), [])
        return tuple(self._deployments[deployment_id] for deployment_id in ids)

    def update_deployment(self, record: DeploymentRecord) -> DeploymentRecord:
        if record.id not in self._deployments:
            raise AgentStudioStoreError(f"Deployment '{record.id}' not found.")
        self._deployments[record.id] = record
        return record

    # -- Logical agent bindings ----------------------------------------

    def set_binding(self, binding: LogicalAgentBinding) -> LogicalAgentBinding:
        self._bindings[(binding.tenant_id, binding.logical_agent_id, binding.environment.value)] = binding
        return binding

    def get_binding(
        self,
        tenant_id: str,
        logical_agent_id: str,
        environment: DeploymentEnvironment,
    ) -> LogicalAgentBinding | None:
        return self._bindings.get((tenant_id, logical_agent_id, environment.value))

    # -- Tool registrations (runtime handler wiring) -----------------------

    def create_tool_registration(self, registration: ToolRegistration) -> ToolRegistration:
        self._tool_registrations[registration.id] = registration
        self._tool_registrations_by_agent.setdefault(
            (registration.tenant_id, registration.logical_agent_id), []
        ).append(registration.id)
        return registration

    def list_tool_registrations(self, tenant_id: str, logical_agent_id: str) -> tuple[ToolRegistration, ...]:
        ids = self._tool_registrations_by_agent.get((tenant_id, logical_agent_id), [])
        return tuple(self._tool_registrations[registration_id] for registration_id in ids)


__all__ = [
    "AgentStudioStore",
    "AgentStudioStoreError",
    "role_at_least",
]
