from __future__ import annotations

from agent_framework import FunctionExecutor, Workflow, WorkflowBuilder
from research_assistant_core.v3_contracts import WorkflowDefinitionV3


def _passthrough(message: object) -> object:
    return message


def compile_agent_framework_workflow(
    definition: WorkflowDefinitionV3,
) -> Workflow:
    executors = {
        node.id: FunctionExecutor(
            _passthrough,
            id=node.id,
            input=object,
            output=object,
            workflow_output=object,
        )
        for node in definition.nodes
    }
    incoming = {node.id: 0 for node in definition.nodes}
    for edge in definition.edges:
        incoming[edge.target_node_id] += 1
    roots = [node_id for node_id, count in incoming.items() if count == 0]
    if not roots:
        raise ValueError("Workflow must contain at least one root node.")
    if len(roots) == 1:
        start = executors[roots[0]]
        builder = WorkflowBuilder(
            name=definition.name,
            start_executor=start,
            output_from="all",
        )
    else:
        start = FunctionExecutor(
            _passthrough,
            id="workflow-start",
            input=object,
            output=object,
            workflow_output=object,
        )
        builder = WorkflowBuilder(
            name=definition.name,
            start_executor=start,
            output_from="all",
        )
        for root_id in roots:
            builder.add_edge(start, executors[root_id])
    for edge in definition.edges:
        builder.add_edge(
            executors[edge.source_node_id],
            executors[edge.target_node_id],
        )
    return builder.build()
