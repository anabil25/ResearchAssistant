"""Deterministically compile a governed Studio manifest into a prompt agent.

The compiler intentionally supports only tool configurations whose SDK shape
and prerequisites are represented in the manifest. A capability is never
silently omitted or translated from arbitrary client data.
"""

from __future__ import annotations

from typing import Any

from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchTool,
    AzureAISearchToolResource,
    CodeInterpreterTool,
    FileSearchTool,
    PromptAgentDefinition,
)

from research_assistant_api.agent_studio.models import AgentManifest, CapabilityBinding


class PromptAgentCompilationError(ValueError):
    """Raised when a governed manifest has no safe prompt-agent translation."""


def _reject_unknown_config(binding: CapabilityBinding, allowed: frozenset[str]) -> None:
    unexpected = sorted(set(binding.config).difference(allowed))
    if unexpected:
        raise PromptAgentCompilationError(
            f"Capability '{binding.descriptor_ref.id}' has unsupported configuration keys: "
            f"{', '.join(unexpected)}."
        )


def _compile_file_search(binding: CapabilityBinding) -> FileSearchTool:
    _reject_unknown_config(binding, frozenset({"vector_store_ids", "max_num_results"}))
    vector_store_ids = binding.config.get("vector_store_ids")
    if (
        not isinstance(vector_store_ids, list)
        or not vector_store_ids
        or not all(isinstance(item, str) and item for item in vector_store_ids)
    ):
        raise PromptAgentCompilationError(
            "File Search requires a non-empty vector_store_ids list from an approved project instance."
        )
    max_num_results = binding.config.get("max_num_results")
    if max_num_results is not None and (
        not isinstance(max_num_results, int) or not 1 <= max_num_results <= 50
    ):
        raise PromptAgentCompilationError("File Search max_num_results must be an integer from 1 through 50.")
    return FileSearchTool(vector_store_ids=vector_store_ids, max_num_results=max_num_results)


def _compile_azure_ai_search(binding: CapabilityBinding) -> AzureAISearchTool:
    _reject_unknown_config(binding, frozenset({"index_name", "query_type", "top_k", "filter"}))
    connection_id = binding.connection_ref.id if binding.connection_ref is not None else None
    index_name = binding.config.get("index_name")
    if not isinstance(connection_id, str) or not connection_id:
        raise PromptAgentCompilationError("Azure AI Search requires an approved project connection.")
    if not isinstance(index_name, str) or not index_name:
        raise PromptAgentCompilationError("Azure AI Search requires an approved index_name.")
    query_type = binding.config.get("query_type")
    if query_type is not None and query_type not in {
        "simple",
        "semantic",
        "vector",
        "vector_simple_hybrid",
        "vector_semantic_hybrid",
    }:
        raise PromptAgentCompilationError("Azure AI Search query_type is not supported by the Foundry SDK.")
    top_k = binding.config.get("top_k")
    if top_k is not None and (not isinstance(top_k, int) or not 1 <= top_k <= 50):
        raise PromptAgentCompilationError("Azure AI Search top_k must be an integer from 1 through 50.")
    filter_value = binding.config.get("filter")
    if filter_value is not None and not isinstance(filter_value, str):
        raise PromptAgentCompilationError("Azure AI Search filter must be a string.")
    index = AISearchIndexResource(
        project_connection_id=connection_id,
        index_name=index_name,
        query_type=query_type,
        top_k=top_k,
        filter=filter_value,
    )
    return AzureAISearchTool(azure_ai_search=AzureAISearchToolResource(indexes=[index]))


def compile_prompt_agent_definition(manifest: AgentManifest) -> PromptAgentDefinition:
    """Compile one manifest into an SDK prompt-agent definition without I/O."""
    model = manifest.model_deployment
    if model is None:
        raise PromptAgentCompilationError("Prompt agents require a project-deployed model.")

    tools: list[Any] = []
    for binding in manifest.capabilities:
        descriptor_id = binding.descriptor_ref.id
        operation = binding.operation_ref.id
        if descriptor_id == "foundry.code_interpreter" and operation == "run":
            _reject_unknown_config(binding, frozenset())
            tools.append(CodeInterpreterTool())
        elif descriptor_id == "foundry.file_search" and operation == "search":
            tools.append(_compile_file_search(binding))
        elif descriptor_id == "foundry.azure_ai_search" and operation == "search":
            tools.append(_compile_azure_ai_search(binding))
        elif descriptor_id == "foundry.web_search":
            raise PromptAgentCompilationError(
                "Web Search is preview in current Foundry guidance and is not enabled by the GA-only policy."
            )
        elif descriptor_id == "foundry.toolbox_connector":
            raise PromptAgentCompilationError(
                "Toolbox Connector requires a current platform-approved preview exception before publication."
            )
        else:
            raise PromptAgentCompilationError(
                f"Capability '{descriptor_id}.{operation}' has no supported prompt-agent compiler mapping."
            )

    return PromptAgentDefinition(
        model=model.deployment_name,
        instructions=manifest.instructions,
        tools=tools or None,
    )