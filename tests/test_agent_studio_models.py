# mypy: disable-error-code=import-untyped

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import (
    AGENT_MANIFEST_SCHEMA_VERSION,
    AGENT_STUDIO_PROTOCOL_VERSION,
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    AgentVisibility,
    ApprovalKind,
    ArtifactContract,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityOperation,
    CitationPolicy,
    DelegationScope,
    DeploymentEnvironment,
    DeploymentRecord,
    EvaluationRecord,
    GateName,
    GateResult,
    GateStatus,
    HealthStatus,
    KnowledgeBinding,
    KnowledgeBindingKind,
    LineageEdge,
    LogicalAgentBinding,
    MemoryAuditAction,
    MemoryAuditRecord,
    MemoryEntry,
    MemoryMechanism,
    MemoryPolicy,
    MemoryScopeBinding,
    MemoryScopeKind,
    ModelDeploymentRef,
    OperationClass,
    OperationMaturity,
    ReleaseGateReport,
    ReleaseStatus,
    ResolvedAgentContract,
    RuntimeRequirements,
    RuntimeSelection,
    RuntimeTarget,
    SchemaRef,
    SpecialistPolicy,
    StudioApprovalRecord,
    TemplateProvenance,
    ToolRegistrationKind,
    ToolRegistrationSpec,
    WorkspaceConnectionRef,
    role_at_least,
)


def _schema_ref(name: str) -> SchemaRef:
    return SchemaRef(
        ref=f"schema://{name}",
        digest=f"sha256:{name}",
        inline_schema={"type": "object", "title": name},
    )


def _binding() -> CapabilityBinding:
    return CapabilityBinding(
        descriptor_id="foundry.azure_ai_search",
        descriptor_version="7",
        descriptor_digest="sha256:descriptor",
        operation="search",
        instance_id="instance-1",
        pinned_provider_version="2026.07",
        instance_fingerprint="sha256:instance",
        input_schema_digest="sha256:input",
        output_schema_digest="sha256:output",
        config={"index": "docs"},
        config_hash="sha256:config",
        connection_ref="conn-search",
        policy_ref="policy://search",
        attached_by="user-1",
    )


def test_role_and_memory_helpers_cover_true_and_false_paths() -> None:
    assert role_at_least(AgentRole.OWNER, AgentRole.VIEWER)
    assert role_at_least(AgentRole.MAINTAINER, AgentRole.MAINTAINER)
    assert not role_at_least(AgentRole.CONTRIBUTOR, AgentRole.MAINTAINER)
    assert not role_at_least(AgentRole.VIEWER, AgentRole.OWNER)

    assert MemoryMechanism.APPLICATION_THREAD.is_ga
    assert MemoryMechanism.APPLICATION_MEMORY_STORE.is_ga
    assert not MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE.is_ga


def test_capability_descriptor_operation_lookup_preserves_provenance() -> None:
    verified_at = datetime(2026, 7, 23, tzinfo=UTC)
    descriptor = CapabilityDescriptor(
        id="foundry.test",
        provider="microsoft_foundry",
        title="Test capability",
        description="Capability descriptor used for unit tests.",
        operations=(
            CapabilityOperation(
                name="search",
                maturity=OperationMaturity.GA,
                operation_class=OperationClass.READ,
                side_effect_destinations=("public_web",),
                source_url="https://example.test/catalog",
                source_version="2026-07",
                last_verified_at=verified_at,
            ),
            CapabilityOperation(
                name="write",
                maturity=OperationMaturity.PREVIEW,
                operation_class=OperationClass.WRITE_IRREVERSIBLE,
                requires_approval=True,
                reason="Preview only.",
            ),
        ),
    )

    resolved = descriptor.operation("search")
    assert resolved is not None
    assert resolved.maturity is OperationMaturity.GA
    assert resolved.operation_class is OperationClass.READ
    assert resolved.side_effect_destinations == ("public_web",)
    assert resolved.source_url == "https://example.test/catalog"
    assert resolved.source_version == "2026-07"
    assert resolved.last_verified_at == verified_at
    assert descriptor.operation("missing") is None


