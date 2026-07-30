from __future__ import annotations

import pytest
from azure.ai.projects.models import AzureAISearchTool, CodeInterpreterTool, FileSearchTool
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    CapabilityBinding,
    CapabilityConnectionRef,
    CapabilityDescriptorRef,
    CapabilityOperationRef,
    ModelDeploymentRef,
)
from research_assistant_api.agent_studio.prompt_agent_compiler import (
    PromptAgentCompilationError,
    compile_prompt_agent_definition,
)


def _manifest(*, capabilities: tuple[CapabilityBinding, ...] = ()) -> AgentManifest:
    return AgentManifest(
        logical_agent_id="agent-prompt-compiler",
        tenant_id="demo",
        project_id="default",
        display_name="Prompt Compiler",
        instructions="Answer only from approved evidence.",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        model_deployment=ModelDeploymentRef(
            deployment_name="gpt-5.4-mini",
            model_name="gpt-5.4-mini",
            model_format="OpenAI",
        ),
        capabilities=capabilities,
    )


def _binding(
    descriptor_id: str,
    operation: str,
    *,
    config: dict[str, object] | None = None,
    connection_id: str | None = None,
) -> CapabilityBinding:
    return CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id=descriptor_id),
        operation_ref=CapabilityOperationRef(id=operation),
        connection_ref=CapabilityConnectionRef(id=connection_id) if connection_id else None,
        config=config or {},
        attached_by="user-1",
    )


def test_compiler_emits_model_instructions_and_supported_tools() -> None:
    definition = compile_prompt_agent_definition(
        _manifest(
            capabilities=(
                _binding("foundry.code_interpreter", "run"),
                _binding(
                    "foundry.file_search",
                    "search",
                    config={"vector_store_ids": ["vs-approved"], "max_num_results": 12},
                ),
                _binding(
                    "foundry.azure_ai_search",
                    "search",
                    config={"index_name": "research-evidence", "top_k": 5},
                    connection_id="conn-search",
                ),
            )
        )
    )

    assert definition.model == "gpt-5.4-mini"
    assert definition.instructions == "Answer only from approved evidence."
    assert definition.tools is not None
    code_interpreter, file_search, azure_ai_search = definition.tools
    assert isinstance(code_interpreter, CodeInterpreterTool)
    assert isinstance(file_search, FileSearchTool)
    assert file_search.vector_store_ids == ["vs-approved"]
    assert isinstance(azure_ai_search, AzureAISearchTool)
    assert azure_ai_search.azure_ai_search.indexes[0].project_connection_id == "conn-search"
    assert azure_ai_search.azure_ai_search.indexes[0].index_name == "research-evidence"


def test_compiler_rejects_manifest_without_project_model() -> None:
    manifest = _manifest().model_copy(update={"model_deployment": None})
    with pytest.raises(PromptAgentCompilationError, match="project-deployed model"):
        compile_prompt_agent_definition(manifest)


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        (_binding("foundry.file_search", "search"), "vector_store_ids"),
        (
            _binding(
                "foundry.azure_ai_search",
                "search",
                config={"index_name": "research-evidence"},
            ),
            "project connection",
        ),
        (_binding("foundry.web_search", "search"), "preview"),
        (_binding("foundry.toolbox_connector", "invoke"), "preview exception"),
        (_binding("foundry.function_calling", "invoke"), "no supported"),
    ],
)
def test_compiler_fails_closed_for_incomplete_or_unsupported_tools(
    binding: CapabilityBinding,
    message: str,
) -> None:
    with pytest.raises(PromptAgentCompilationError, match=message):
        compile_prompt_agent_definition(_manifest(capabilities=(binding,)))


def test_compiler_rejects_unknown_tool_configuration() -> None:
    with pytest.raises(PromptAgentCompilationError, match="unsupported configuration keys"):
        compile_prompt_agent_definition(
            _manifest(
                capabilities=(
                    _binding("foundry.code_interpreter", "run", config={"container_id": "untrusted"}),
                )
            )
        )