from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agent_framework import WorkflowAgent
from agent_framework_foundry_hosting import ResponsesHostServer
from coordinator.factory import MANIFEST
from coordinator.factory import build_agent as build_coordinator
from shared.contracts import SpecialistRequest, SpecialistResult
from shared.factory import get_factory
from shared.settings import HarnessSettings

ROOT = Path(__file__).parents[1]


def _settings(*, toolbox_endpoint: str | None = None) -> HarnessSettings:
    return HarnessSettings(
        foundry_project_endpoint="https://project.example",
        model_deployment_name="gpt-5.4-mini",
        model_deployment_version="2026-03-17",
        source_tree_digest="0" * 64,
        deployment_tenant_id="tenant-a",
        deployment_project_id="project-a",
        toolbox_endpoint=toolbox_endpoint,
    )


async def _specialist_invoker(request: SpecialistRequest) -> SpecialistResult:
    return SpecialistResult(
        request_id=request.request_id,
        capability=request.capability,
        agent_name=request.target_agent,
        error_code="not_configured",
    )


def test_coordinator_starts_without_custom_provider_or_release_attestation() -> None:
    agent = build_coordinator(
        settings=_settings(),
        invoker=_specialist_invoker,
        trusted_tenant_id="tenant-a",
        trusted_project_id="project-a",
    )

    assert isinstance(agent, WorkflowAgent)
    assert agent.name == MANIFEST.name
    assert ResponsesHostServer(agent, configure_observability=None) is not None


def test_toolbox_agent_starts_without_custom_provider_or_release_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toolbox = SimpleNamespace()
    constructed: list[dict[str, object]] = []
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda *_args, **_kwargs: toolbox)
    monkeypatch.setattr(
        "shared.factory.Agent",
        lambda **kwargs: constructed.append(kwargs) or SimpleNamespace(**kwargs),
    )

    agent = get_factory("dataset").build_hosted(
        client=object(),
        settings=_settings(toolbox_endpoint="https://toolbox.example/mcp"),
        trusted_tenant_id="tenant-a",
        trusted_project_id="project-a",
    )

    assert agent.name == "dataset-agent"
    assert constructed[0]["tools"] is toolbox


def test_hosted_agents_use_platform_managed_tool_configuration(
    azure_manifest: dict[str, Any],
) -> None:
    hosted_services = (
        service
        for service in azure_manifest["services"].values()
        if service.get("host") == "azure.ai.agent"
    )

    assert all(
        "RESEARCH_ALLOW_POC_RUNTIME"
        not in {item["name"] for item in service["environmentVariables"]}
        for service in hosted_services
    )