def test_agent_manifest_validates_full_current_shape_and_defaults() -> None:
    manifest = AgentManifest(
        logical_agent_id="agent-agent-studio-test",
        tenant_id="tenant-1",
        display_name="Agent Studio test agent",
        description="Exercises the full manifest shape.",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        visibility=AgentVisibility.ORG,
        capabilities=(_binding(),),
        runtime_requirements=RuntimeRequirements(
            requires_custom_code=False,
            requires_custom_orchestration_workflow=True,
            requires_non_ga_tool=False,
            uses_project_deployed_model_only=False,
        ),
        model_deployment=ModelDeploymentRef(
            deployment_name="gpt-4o-prod",
            model_name="gpt-4o",
            model_format="openai",
            capacity=30,
        ),
        input_schema_ref=_schema_ref("input"),
        output_schema_ref=_schema_ref("output"),
        knowledge_bindings=(
            KnowledgeBinding(
                kind=KnowledgeBindingKind.AZURE_AI_SEARCH,
                connection_ref="conn-search",
                capability_binding_index=0,
                description="Ground from Azure AI Search.",
            ),
        ),
        memory_policy=MemoryPolicy(
            scopes=(
                MemoryScopeBinding(
                    kind=MemoryScopeKind.PROJECT,
                    enabled=True,
                    mechanism=MemoryMechanism.APPLICATION_MEMORY_STORE,
                    retention_days=30,
                ),
            ),
        ),
        specialist_policy=SpecialistPolicy(
            delegation_scope=DelegationScope.SPECIALIST_POOL,
            allowed_specialist_logical_agent_ids=("agent-specialist-a",),
            max_delegation_depth=2,
        ),
        policy_refs=("policy://destination", "policy://permissions"),
        evaluation_suite_refs=("eval://safety",),
        citation_policy=CitationPolicy(
            require_citations=True,
            allowed_evidence_sources=("azure_ai_search", "public_web"),
        ),
        artifact_contract=ArtifactContract(
            output_kind="json",
            max_output_bytes=4096,
            requires_human_review=True,
        ),
        template_provenance=TemplateProvenance(
            template_id="template-1",
            template_version="3",
            source_url="https://example.test/template",
        ),
        workspace_connections=("conn-search", "conn-memory"),
        tags=("governed", "agent-studio"),
    )

    assert manifest.project_id == "default"
    assert manifest.schema_version == AGENT_MANIFEST_SCHEMA_VERSION
    assert manifest.capabilities[0].instance_id == "instance-1"
    assert manifest.capabilities[0].connection_ref == "conn-search"
    assert manifest.knowledge_bindings[0].capability_binding_index == 0
    assert manifest.memory_policy.is_enabled(MemoryScopeKind.PROJECT) is True
    assert manifest.specialist_policy.delegation_scope is DelegationScope.SPECIALIST_POOL
    assert manifest.citation_policy.require_citations is True
    assert manifest.artifact_contract.output_kind == "json"
    assert manifest.template_provenance is not None
    assert manifest.workspace_connections == ("conn-search", "conn-memory")
    assert manifest.tags == ("governed", "agent-studio")


def test_agent_manifest_rejects_invalid_logical_agent_id() -> None:
    with pytest.raises(ValidationError):
        AgentManifest(
            logical_agent_id="not-valid",
            tenant_id="tenant-1",
            display_name="Broken",
            owner_kind=AgentOwnerKind.USER,
            owner_id="user-1",
        )


