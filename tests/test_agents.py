from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from openai import APIStatusError
from shared.profiles import get_profile, list_profiles
from shared.tools import (
    _invoke_specialist,
    build_delegate_tool,
    delegated_agent_name,
    tools_for_profile,
)

ROOT = Path(__file__).parents[1]


def test_offline_and_public_online_agent_profiles_are_packaged() -> None:
    profiles = list_profiles()

    assert {profile.id for profile in profiles} == {
        "coordinator",
        "literature",
        "grant",
        "matching",
        "dataset",
        "institution",
        "literature_online",
        "grant_online",
        "matching_online",
    }
    for profile in profiles:
        assert "Evidence over fluency" in profile.instructions
        assert "untrusted data" in profile.instructions
        assert profile.workflow_steps
        assert profile.output_contract.endswith("V2")
        expected_tools = 1 if profile.id == "coordinator" else 0
        assert len(tools_for_profile(profile)) == expected_tools
    assert len({profile.output_contract for profile in profiles}) == len(profiles)
    assert len({profile.workflow_steps for profile in profiles}) == len(profiles)


def test_web_search_is_enabled_only_for_current_source_agents() -> None:
    class FakeClient:
        def get_web_search_tool(self, **kwargs: Any) -> dict[str, Any]:
            return {"kind": "web_search", **kwargs}

    enabled = {"literature_online", "grant_online", "matching_online"}
    for profile in list_profiles():
        tools = tools_for_profile(profile, FakeClient())
        has_web = any(isinstance(item, dict) and item.get("kind") == "web_search" for item in tools)
        assert has_web is (profile.id in enabled)
        expected_count = 1 if profile.id == "coordinator" or profile.id in enabled else 0
        assert len(tools) == expected_count


def test_coordinator_routes_public_only_to_online_specialists() -> None:
    assert delegated_agent_name("literature", "public") == "literature-online-agent"
    assert delegated_agent_name("literature", "internal") == "literature-agent"
    assert delegated_agent_name("grant", "confidential") == "grant-agent"
    assert delegated_agent_name("institutional_qa", "restricted") == "institution-agent"
    assert delegated_agent_name("unsupported", "public") is None


def test_azure_manifest_uses_current_hosted_agent_contract() -> None:
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    services = manifest["services"]
    agent_services = {name: config for name, config in services.items() if config.get("host") == "azure.ai.agent"}

    assert len(agent_services) == 9
    for name, config in agent_services.items():
        assert config["kind"] == "hosted", name
        assert config["codeConfiguration"]["runtime"] == "python_3_13"
        assert config["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
        assert "agent_reference" not in str(config)
        entry_point = ROOT / "agents" / config["codeConfiguration"]["entryPoint"]
        source = entry_point.read_text(encoding="utf-8")
        assert "sys.path.insert" in source, name
        assert source.index("sys.path.insert") < source.index(
            "from shared.runtime import run_profile"
        ), name
    toolbox_variables = {
        name: next(
            (
                item["value"]
                for item in config["environmentVariables"]
                if item["name"] == "TOOLBOX_ENDPOINT"
            ),
            None,
        )
        for name, config in agent_services.items()
        if name.endswith("-online-agent") or name == "dataset-agent"
    }
    assert toolbox_variables == {
        "literature-online-agent": "${TOOLBOX_LITERATURE_MCP_ENDPOINT}",
        "grant-online-agent": "${TOOLBOX_GRANT_MCP_ENDPOINT}",
        "matching-online-agent": "${TOOLBOX_MATCHING_MCP_ENDPOINT}",
        "dataset-agent": "${TOOLBOX_DATASET_MCP_ENDPOINT}",
    }


def test_online_agents_use_foundry_toolbox_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    monkeypatch.setenv("TOOLBOX_ENDPOINT", "https://foundry.example/toolboxes/test/mcp?api-version=v1")
    monkeypatch.setattr("shared.tools.get_credential", lambda: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda _credential: marker)

    assert tools_for_profile(get_profile("grant_online")) is marker
    assert tools_for_profile(get_profile("dataset")) is marker


def test_bicep_model_parameters_match_azure_manifest() -> None:
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))

    assert parameters["parameters"]["deployments"]["value"] == (manifest["services"]["ai-project"]["deployments"])


def test_accelerator_infrastructure_has_no_region_or_migration_pin() -> None:
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(encoding="utf-8")
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")

    assert "searchLocation" not in parameters["parameters"]
    assert "-v2" not in container_apps
    assert "keyVault" not in resources


def test_accelerator_private_data_and_ci_principal_contracts() -> None:
    storage = (ROOT / "infra" / "modules" / "storage.bicep").read_text(encoding="utf-8")
    cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
    search = (ROOT / "infra" / "modules" / "search.bicep").read_text(encoding="utf-8")

    assert "publicNetworkAccess: 'Disabled'" in storage
    assert "publicNetworkAccess: 'Disabled'" in cosmos
    assert "param principalType string = 'User'" in storage
    assert "param principalType string = 'User'" in search
    assert "principalType: principalType" in storage
    assert search.count("principalType: principalType") == 2


