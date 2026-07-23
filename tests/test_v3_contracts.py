from __future__ import annotations

from pydantic import HttpUrl, ValidationError
from research_assistant_core.models import Capability, RunStatus
from research_assistant_core.v3_contracts import (
    ApprovalRequestV3,
    ConnectorAuthMode,
    ConnectorContractV3,
    ConnectorLifecycle,
    ConnectorProtocol,
    DatasetExecutionV3,
    EvidenceReferenceV3,
    EvidenceResolution,
    MatchCandidateV3,
    MatchEntityType,
    MatchScoreComponentV3,
    RunContractV3,
    ToolboxBindingV3,
    WorkflowDefinitionV3,
    WorkflowEdgeV3,
    WorkflowNodeKind,
    WorkflowNodeV3,
    WorkflowPortV3,
    WorkflowTriggerV3,
    v3_contract_bundle,
)


def test_v3_contract_bundle_contains_every_platform_boundary() -> None:
    schemas = v3_contract_bundle()["schemas"]

    assert {
        "ResearchArtifactV3",
        "LiteratureWorkspaceV3",
        "GrantWorkspaceV3",
        "MatchingWorkspaceV3",
        "DatasetWorkspaceV3",
        "InstitutionalWorkspaceV3",
        "ConnectorContractV3",
        "WorkflowDefinitionV3",
        "ApprovalRequestV3",
        "RunContractV3",
    }.issubset(schemas)


def test_verified_evidence_requires_resolved_provenance() -> None:
    try:
        EvidenceReferenceV3(
            id="evidence-test-record",
            source_id="source-1",
            title="Test source",
            resolution=EvidenceResolution.VERIFIED,
        )
    except ValidationError as exc:
        assert "requires chunk, citation, quote, and checksum" in str(exc)
    else:
        raise AssertionError("Verified evidence without provenance must be rejected.")


def test_active_connector_requires_immutable_gateway_bindings() -> None:
    try:
        ConnectorContractV3(
            id="grant_source",
            name="Grant source",
            protocol=ConnectorProtocol.OPENAPI,
            auth_mode=ConnectorAuthMode.MANAGED_IDENTITY,
            lifecycle=ConnectorLifecycle.ACTIVE,
            allowed_hosts=["api.example.gov"],
            allowed_path_prefixes=["/v1/opportunities"],
            terms_url=HttpUrl("https://example.gov/terms"),
            license_summary="Public metadata under provider terms.",
            assigned_capabilities=[Capability.GRANT],
        )
    except ValidationError as exc:
        assert "requires APIM, MCP, and Toolbox" in str(exc)
    else:
        raise AssertionError("An active connector without immutable gateway bindings must be rejected.")

    connector = ConnectorContractV3(
        id="grant_source",
        name="Grant source",
        protocol=ConnectorProtocol.OPENAPI,
        auth_mode=ConnectorAuthMode.MANAGED_IDENTITY,
        lifecycle=ConnectorLifecycle.ACTIVE,
        allowed_hosts=["api.example.gov"],
        allowed_path_prefixes=["/v1/opportunities"],
        terms_url=HttpUrl("https://example.gov/terms"),
        license_summary="Public metadata under provider terms.",
        apim_api_version="v1",
        apim_revision="2",
        mcp_server_version="v1",
        toolbox_binding=ToolboxBindingV3(
            toolbox_name="grant-studio",
            toolbox_version="4",
            server_label="grant_source",
            require_approval="never",
            default_version=True,
        ),
        assigned_capabilities=[Capability.GRANT],
    )

    assert connector.toolbox_binding is not None
    assert connector.toolbox_binding.default_version is True


def test_workflow_contract_rejects_unknown_ports_and_cycles() -> None:
    output = WorkflowPortV3(id="result", schema_ref="#/$defs/ResearchArtifactV3")
    input_port = WorkflowPortV3(id="artifact", schema_ref="#/$defs/ResearchArtifactV3")
    first = WorkflowNodeV3(
        id="first",
        label="First",
        kind=WorkflowNodeKind.STUDIO,
        inputs=[input_port],
        outputs=[output],
    )
    second = WorkflowNodeV3(
        id="second",
        label="Second",
        kind=WorkflowNodeKind.APPROVAL,
        inputs=[input_port],
        outputs=[output],
        approval_required=True,
    )
    valid = WorkflowDefinitionV3(
        id="workflow-evidence-review",
        version="3.0.0",
        name="Evidence review",
        trigger=WorkflowTriggerV3(kind="manual"),
        nodes=[first, second],
        edges=[
            WorkflowEdgeV3(
                id="first-to-second",
                source_node_id="first",
                source_port_id="result",
                target_node_id="second",
                target_port_id="artifact",
            )
        ],
        execution_mode="agent_framework",
    )

    assert valid.edges[0].target_node_id == "second"

    try:
        WorkflowDefinitionV3(
            id="workflow-cyclic-review",
            version="3.0.0",
            name="Cyclic review",
            trigger=WorkflowTriggerV3(kind="manual"),
            nodes=[first, second],
            edges=[
                WorkflowEdgeV3(
                    id="first-to-second",
                    source_node_id="first",
                    source_port_id="result",
                    target_node_id="second",
                    target_port_id="artifact",
                ),
                WorkflowEdgeV3(
                    id="second-to-first",
                    source_node_id="second",
                    source_port_id="result",
                    target_node_id="first",
                    target_port_id="artifact",
                ),
            ],
            execution_mode="durable_agent_framework",
        )
    except ValidationError as exc:
        assert "must be acyclic" in str(exc)
    else:
        raise AssertionError("A cyclic workflow must be rejected.")


def test_matching_score_and_execution_credentials_are_deterministic() -> None:
    try:
        MatchCandidateV3(
            id="person-1",
            name="Researcher One",
            entity_type=MatchEntityType.PERSON,
            hard_filters_passed=True,
            score=90,
            components=[
                MatchScoreComponentV3(
                    criterion_id="expertise",
                    label="Expertise",
                    weight=1,
                    match=0.8,
                    contribution=80,
                    evidence_ids=["evidence-person-1"],
                )
            ],
            evidence_ids=["evidence-person-1"],
            availability="unknown",
        )
    except ValidationError as exc:
        assert "must equal the sum" in str(exc)
    else:
        raise AssertionError("A model-supplied score that disagrees with its components must be rejected.")

    execution = DatasetExecutionV3(
        status="planned",
    )
    assert execution.tool_name == "code_interpreter"
    assert execution.project_scoped_context is True


def test_approval_and_run_contract_capture_exact_audit_boundary() -> None:
    approval = ApprovalRequestV3(
        id="approval-release-report",
        run_id="run-1",
        action="Release dataset report version 3.",
        destination="Workspace Library",
        payload_digest=f"sha256:{'a' * 64}",
        evidence_ids=["evidence-dataset-1"],
        policy_reason="Publishing a shared artifact requires review.",
        requested_by="dataset-agent",
    )
    run = RunContractV3(
        id="run-1",
        tenant_id="demo",
        project_id="demo-project",
        capability=Capability.DATASET,
        status=RunStatus.WAITING_FOR_APPROVAL,
        progress=80,
        execution_mode="durable_agent_framework",
        trace_id="trace-1",
        pending_approval_ids=[approval.id],
    )

    assert run.pending_approval_ids == ["approval-release-report"]