def test_related_models_construct_and_default_factories_execute() -> None:
    manifest = AgentManifest(
        logical_agent_id="agent-related-models",
        tenant_id="tenant-1",
        display_name="Related models agent",
        owner_kind=AgentOwnerKind.SYSTEM,
        owner_id="system",
        capabilities=(_binding(),),
    )
    runtime_selection = RuntimeSelection(
        target=RuntimeTarget.CUSTOM_HOSTED,
        reasons=("requires_custom_code",),
    )
    tool_registration = ToolRegistrationSpec(
        id="tool-1",
        tenant_id="tenant-1",
        project_id="project-1",
        logical_agent_id=manifest.logical_agent_id,
        descriptor_id="custom.hosted_code",
        operation="run",
        kind=ToolRegistrationKind.CUSTOM_HANDLER,
        handler_ref="python://handlers.run",
        registered_by="system",
    )
    workspace_connection = WorkspaceConnectionRef(
        id="conn-1",
        kind="azure_ai_search",
        display_name="Search connection",
        tenant_id="tenant-1",
        project_id="project-1",
    )
    memory_entry = MemoryEntry(
        id="memory-1",
        tenant_id="tenant-1",
        project_id="project-1",
        scope_kind=MemoryScopeKind.USER,
        scope_id="user-1",
        logical_agent_id=manifest.logical_agent_id,
        content="Remember this.",
        created_by="user-1",
        ttl_days=7,
    )
    memory_audit = MemoryAuditRecord(
        id="audit-1",
        tenant_id="tenant-1",
        project_id="project-1",
        entry_id=memory_entry.id,
        action=MemoryAuditAction.EXPORT,
        actor_id="user-1",
    )
    draft = AgentDraft(
        logical_agent_id=manifest.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        manifest=manifest,
        updated_by="user-1",
    )
    lineage = LineageEdge(
        tenant_id="tenant-1",
        project_id="project-1",
        child_logical_agent_id=manifest.logical_agent_id,
        child_version_id="version-2",
        parent_logical_agent_id="agent-parent",
        parent_version_id="version-1",
    )
    version = AgentVersion(
        id="version-2",
        logical_agent_id=manifest.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        sequence=2,
        manifest=manifest,
        manifest_hash="sha256:manifest",
        created_by="user-1",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        runtime_selection_reasons=("requires_custom_code",),
        model_deployment=ModelDeploymentRef(
            deployment_name="gpt-4o-prod",
            model_name="gpt-4o",
            model_format="openai",
        ),
        capability_versions={"foundry.azure_ai_search": "7"},
    )
    release = AgentRelease(
        id="release-1",
        version_id=version.id,
        logical_agent_id=version.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        status=ReleaseStatus.ACTIVE,
        created_by="user-1",
    )
    resolved = ResolvedAgentContract(
        logical_agent_id=version.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        version_id=version.id,
        release_id=release.id,
        release_status=release.status,
        manifest_hash=version.manifest_hash,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        capability_versions=version.capability_versions,
        input_schema_ref=_schema_ref("input"),
        output_schema_ref=_schema_ref("output"),
        artifact_metadata=version.artifact_metadata,
        protocol_version=AGENT_STUDIO_PROTOCOL_VERSION,
    )
    evaluation = EvaluationRecord(
        id="eval-1",
        version_id=version.id,
        evaluator="pytest",
        summary="Looks good.",
    )
    approval = StudioApprovalRecord(
        id="approval-1",
        version_id=version.id,
        tenant_id="tenant-1",
        project_id="project-1",
        kind=ApprovalKind.RELEASE_PROMOTION,
        gated_action="promote",
        destination="development",
        requested_by="user-1",
        evidence_summary="All checks passed.",
        risk="low",
        idempotency_key="approval-1",
    )
    deployment = DeploymentRecord(
        id="deployment-1",
        logical_agent_id=version.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        version_id=version.id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by="user-1",
    )
    logical_binding = LogicalAgentBinding(
        logical_agent_id=version.logical_agent_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version.id,
        updated_by="user-1",
    )

    assert runtime_selection.reasons == ("requires_custom_code",)
    assert tool_registration.kind is ToolRegistrationKind.CUSTOM_HANDLER
    assert workspace_connection.project_id == "project-1"
    assert memory_entry.ttl_days == 7
    assert memory_audit.action is MemoryAuditAction.EXPORT
    assert draft.etag
    assert lineage.relationship == "fork"
    assert version.protocol_version == AGENT_STUDIO_PROTOCOL_VERSION
    assert release.environment is DeploymentEnvironment.DEVELOPMENT
    assert resolved.artifact_metadata == version.artifact_metadata
    assert evaluation.advisory is True
    assert approval.state.value == "pending"
    assert deployment.health.status is HealthStatus.UNKNOWN
    assert logical_binding.environment is DeploymentEnvironment.DEVELOPMENT


def test_release_gate_report_accepts_not_applicable_and_blocks_skipped_or_failed() -> None:
    passing = ReleaseGateReport(
        id="report-pass",
        version_id="version-1",
        results=(
            GateResult(name=GateName.SCHEMA, status=GateStatus.PASSED),
            GateResult(name=GateName.BUILD, status=GateStatus.NOT_APPLICABLE),
        ),
    )
    blocking = ReleaseGateReport(
        id="report-block",
        version_id="version-1",
        results=(
            GateResult(name=GateName.TEST, status=GateStatus.SKIPPED, detail="No test evidence supplied."),
            GateResult(name=GateName.SECURITY, status=GateStatus.FAILED, detail="secret found"),
        ),
    )

    assert passing.passed
    assert passing.blocking_gates() == ()
    assert not blocking.passed
    assert tuple(result.name for result in blocking.blocking_gates()) == (
        GateName.TEST,
        GateName.SECURITY,
    )
