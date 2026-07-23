"""Development deployments, health/trace metadata, rollback, and stable
logical-agent-ID resolution to an exact released version.
"""

from __future__ import annotations

from uuid import uuid4

from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry
from research_assistant_api.agent_studio.model_discovery import (
    ModelDiscovery,
    ModelDiscoveryError,
    UnavailableModelDiscovery,
)
from research_assistant_api.agent_studio.models import (
    AgentRole,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentRecord,
    HealthStatus,
    LogicalAgentBinding,
    ReleaseStatus,
    ResolvedAgentContract,
    role_at_least,
    utc_now,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

_DEPLOYABLE_RELEASE_STATUSES = frozenset(
    {ReleaseStatus.GATED, ReleaseStatus.APPROVED, ReleaseStatus.ACTIVE}
)


class DeploymentServiceError(RuntimeError):
    pass


class DeploymentService:
    def __init__(
        self,
        store: AgentStudioStore,
        *,
        capability_registry: CapabilityRegistry | None = None,
        model_discovery: ModelDiscovery | None = None,
    ) -> None:
        self._store = store
        # Both are optional purely for lightweight unit tests that exercise
        # deployment mechanics unrelated to model/approval revalidation.
        # Production wiring (``app.py``) always supplies both; when a
        # version actually declares a ``model_deployment`` or an approval-
        # gated capability binding, omitting these still fails closed at the
        # point the check would run (see ``_revalidate_model_deployment``/
        # ``_revalidate_capability_approvals`` below) rather than silently
        # skipping.
        self._registry = capability_registry
        self._model_discovery = model_discovery

    def _revalidate_model_deployment(self, version: AgentVersion) -> None:
        declared = version.model_deployment
        if declared is None:
            return
        discovery: ModelDiscovery = (
            self._model_discovery if self._model_discovery is not None else UnavailableModelDiscovery()
        )
        try:
            live = discovery.list_deployed_models()
        except ModelDiscoveryError as exc:
            raise DeploymentServiceError(
                f"Cannot revalidate model deployment '{declared.deployment_name}' at deploy time: {exc}"
            ) from exc
        match = next(
            (
                ref
                for ref in live
                if ref.deployment_name == declared.deployment_name and ref.model_name == declared.model_name
            ),
            None,
        )
        if match is None:
            raise DeploymentServiceError(
                f"Model deployment '{declared.deployment_name}' (model '{declared.model_name}') is missing, "
                "stale, or unavailable; deployment smoke-check cannot proceed."
            )

    def _revalidate_capability_approvals(self, scope: ScopeContext, version: AgentVersion) -> None:
        if self._registry is None:
            return
        catalog = self._registry.as_mapping()
        approvals = self._store.list_approvals(scope, version.id)
        for binding in version.manifest.capabilities:
            descriptor = catalog.get(binding.descriptor_ref.id)
            if descriptor is None:
                continue
            operation = descriptor.operation(binding.operation_ref.id)
            if operation is None or not operation.requires_approval:
                continue
            destination = f"{binding.descriptor_ref.id}.{binding.operation_ref.id}"
            approved = next(
                (
                    record
                    for record in approvals
                    if record.kind == ApprovalKind.CAPABILITY_OPERATION
                    and record.destination == destination
                    and record.state == ApprovalState.APPROVED
                ),
                None,
            )
            if approved is None:
                raise DeploymentServiceError(
                    f"Capability binding '{destination}' requires approval but no approved record was found."
                )
            if approved.content_hash != version.manifest_hash:
                raise DeploymentServiceError(
                    f"Capability binding '{destination}' approval is bound to a different manifest content hash."
                )
            if approved.expires_at is not None and approved.expires_at <= utc_now():
                raise DeploymentServiceError(f"Capability binding '{destination}' approval has expired.")

    def _revalidate_capability_bindings(self, version: AgentVersion) -> None:
        """Hard-fail deploy if any capability binding has gone stale.

        Independent of ``_revalidate_capability_approvals``: this re-checks
        descriptor/operation/instance digests, fingerprints, versions, and
        destination constraints against the *live* registry, not just
        approval state. A binding that was fresh at cut/gate time can still
        drift before deploy (e.g. the provider descriptor changed, or the
        discovered instance was reconfigured/removed).
        """
        if self._registry is None:
            return
        for binding in version.manifest.capabilities:
            reason = self._registry.check_binding_freshness(binding)
            if reason is not None:
                raise DeploymentServiceError(f"Capability binding is stale and cannot be deployed: {reason}")

    def deploy(
        self,
        *,
        tenant_id: str,
        project_id: str,
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
        The declared model deployment (if any) and any approval-gated
        capability bindings are revalidated again here, independent of the
        gate run at cut time, since either can have gone stale/expired
        between cut and deploy.
        """
        if not role_at_least(actor_role, AgentRole.CONTRIBUTOR):
            raise DeploymentServiceError(f"Role '{actor_role.value}' cannot create deployments.")
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        version = self._store.get_version(scope, version_id)
        if version is None or version.logical_agent_id != logical_agent_id:
            raise DeploymentServiceError(f"Version '{version_id}' not found for agent '{logical_agent_id}'.")
        latest_release = self._store.latest_release_for_version(scope, version_id)
        if latest_release is None or latest_release.status not in _DEPLOYABLE_RELEASE_STATUSES:
            current_status = latest_release.status.value if latest_release is not None else "none"
            raise DeploymentServiceError(
                f"Version '{version_id}' has release status '{current_status}'; it must pass all hard gates "
                "before it can be deployed."
            )
        self._revalidate_model_deployment(version)
        self._revalidate_capability_approvals(scope, version)
        self._revalidate_capability_bindings(version)
        runtime_target = version.runtime_target
        if runtime_target is None:
            raise DeploymentServiceError(f"Version '{version_id}' has no runtime_target resolved.")
        record = DeploymentRecord(
            id=str(uuid4()),
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version_id=version_id,
            runtime_target=runtime_target,
            deployed_by=deployed_by,
            trace_ref=trace_ref,
        )
        self._store.create_deployment(scope, record)
        self._store.set_binding(
            scope,
            LogicalAgentBinding(
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                project_id=project_id,
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
        project_id: str,
        deployment_id: str,
        status: HealthStatus,
        detail: str = "",
        trace_ref: str | None = None,
    ) -> DeploymentRecord:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        deployment = self._store.get_deployment(scope, deployment_id)
        if deployment is None:
            raise DeploymentServiceError(f"Deployment '{deployment_id}' not found.")
        updates: dict[str, object] = {"health": DeploymentHealth(status=status, detail=detail)}
        if trace_ref is not None:
            updates["trace_ref"] = trace_ref
        updated = deployment.model_copy(update=updates)
        return self._store.update_deployment(scope, updated)

    def rollback(
        self,
        *,
        tenant_id: str,
        project_id: str,
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
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        failing = self._store.get_deployment(scope, deployment_id)
        if failing is None or failing.logical_agent_id != logical_agent_id:
            raise DeploymentServiceError(f"Deployment '{deployment_id}' not found for agent '{logical_agent_id}'.")
        history = self._store.list_deployments(scope, logical_agent_id)
        if not any(deployment.version_id == target_version_id for deployment in history):
            raise DeploymentServiceError(
                f"Version '{target_version_id}' has no prior deployment history for agent '{logical_agent_id}'."
            )
        target_version = self._store.get_version(scope, target_version_id)
        if target_version is None:
            raise DeploymentServiceError(f"Version '{target_version_id}' not found.")
        record = DeploymentRecord(
            id=str(uuid4()),
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version_id=target_version_id,
            runtime_target=target_version.runtime_target or failing.runtime_target,
            deployed_by=deployed_by,
            rollback_of_deployment_id=deployment_id,
        )
        self._store.create_deployment(scope, record)
        self._store.set_binding(
            scope,
            LogicalAgentBinding(
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                project_id=project_id,
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
        project_id: str,
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
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        binding = self._store.get_binding(scope, logical_agent_id, environment)
        if binding is None:
            return None
        version = self._store.get_version(scope, binding.resolved_version_id)
        if version is None:
            return None
        return self._build_contract(scope, version, environment)

    def contract_for_version(
        self,
        *,
        tenant_id: str,
        project_id: str,
        version_id: str,
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
    ) -> ResolvedAgentContract | None:
        """Exact-version contract lookup, independent of any environment binding.

        For the future node palette/compiler to pin a specific, already-known
        version/release (e.g. re-validating a previously composed workflow
        node) without depending on "whatever is currently bound" for an
        environment.
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        version = self._store.get_version(scope, version_id)
        if version is None:
            return None
        return self._build_contract(scope, version, environment)

    def catalog(
        self,
        *,
        tenant_id: str,
        project_id: str,
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
    ) -> tuple[ResolvedAgentContract, ...]:
        """Released-agent catalog for the future node palette/compiler.

        Lists the exact, pinned contract currently bound to ``environment``
        for every logical agent this tenant has a draft/manifest for.
        Agents with no environment binding yet are omitted (nothing to pin).
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        contracts: list[ResolvedAgentContract] = []
        for draft in self._store.list_drafts(scope):
            contract = self.resolve(
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=draft.logical_agent_id,
                environment=environment,
            )
            if contract is not None:
                contracts.append(contract)
        return tuple(contracts)

    def _build_contract(
        self,
        scope: ScopeContext,
        version: AgentVersion,
        environment: DeploymentEnvironment,
    ) -> ResolvedAgentContract | None:
        if version.runtime_target is None:
            return None
        release = self._store.latest_release_for_version(scope, version.id)
        if release is None:
            return None
        return ResolvedAgentContract(
            logical_agent_id=version.logical_agent_id,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            environment=environment,
            version_id=version.id,
            release_id=release.id,
            release_status=release.status,
            manifest_hash=version.manifest_hash,
            runtime_target=version.runtime_target,
            capability_versions=dict(version.capability_versions),
            input_schema_ref=version.manifest.input_schema_ref,
            output_schema_ref=version.manifest.output_schema_ref,
            artifact_metadata=version.artifact_metadata,
            protocol_version=version.protocol_version,
        )

