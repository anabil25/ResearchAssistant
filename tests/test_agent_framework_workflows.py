from __future__ import annotations

import pytest
from research_assistant_api.workflow_compiler import (
    _passthrough,
    compile_agent_framework_workflow,
)
from research_assistant_core.v3_contracts import (
    WorkflowDefinitionV3,
    WorkflowEdgeV3,
    WorkflowNodeKind,
    WorkflowNodeV3,
    WorkflowPortV3,
    WorkflowTriggerV3,
)


def test_v3_graph_compiles_to_agent_framework_workflow() -> None:
    artifact = WorkflowPortV3(
        id="artifact",
        schema_ref="#/$defs/ResearchArtifactV3",
    )
    definition = WorkflowDefinitionV3(
        id="workflow-literature-review",
        version="3.0.0",
        name="Literature review",
        trigger=WorkflowTriggerV3(kind="manual"),
        nodes=[
            WorkflowNodeV3(
                id="literature",
                label="Literature Studio",
                kind=WorkflowNodeKind.STUDIO,
                outputs=[artifact],
            ),
            WorkflowNodeV3(
                id="approval",
                label="Human approval",
                kind=WorkflowNodeKind.APPROVAL,
                inputs=[artifact],
                approval_required=True,
            ),
        ],
        edges=[
            WorkflowEdgeV3(
                id="literature-to-approval",
                source_node_id="literature",
                source_port_id="artifact",
                target_node_id="approval",
                target_port_id="artifact",
            )
        ],
        execution_mode="durable_agent_framework",
    )

    workflow = compile_agent_framework_workflow(definition)

    assert workflow.name == "Literature review"
    assert workflow.start_executor_id == "literature"
    assert {executor.id for executor in workflow.get_executors_list()} == {
        "literature",
        "approval",
    }
    assert workflow.graph_signature_hash


def test_passthrough_executor_keeps_message_identity() -> None:
    payload = {"message": "hello"}

    assert _passthrough(payload) is payload


def test_v3_graph_with_multiple_roots_adds_synthetic_start_executor() -> None:
    artifact = WorkflowPortV3(
        id="artifact",
        schema_ref="#/$defs/ResearchArtifactV3",
    )
    definition = WorkflowDefinitionV3(
        id="workflow-two-roots",
        version="3.0.0",
        name="Two roots",
        trigger=WorkflowTriggerV3(kind="manual"),
        nodes=[
            WorkflowNodeV3(
                id="literature",
                label="Literature",
                kind=WorkflowNodeKind.STUDIO,
                outputs=[artifact],
            ),
            WorkflowNodeV3(
                id="grant",
                label="Grant",
                kind=WorkflowNodeKind.STUDIO,
                outputs=[artifact],
            ),
        ],
        edges=[],
        execution_mode="durable_agent_framework",
    )

    workflow = compile_agent_framework_workflow(definition)

    assert workflow.name == "Two roots"
    assert workflow.start_executor_id == "workflow-start"
    assert {executor.id for executor in workflow.get_executors_list()} == {
        "workflow-start",
        "literature",
        "grant",
    }


def test_v3_graph_requires_at_least_one_root() -> None:
    artifact = WorkflowPortV3(
        id="artifact",
        schema_ref="#/$defs/ResearchArtifactV3",
    )
    definition = WorkflowDefinitionV3.model_construct(
        id="workflow-cycle",
        version="3.0.0",
        name="Cycle",
        trigger=WorkflowTriggerV3(kind="manual"),
        nodes=[
            WorkflowNodeV3(
                id="node-a",
                label="A",
                kind=WorkflowNodeKind.STUDIO,
                inputs=[artifact],
                outputs=[artifact],
            ),
            WorkflowNodeV3(
                id="node-b",
                label="B",
                kind=WorkflowNodeKind.APPROVAL,
                inputs=[artifact],
                outputs=[artifact],
            ),
        ],
        edges=[
            WorkflowEdgeV3(
                id="a-to-b",
                source_node_id="node-a",
                source_port_id="artifact",
                target_node_id="node-b",
                target_port_id="artifact",
            ),
            WorkflowEdgeV3(
                id="b-to-a",
                source_node_id="node-b",
                source_port_id="artifact",
                target_node_id="node-a",
                target_port_id="artifact",
            ),
        ],
        execution_mode="durable_agent_framework",
    )

    with pytest.raises(
        ValueError,
        match="must contain at least one root",
    ):
        compile_agent_framework_workflow(definition)
