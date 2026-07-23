"""Draft mutation, immutable version cuts, forks/lineage, and promotion.

This is the central authorization/business-logic surface: it is the only
place that mutates drafts, cuts immutable ``AgentVersion`` records, records
lineage, runs the deterministic runtime selection, and drives the
promotion/approval workflow. Every mutating method takes an explicit
``actor_role`` (resolved by the caller from ownership + identity) and
enforces role-based authorization before doing anything.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from research_assistant_api.agent_studio.approvals import (
    DEFAULT_APPROVAL_VALIDITY,
    ApprovalError,
    decide_approval,
    idempotency_key,
    requires_approval,
)
from research_assistant_api.agent_studio.capability_registry import CapabilityRegistry
from research_assistant_api.agent_studio.model_discovery import (
    ModelDiscovery,
    ModelDiscoveryError,
    UnavailableModelDiscovery,
)
from research_assistant_api.agent_studio.models import (
    AGENT_STUDIO_PROTOCOL_VERSION,
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    AgentVisibility,
    ApprovalKind,
    ApprovalState,
    DeploymentEnvironment,
    LineageEdge,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    StudioApprovalRecord,
    ToolRegistrationKind,
    ToolRegistrationSpec,
    role_at_least,
    utc_now,
)
from research_assistant_api.agent_studio.policy_gates import GateEvidence, run_gates
from research_assistant_api.agent_studio.release_artifact_metadata import (
    InstalledPackageArtifactSource,
    ReleaseArtifactSource,
)
from research_assistant_api.agent_studio.runtime_selection import select_runtime
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore


class ReleaseServiceError(RuntimeError):
    pass


class AuthorizationError(ReleaseServiceError):
    pass


def manifest_hash(manifest: AgentManifest) -> str:
    """Deterministic content hash for a manifest (stable key ordering)."""
    canonical = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_scoped_approval_request(
    *,
    approval_id: str,
    tenant_id: str,
    project_id: str,
    version_id: str,
    kind: ApprovalKind,
    gated_action: str,
    destination: str,
    requested_by: str,
    evidence_summary: str,
    risk: str,
    requested_role: AgentRole | None = None,
    content_hash: str | None = None,
    environment: DeploymentEnvironment | None = None,
    permissions_policy_ref: str | None = None,
    destination_policy_ref: str | None = None,
) -> StudioApprovalRecord:
    if kind is ApprovalKind.ADMIN_ESCALATION and requested_role is None:
        raise ApprovalError("Admin escalation requests must specify requested_role.")
    return StudioApprovalRecord(
        id=approval_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        kind=kind,
        gated_action=gated_action,
        destination=destination,
        requested_by=requested_by,
        evidence_summary=evidence_summary,
        risk=risk,
        idempotency_key=idempotency_key(
            kind=kind,
            version_id=version_id,
            requested_by=requested_by,
            destination=destination,
        ),
        requested_role=requested_role,
        content_hash=content_hash,
        environment=environment,
        permissions_policy_ref=permissions_policy_ref,
        destination_policy_ref=destination_policy_ref,
        expires_at=utc_now() + DEFAULT_APPROVAL_VALIDITY,
    )


class ReleaseService:
    def __init__(
        self,
        store: AgentStudioStore,
        capability_registry: CapabilityRegistry,
        *,
        model_discovery: ModelDiscovery | None = None,
        artifact_source: ReleaseArtifactSource | None = None,
    ) -> None:
        self._store = store
        self._registry = capability_registry
        # Fail-closed default: if no live discovery is wired, any manifest
        # that declares a ``model_deployment`` will hard-fail cut/deploy
        # revalidation rather than silently skipping the check. Tests must
        # explicitly supply a fake/in-memory discovery to exercise the
        # success path.
        self._model_discovery: ModelDiscovery = (
            model_discovery if model_discovery is not None else UnavailableModelDiscovery()
        )
        self._artifact_source: ReleaseArtifactSource = (
            artifact_source if artifact_source is not None else InstalledPackageArtifactSource()
        )

    # -- Agent creation and drafts ----------------------------------------

    def create_agent(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        display_name: str,
        owner_kind: AgentOwnerKind,
        owner_id: str,
        requested_by: str,
        is_platform_owner: bool,
        visibility: AgentVisibility = AgentVisibility.PRIVATE,
        description: str = "",
    ) -> AgentDraft:
        """Create a new logical agent's initial draft.

        System agents may only be created by a platform owner (``platform
        owners version system agents``); user agents may be created by any
        authenticated principal, who becomes the initial ``OWNER``.
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        if owner_kind is AgentOwnerKind.SYSTEM and not is_platform_owner:
            raise AuthorizationError("Only platform owners may create system agents.")
        if self._store.get_draft(scope, logical_agent_id) is not None:
            raise ReleaseServiceError(f"Agent '{logical_agent_id}' already exists.")
        manifest = AgentManifest(
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            display_name=display_name,
            description=description,
            owner_kind=owner_kind,
            owner_id=owner_id,
            visibility=visibility,
        )
        draft = AgentDraft(
            logical_agent_id=logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            manifest=manifest,
            updated_by=requested_by,
        )
        self._store.save_draft(scope, draft)
        self._store.grant_ownership(
            scope,
            OwnershipGrant(
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=logical_agent_id,
                principal_id=owner_id,
                role=AgentRole.OWNER,
                granted_by=requested_by,
            )
        )
        return draft

    def update_draft(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        manifest: AgentManifest,
        updated_by: str,
        actor_role: AgentRole,
    ) -> AgentDraft:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        if (
            manifest.logical_agent_id != logical_agent_id
            or manifest.tenant_id != tenant_id
            or manifest.project_id != project_id
        ):
            raise ReleaseServiceError("Manifest logical_agent_id/tenant_id/project_id must match the target agent.")
        existing = self._store.get_draft(scope, logical_agent_id)
        if existing is None:
            raise ReleaseServiceError(f"Agent '{logical_agent_id}' has no draft to update.")
        updated = existing.model_copy(
            update={
                "manifest": manifest,
                "updated_by": updated_by,
                "updated_at": utc_now(),
                "etag": str(uuid4()),
            }
        )
        return self._store.save_draft(scope, updated)

    # -- Forking (private specialists) ------------------------------------

    def fork(
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_logical_agent_id: str,
        source_version_id: str,
        new_logical_agent_id: str,
        requested_by: str,
    ) -> AgentDraft:
        """Fork a released version into a new private, user-owned draft.

        Only same-scope forking is supported: the source version must be
        resolvable in the caller's own tenant/project. Cross-tenant or
        cross-project template sharing is out of scope for this
        implementation and must be handled by an explicit export/import
        flow, not silent cross-scope reads.
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        source_version = self._store.get_version(scope, source_version_id)
        if source_version is None or source_version.logical_agent_id != source_logical_agent_id:
            raise ReleaseServiceError(f"Source version '{source_version_id}' was not found in this scope.")
        if self._store.get_draft(scope, new_logical_agent_id) is not None:
            raise ReleaseServiceError(f"Agent '{new_logical_agent_id}' already exists.")
        forked_manifest = source_version.manifest.model_copy(
            update={
                "logical_agent_id": new_logical_agent_id,
                "owner_kind": AgentOwnerKind.USER,
                "owner_id": requested_by,
                "visibility": AgentVisibility.PRIVATE,
            }
        )
        draft = AgentDraft(
            logical_agent_id=new_logical_agent_id,
            tenant_id=tenant_id,
            project_id=project_id,
            manifest=forked_manifest,
            updated_by=requested_by,
            based_on_version_id=source_version_id,
        )
        self._store.save_draft(scope, draft)
        self._store.grant_ownership(
            scope,
            OwnershipGrant(
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=new_logical_agent_id,
                principal_id=requested_by,
                role=AgentRole.OWNER,
                granted_by=requested_by,
            )
        )
        return draft

    # -- Cutting immutable versions ----------------------------------------

    def _revalidate_model_deployment(self, manifest: AgentManifest) -> None:
        """Hard-fail if a declared ``model_deployment`` isn't live in the
        project's discovered deployments right now.

        A manifest with no ``model_deployment`` (e.g. a pure Custom Hosted
        agent with no model policy) has nothing to revalidate. Otherwise the
        declared deployment must still exist, live, in
        ``model_discovery.list_deployed_models()`` with a matching resource
        id/name and model name — missing, stale, or unavailable discovery
        all hard-fail identically rather than trusting the declared ref.
        """
        declared = manifest.model_deployment
        if declared is None:
            return
        try:
            live = self._model_discovery.list_deployed_models()
        except ModelDiscoveryError as exc:
            raise ReleaseServiceError(
                f"Cannot revalidate model deployment '{declared.deployment_name}': {exc}"
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
            raise ReleaseServiceError(
                f"Model deployment '{declared.deployment_name}' (model '{declared.model_name}') "
                "was not found among the project's live deployed models; it is missing, stale, or unavailable."
            )

    def cut_version(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        actor_id: str,
        actor_role: AgentRole,
    ) -> AgentVersion:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        draft = self._store.get_draft(scope, logical_agent_id)
        if draft is None:
            raise ReleaseServiceError(f"Agent '{logical_agent_id}' has no draft to cut.")
        self._revalidate_model_deployment(draft.manifest)
        previous_versions = self._store.list_versions(scope, logical_agent_id)
        parent_version_id = previous_versions[-1].id if previous_versions else None

        selection = select_runtime(draft.manifest, self._registry.as_mapping())
        capability_versions = {
            binding.descriptor_id: binding.descriptor_version for binding in draft.manifest.capabilities
        }

        def _build(sequence: int) -> AgentVersion:
            # Cutting a version freezes the canonical manifest/hash/bundle
            # content forever; only the pre-reserved ``sequence`` (supplied
            # atomically by the store) and lineage/fork ids derived from it
            # vary between the two constructions.
            fork_of_version_id = draft.based_on_version_id if sequence == 1 else None
            return AgentVersion(
                id=str(uuid4()),
                logical_agent_id=logical_agent_id,
                tenant_id=tenant_id,
                project_id=project_id,
                sequence=sequence,
                manifest=draft.manifest,
                manifest_hash=manifest_hash(draft.manifest),
                created_by=actor_id,
                parent_version_id=parent_version_id,
                fork_of_version_id=fork_of_version_id,
                runtime_target=selection.target,
                runtime_selection_reasons=selection.reasons,
                model_deployment=draft.manifest.model_deployment,
                capability_versions=capability_versions,
                artifact_metadata=self._artifact_source.current_metadata(),
                protocol_version=AGENT_STUDIO_PROTOCOL_VERSION,
            )

        version = self._store.allocate_version(scope, logical_agent_id, _build)

        if version.fork_of_version_id is not None:
            parent = self._store.get_version(scope, version.fork_of_version_id)
            if parent is not None:
                self._store.add_lineage_edge(
                    scope,
                    LineageEdge(
                        tenant_id=tenant_id,
                        project_id=project_id,
                        child_logical_agent_id=logical_agent_id,
                        child_version_id=version.id,
                        parent_logical_agent_id=parent.logical_agent_id,
                        parent_version_id=parent.id,
                    )
                )
        return version

    # -- Hard gates -------------------------------------------------------

    def run_release_gates(
        self,
        *,
        tenant_id: str,
        project_id: str,
        version_id: str,
        evidence: GateEvidence,
    ) -> ReleaseGateReport:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        version = self._store.get_version(scope, version_id)
        if version is None:
            raise ReleaseServiceError(f"Version '{version_id}' not found.")
        report = run_gates(
            version_id=version_id,
            report_id=str(uuid4()),
            manifest=version.manifest,
            manifest_hash=version.manifest_hash,
            capability_catalog=self._registry.as_mapping(),
            evidence=evidence,
            runtime_target=version.runtime_target,
            capability_approvals=self._store.list_approvals(scope, version_id),
        )
        self._store.save_gate_report(report)
        if report.passed:
            # Only a passing gate run produces a durable ``AgentRelease``
            # row; a failed attempt leaves the immutable ``ReleaseGateReport``
            # as the sole audit record and the version stays un-released.
            previous = self._store.latest_release_for_version(scope, version_id)
            release = AgentRelease(
                id=str(uuid4()),
                version_id=version_id,
                logical_agent_id=version.logical_agent_id,
                tenant_id=tenant_id,
                project_id=project_id,
                status=ReleaseStatus.GATED,
                gate_report_id=report.id,
                previous_release_id=previous.id if previous is not None else None,
                created_by=version.created_by,
                detail="Passed all applicable hard gates.",
            )
            self._store.create_release(scope, release)
        return report

    # -- Promotion / approvals --------------------------------------------

    def request_promotion(
        self,
        *,
        tenant_id: str,
        project_id: str,
        version_id: str,
        actor_id: str,
        actor_role: AgentRole,
        destination: str,
        evidence_summary: str,
        risk: str = "medium",
        environment: DeploymentEnvironment = DeploymentEnvironment.DEVELOPMENT,
        permissions_policy_ref: str | None = None,
        destination_policy_ref: str | None = None,
    ) -> StudioApprovalRecord | AgentVersion:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        version = self._store.get_version(scope, version_id)
        if version is None:
            raise ReleaseServiceError(f"Version '{version_id}' not found.")
        latest_release = self._store.latest_release_for_version(scope, version_id)
        if latest_release is None or latest_release.status != ReleaseStatus.GATED:
            current_status = latest_release.status.value if latest_release is not None else "none"
            raise ReleaseServiceError(
                f"Version '{version_id}' has release status '{current_status}'; it must pass all hard gates "
                "(status GATED) before promotion can be requested."
            )
        kind = ApprovalKind.FORK_PROMOTION if version.fork_of_version_id else ApprovalKind.RELEASE_PROMOTION
        if requires_approval(actor_role=actor_role, kind=kind):
            record = _build_scoped_approval_request(
                approval_id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                version_id=version_id,
                kind=kind,
                gated_action="promote_version",
                destination=destination,
                requested_by=actor_id,
                evidence_summary=evidence_summary,
                risk=risk,
                content_hash=version.manifest_hash,
                environment=environment,
                permissions_policy_ref=permissions_policy_ref,
                destination_policy_ref=destination_policy_ref,
            )
            return self._store.create_approval(scope, record)
        return self._promote(scope, version_id)

    def decide_promotion(
        self,
        *,
        tenant_id: str,
        project_id: str,
        approval_id: str,
        approver_id: str,
        approver_role: AgentRole,
        approve: bool,
        rationale: str | None = None,
    ) -> StudioApprovalRecord:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        record = self._store.get_approval(scope, approval_id)
        if record is None:
            raise ReleaseServiceError(f"Approval '{approval_id}' not found.")
        decided = decide_approval(
            record,
            approver_id=approver_id,
            approver_role=approver_role,
            approve=approve,
            rationale=rationale,
        )
        self._store.save_approval_decision(scope, decided)
        if decided.state == ApprovalState.APPROVED:
            self._promote(scope, decided.version_id)
        return decided

    # -- Capability-operation approvals -----------------------------------

    def request_capability_approval(
        self,
        *,
        tenant_id: str,
        project_id: str,
        version_id: str,
        descriptor_id: str,
        operation: str,
        actor_id: str,
        actor_role: AgentRole,
        evidence_summary: str,
        risk: str = "medium",
        permissions_policy_ref: str | None = None,
        destination_policy_ref: str | None = None,
    ) -> StudioApprovalRecord:
        """Request approval for one attached capability operation binding.

        This is the way the APPROVAL hard gate (and deploy-time recheck) is
        satisfied for an operation whose ``CapabilityOperation.requires_approval``
        is True: the record is bound to this exact version's content hash and
        to the ``descriptor_id.operation`` destination, so it can never be
        reused to approve a different manifest or a different operation.
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        version = self._store.get_version(scope, version_id)
        if version is None:
            raise ReleaseServiceError(f"Version '{version_id}' not found.")
        destination = f"{descriptor_id}.{operation}"
        binding = next(
            (
                b
                for b in version.manifest.capabilities
                if b.descriptor_id == descriptor_id and b.operation == operation
            ),
            None,
        )
        if binding is None:
            raise ReleaseServiceError(f"Version '{version_id}' has no capability binding for '{destination}'.")
        record = _build_scoped_approval_request(
            approval_id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            version_id=version_id,
            kind=ApprovalKind.CAPABILITY_OPERATION,
            gated_action="attach_capability_operation",
            destination=destination,
            requested_by=actor_id,
            evidence_summary=evidence_summary,
            risk=risk,
            content_hash=version.manifest_hash,
            permissions_policy_ref=permissions_policy_ref,
            destination_policy_ref=destination_policy_ref,
        )
        return self._store.create_approval(scope, record)

    def decide_capability_approval(
        self,
        *,
        tenant_id: str,
        project_id: str,
        approval_id: str,
        approver_id: str,
        approver_role: AgentRole,
        approve: bool,
        rationale: str | None = None,
    ) -> StudioApprovalRecord:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        record = self._store.get_approval(scope, approval_id)
        if record is None:
            raise ReleaseServiceError(f"Approval '{approval_id}' not found.")
        if record.kind is not ApprovalKind.CAPABILITY_OPERATION:
            raise ReleaseServiceError(f"Approval '{approval_id}' is not a capability-operation approval.")
        decided = decide_approval(
            record,
            approver_id=approver_id,
            approver_role=approver_role,
            approve=approve,
            rationale=rationale,
        )
        return self._store.save_approval_decision(scope, decided)

    def _promote(self, scope: ScopeContext, version_id: str) -> AgentVersion:
        version = self._store.get_version(scope, version_id)
        if version is None:
            raise ReleaseServiceError(f"Version '{version_id}' not found.")
        gated = self._store.latest_release_for_version(scope, version_id)
        if gated is None:
            raise ReleaseServiceError(f"Version '{version_id}' has no gated release to promote.")
        # Each transition is its own append-only record, chained via
        # ``previous_release_id`` — never an in-place mutation of ``gated``.
        approved = self._store.create_release(
            scope,
            AgentRelease(
                id=str(uuid4()),
                version_id=version_id,
                logical_agent_id=version.logical_agent_id,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                status=ReleaseStatus.APPROVED,
                gate_report_id=gated.gate_report_id,
                previous_release_id=gated.id,
                created_by=version.created_by,
                detail="Promotion approved.",
            )
        )
        active = self._store.create_release(
            scope,
            AgentRelease(
                id=str(uuid4()),
                version_id=version_id,
                logical_agent_id=version.logical_agent_id,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                status=ReleaseStatus.ACTIVE,
                gate_report_id=gated.gate_report_id,
                previous_release_id=approved.id,
                created_by=version.created_by,
                detail="Version activated.",
            )
        )
        _ = active
        return version

    # -- Admin escalation --------------------------------------------------

    def request_role_escalation(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        requested_by: str,
        requested_role: AgentRole,
        evidence_summary: str,
        risk: str = "high",
    ) -> StudioApprovalRecord:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        record = _build_scoped_approval_request(
            approval_id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            version_id=logical_agent_id,
            kind=ApprovalKind.ADMIN_ESCALATION,
            gated_action="grant_role",
            destination=logical_agent_id,
            requested_by=requested_by,
            evidence_summary=evidence_summary,
            risk=risk,
            requested_role=requested_role,
        )
        return self._store.create_approval(scope, record)

    def decide_role_escalation(
        self,
        *,
        tenant_id: str,
        project_id: str,
        approval_id: str,
        approver_id: str,
        approver_role: AgentRole,
        approve: bool,
        rationale: str | None = None,
    ) -> StudioApprovalRecord:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        record = self._store.get_approval(scope, approval_id)
        if record is None:
            raise ReleaseServiceError(f"Approval '{approval_id}' not found.")
        if record.kind is not ApprovalKind.ADMIN_ESCALATION:
            raise ReleaseServiceError(f"Approval '{approval_id}' is not an admin escalation request.")
        decided = decide_approval(
            record,
            approver_id=approver_id,
            approver_role=approver_role,
            approve=approve,
            rationale=rationale,
        )
        self._store.save_approval_decision(scope, decided)
        if decided.state == ApprovalState.APPROVED and decided.requested_role is not None:
            self._store.grant_ownership(
                scope,
                OwnershipGrant(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    logical_agent_id=decided.destination,
                    principal_id=decided.requested_by,
                    role=decided.requested_role,
                    granted_by=approver_id,
                )
            )
        return decided

    @staticmethod
    def _require_role(actor_role: AgentRole, minimum: AgentRole) -> None:
        if not role_at_least(actor_role, minimum):
            raise AuthorizationError(f"Role '{actor_role.value}' does not meet the minimum '{minimum.value}'.")

    # -- Tool registration (runtime handler wiring) ------------------------

    def register_tool(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
        descriptor_id: str,
        operation: str,
        kind: ToolRegistrationKind,
        handler_ref: str,
        registered_by: str,
        actor_role: AgentRole,
    ) -> ToolRegistrationSpec:
        """Register the runtime handler for a GA capability operation.

        Distinct from attaching a ``CapabilityBinding`` to a manifest: this
        declares *how* an already-attachable operation is dispatched at
        runtime (Managed Foundry native resolution vs. a Custom Hosted
        handler reference), and is independently authorized/persisted.
        """
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        self._require_role(actor_role, AgentRole.CONTRIBUTOR)
        self._registry.validate_attachment(descriptor_id=descriptor_id, operation=operation)
        registration = ToolRegistrationSpec(
            id=str(uuid4()),
            tenant_id=tenant_id,
            project_id=project_id,
            logical_agent_id=logical_agent_id,
            descriptor_id=descriptor_id,
            operation=operation,
            kind=kind,
            handler_ref=handler_ref,
            registered_by=registered_by,
        )
        return self._store.create_tool_registration(scope, registration)

    def list_tool_registrations(
        self,
        tenant_id: str,
        project_id: str,
        logical_agent_id: str,
    ) -> tuple[ToolRegistrationSpec, ...]:
        scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
        return self._store.list_tool_registrations(scope, logical_agent_id)


def resolve_actor_role(
    store: AgentStudioStore,
    *,
    tenant_id: str,
    project_id: str,
    logical_agent_id: str,
    principal_id: str,
) -> AgentRole:
    """Resolve the effective role of ``principal_id`` on an agent, defaulting to VIEWER."""
    scope = ScopeContext(tenant_id=tenant_id, project_id=project_id)
    role = store.role_for(scope, logical_agent_id, principal_id)
    return role if role is not None else AgentRole.VIEWER
