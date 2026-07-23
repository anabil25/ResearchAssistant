"""Development deployments, health/trace metadata, rollback, and stable
logical-agent-ID resolution to an exact released version.
"""

from __future__ import annotations

from uuid import uuid4

from research_assistant_api.agent_studio.models import (
    AgentRole,
    AgentVersion,
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentRecord,
    HealthStatus,
    LogicalAgentBinding,
    ReleaseStatus,
    ResolvedAgentContract,
    role_at_least,
)
from research_assistant_api.agent_studio.store import AgentStudioStore

_DEPLOYABLE_RELEASE_STATUSES = frozenset(
    {ReleaseStatus.GATED, ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE}
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

        A version with no ``AgentRelease`` record at all (never gated, or
        gates failed) cannot be deployed even to the development
        environment; smoke-test failure at deployment time still blocks
        activation independently (see ``record_health``/router smoke gate).
        """
        if not role_at_least(actor_role, AgentRole.CONTRIBUTOR):
            raise DeploymentServiceError(f"Role '{actor_role.value}' cannot create deployments.")
        version = self._store.get_version(tenant_id, version_id)
        if version is None or version.logical_agent_id != logical_agent_id:
            raise DeploymentServiceError(f"Version '{version_id}' not found for agent '{logical_agent_id}'.")
        latest_release = self._store.latest_release_for_version(tenant_id, version_id)
        if latest_release is None or latest_release.status not in _DEPLOYABLE_RELEASE_STATUSES:
            current_status = latest_release.status.value if latest_release is not None else "none"
            raise DeploymentServiceError(
                f"Version '{version_id}' has release status '{current_status}'; it must pass all hard gates "
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
    ) -> ResolvedAgentContract | None:
        """Resolve a stable logical agent ID to the exact, pinned release contract.

        This is the composition/resolution contract consumed by the future
        typed workflow compiler/node palette: a published workflow pins the
        returned ``version_id``/``release_id``/``manifest_hash`` at compose
        time and execution must read them back verbatim, never silently
        re-resolving to "whatever is latest now".
        """
        binding = self._store.get_binding(tenant_id, logical_agent_id, environment)
        if binding is None:
            return None
        version = self._store.get_version(tenant_id, binding.resolved_version_id)
        if version is None:
            return None
        return self._build_contract(tenant_id, version, environment)

    def contract_for_version(
        self,
        *,
        tenant_id: str,
        version_id: str,
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
    ) -> ResolvedAgentContract | None:
        """Exact-version contract lookup, independent of any environment binding.

        For the future node palette/compiler to pin a specific, already-known
        version/release (e.g. re-validating a previously composed workflow
        node) without depending on "whatever is currently bound" for an
        environment.
        """
        version = self._store.get_version(tenant_id, version_id)
        if version is None:
            return None
        return self._build_contract(tenant_id, version, environment)

    def catalog(
        self,
        *,
        tenant_id: str,
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
    ) -> tuple[ResolvedAgentContract, ...]:
        """Released-agent catalog for the future node palette/compiler.

        Lists the exact, pinned contract currently bound to ``environment``
        for every logical agent this tenant has a draft/manifest for.
        Agents with no environment binding yet are omitted (nothing to pin).
        """
        contracts: list[ResolvedAgentContract] = []
        for draft in self._store.list_drafts(tenant_id):
            contract = self.resolve(
                tenant_id=tenant_id,
                logical_agent_id=draft.logical_agent_id,
                environment=environment,
            )
            if contract is not None:
                contracts.append(contract)
        return tuple(contracts)

    def _build_contract(
        self,
        tenant_id: str,
        version: AgentVersion,
        environment: DeploymentEnvironment,
    ) -> ResolvedAgentContract | None:
        if version.runtime_target is None:
            return None
        release = self._store.latest_release_for_version(tenant_id, version.id)
        if release is None:
            return None
        return ResolvedAgentContract(
            logical_agent_id=version.logical_agent_id,
            tenant_id=tenant_id,
            environment=environment,
            version_id=version.id,
            release_id=release.id,
            release_status=release.status,
            manifest_hash=version.manifest_hash,
            runtime_target=version.runtime_target,
            capability_versions=dict(version.capability_versions),
            input_schema_ref=version.manifest.input_schema_ref,
            output_schema_ref=version.manifest.output_schema_ref,
            package_version=version.package_version,
            protocol_version=version.protocol_version,
        )