def test_preprovision_checks_requested_model_capacity() -> None:
    powershell = (ROOT / "scripts" / "preprovision.ps1").read_text(
        encoding="utf-8"
    )
    posix = (ROOT / "scripts" / "preprovision.sh").read_text(encoding="utf-8")

    assert "cognitiveservices usage list" in powershell
    assert "deployment.sku.capacity" in powershell
    assert "cognitiveservices usage list" in posix
    assert "needed=\"$capacity\"" in posix
    assert "azd env set AZURE_PRINCIPAL_ID" in powershell
    assert "azd env set AZURE_PRINCIPAL_TYPE" in powershell
    assert "azd env set AZURE_PRINCIPAL_ID" in posix
    assert "azd env set AZURE_PRINCIPAL_TYPE" in posix


def test_accelerator_uses_one_environment_scoped_durable_task_hub() -> None:
    module = (ROOT / "infra" / "modules" / "durable-task.bicep").read_text(encoding="utf-8")

    assert "resource legacyTaskHub" not in module
    assert "name: 'research'" in module
    assert "output taskHubName string = taskHub.name" in module


def test_coordinator_specialist_invocation_retries_transient_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps: list[int] = []

    class Responses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise APIStatusError(
                    "session not ready",
                    response=httpx.Response(
                        424,
                        request=httpx.Request(
                            "POST",
                            "https://foundry.example.test/responses",
                        ),
                    ),
                    body={"error": {"code": "session_not_ready"}},
                )
            if attempts == 2:
                return SimpleNamespace(output_text=" ")
            return SimpleNamespace(output_text="Bounded specialist analysis")

    monkeypatch.setattr("shared.tools.time.sleep", sleeps.append)

    output = _invoke_specialist(
        SimpleNamespace(responses=Responses()),
        "Analyze supplied evidence.",
        "literature-agent",
    )

    assert output == "Bounded specialist analysis"
    assert attempts == 3
    assert sleeps == [15, 2]


def test_specialist_invocation_exhausts_transient_and_empty_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotReadyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            raise APIStatusError(
                "session not ready",
                response=httpx.Response(
                    424,
                    request=httpx.Request(
                        "POST",
                        "https://foundry.example.test/responses",
                    ),
                ),
                body={"error": {"code": "session_not_ready"}},
            )

    monkeypatch.setattr("shared.tools.time.sleep", lambda _delay: None)
    with pytest.raises(APIStatusError):
        _invoke_specialist(
            SimpleNamespace(responses=NotReadyResponses()),
            "Analyze.",
            "dataset-agent",
        )

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(output_text=" ")

    with pytest.raises(RuntimeError, match="returned no output"):
        _invoke_specialist(
            SimpleNamespace(responses=EmptyResponses()),
            "Analyze.",
            "dataset-agent",
        )


def test_delegate_tool_rejects_invalid_routes_and_invokes_valid_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = build_delegate_tool()
    unsupported = json.loads(
        tool.func(
            capability="unsupported",
            request="Analyze.",
            sensitivity="public",
        )
    )
    assert unsupported["error"] == "unsupported_capability"

    invalid = json.loads(
        tool.func(
            capability="literature",
            request="Analyze.",
            sensitivity="invalid",
        )
    )
    assert invalid["error"] == "invalid_sensitivity"

    class Responses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(output_text="Verified delegation")

    class Project:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["endpoint"] == "https://foundry.example.test"
            assert kwargs["allow_preview"] is True

        def get_openai_client(self, *, agent_name: str) -> SimpleNamespace:
            assert agent_name == "literature-online-agent"
            return SimpleNamespace(responses=Responses())

    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example.test",
    )
    monkeypatch.setattr("shared.tools.get_credential", lambda: object())
    monkeypatch.setattr("shared.tools.AIProjectClient", Project)

    assert (
        tool.func(
            capability="literature",
            request="Analyze.",
            sensitivity="public",
        )
        == "Verified delegation"
    )


def test_coordinator_and_specialist_names_are_stable() -> None:
    assert get_profile("coordinator").name == "research-coordinator"
    assert get_profile("literature").name == "literature-agent"


def test_each_hosted_agent_has_a_smoke_evaluation_dataset() -> None:
    eval_directory = ROOT / "agents" / "evals"
    expected = {
        "coordinator",
        "literature",
        "grant",
        "matching",
        "dataset",
        "institution",
        "literature_online",
        "grant_online",
        "matching_online",
    }

    assert {path.stem for path in eval_directory.glob("*.jsonl")} == expected
    for path in eval_directory.glob("*.jsonl"):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) >= 3
        assert all(row.get("query") and row.get("expected_behavior") for row in rows)
