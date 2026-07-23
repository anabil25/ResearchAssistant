"""Development deployments, health/trace metadata, rollback, and stable
logical-agent-ID resolution to an exact released version.
"""

from __future__ import annotations

from uuid import uuid4

from research_assistant_api.agent_studio.models import (
    AgentRole,
    AgentVersion,
    AgentVersionStatus,
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentRecord,
    HealthStatus,
    LogicalAgentBinding,
    role_at_least,
)
from research_assistant_api.agent_studio.store import AgentStudioStore

_DEPLOYABLE_STATUSES = frozenset(
    {AgentVersionStatus.GATED, AgentVersionStatus.APPROVED, AgentVersionStatus.RELEASED}
)


class DeploymentServiceError(RuntimeError):
    pass


class DeploymentService:
    def __init__(self, store: AgentStudioStore) -> None:
        self._store = store

    def deploy(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        version_id: str,
        deployed_by: str,
        actor_role: AgentRole,
        trace_ref: str | None = None,
    ) -> DeploymentRecord:
        """Create a development deployment for a version that has passed all hard gates.

        Versions still in ``DRAFT`` (never gated, or gates failed) cannot be
        deployed even to the development environment.
        """
        if not role_at_least(actor_role, AgentRole.CONTRIBUTOR):
            raise DeploymentServiceError(f"Role '{actor_role.value}' cannot create deployments.")
        version = self._store.get_version(tenant_id, version_id)
        if version is None or version.logical_agent_id != logical_agent_id:
            raise DeploymentServiceError(f"Version '{version_id}' not found for agent '{logical_agent_id}'.")
        if version.status not in _DEPLOYABLE_STATUSES:
            raise DeploymentServiceError(
                f"Version '{version_id}' has status '{version.status.value}'; it must pass all hard gates "
                "before it can be deployed."
            )
        runtime_target = version.runtime_target
        if runtime_target is None:
            raise DeploymentServiceError(f"Version '{version_id}' has no runtime_target resolved.")
        record = DeploymentRecord(
            id=str(uuid4()),
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            version_id=version_id,
            runtime_target=runtime_target,
            deployed_by=deployed_by,
            trace_ref=trace_ref,
        )
        self._store.create_deployment(record)
        self._store.set_binding(
            LogicalAgentBinding(
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                environment=DeploymentEnvironment.DEVELOPMENT,
                resolved_version_id=version_id,
                updated_by=deployed_by,
            )
        )
        return record

    def record_health(
        self,
        *,
        tenant_id: str,
        deployment_id: str,
        status: HealthStatus,
        detail: str = "",
        trace_ref: str | None = None,
    ) -> DeploymentRecord:
        deployment = self._store.get_deployment(tenant_id, deployment_id)
        if deployment is None:
            raise DeploymentServiceError(f"Deployment '{deployment_id}' not found.")
        updates: dict[str, object] = {"health": DeploymentHealth(status=status, detail=detail)}
        if trace_ref is not None:
            updates["trace_ref"] = trace_ref
        updated = deployment.model_copy(update=updates)
        return self._store.update_deployment(updated)

    def rollback(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        deployment_id: str,
        target_version_id: str,
        deployed_by: str,
        actor_role: AgentRole,
    ) -> DeploymentRecord:
        """Roll a logical agent's development deployment back to a previously deployed version.

        ``target_version_id`` must have been deployed for this logical agent
        before (rollback never invents a new/unvetted version target).
        """
        if not role_at_least(actor_role, AgentRole.MAINTAINER):
            raise DeploymentServiceError(f"Role '{actor_role.value}' cannot perform a rollback.")
        failing = self._store.get_deployment(tenant_id, deployment_id)
        if failing is None or failing.logical_agent_id != logical_agent_id:
            raise DeploymentServiceError(f"Deployment '{deployment_id}' not found for agent '{logical_agent_id}'.")
        history = self._store.list_deployments(tenant_id, logical_agent_id)
        if not any(deployment.version_id == target_version_id for deployment in history):
            raise DeploymentServiceError(
                f"Version '{target_version_id}' has no prior deployment history for agent '{logical_agent_id}'."
            )
        target_version = self._store.get_version(tenant_id, target_version_id)
        if target_version is None:
            raise DeploymentServiceError(f"Version '{target_version_id}' not found.")
        record = DeploymentRecord(
            id=str(uuid4()),
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            version_id=target_version_id,
            runtime_target=target_version.runtime_target or failing.runtime_target,
            deployed_by=deployed_by,
            rollback_of_deployment_id=deployment_id,
        )
        self._store.create_deployment(record)
        self._store.set_binding(
            LogicalAgentBinding(
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                environment=DeploymentEnvironment.DEVELOPMENT,
                resolved_version_id=target_version_id,
                updated_by=deployed_by,
            )
        )
        return record

    def resolve(
        self,
        *,
        tenant_id: str,
        logical_agent_id: str,
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
    ) -> AgentVersion | None:
        """Resolve a stable logical agent ID to the exact release it is bound to."""
        binding = self._store.get_binding(tenant_id, logical_agent_id, environment)
        if binding is None:
            return None
        return self._store.get_version(tenant_id, binding.resolved_version_id)
