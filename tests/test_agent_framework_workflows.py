from __future__ import annotations

from research_assistant_api.workflow_compiler import (
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
