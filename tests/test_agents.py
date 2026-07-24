from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from openai import APIStatusError
from shared.errors import ConfigurationError
from shared.profiles import get_profile, list_profiles
from shared.settings import HarnessSettings
from shared.tools import _invoke_specialist, delegated_agent_name, tools_for_profile

from scripts.build_agent_source_bundle import source_bundle_hash

ROOT = Path(__file__).parents[1]
TEST_SOURCE_BUNDLE_HASH = source_bundle_hash((("fixture.py", b"VALUE = 1\n"),))


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
    for profile in list_profiles():
        assert "Evidence over fluency" in profile.instructions
        assert "untrusted data" in profile.instructions
        assert profile.workflow_steps
        assert profile.output_contract.endswith("V2")
        assert profile.schema_version == "2.0"
        assert profile.behavior_version == "3.0.0"
        expected_model = "gpt-5.6-sol" if profile.id in {"literature", "grant"} else "gpt-5.4-mini"
        assert profile.model_policy.deployment_name == expected_model
        assert profile.model_policy.pinned_model_version
        assert profile.runtime_requirements.selected_runtime == "custom"
        assert profile.knowledge_bindings
        assert profile.evidence_policy.output_schema == profile.output_schema
        assert profile.artifact_policy.output_schema == profile.output_schema
        assert profile.artifact_policy.provenance_required is True
        for binding in profile.capability_bindings:
            assert binding.operation_ref.id
            assert binding.instance_ref
            assert len(binding.instance_ref.fingerprint) == 64
            assert binding.instance_ref.fingerprint == binding.instance_ref.fingerprint.lower()
            assert binding.instance_ref.discovered_provider_version
            assert binding.instance_ref.discovered_resource_version
            assert (
                binding.instance_ref.discovered_provider_version
                != binding.instance_ref.discovered_resource_version
            )
            assert binding.operation_ref.input_schema_digest
            assert binding.operation_ref.output_schema_digest
            assert binding.connection_ref.id
            assert binding.policy_ref.id
        if any(binding.operation_ref.id.startswith("foundry.toolbox.") for binding in profile.capability_bindings):
            with pytest.raises(ConfigurationError, match="Toolbox"):
                tools_for_profile(profile)
        else:
            assert tools_for_profile(profile) == []
    assert len({profile.output_contract for profile in profiles}) == len(profiles)
    assert len({profile.workflow_steps for profile in profiles}) == len(profiles)
    coordinator = get_profile("coordinator")
    assert coordinator.specialist_policy is not None
    assert len(coordinator.specialist_policy.specialists) == 8


def test_missing_toolbox_never_falls_back_to_web_search() -> None:
    class FakeClient:
        def get_web_search_tool(self, **kwargs: Any) -> dict[str, Any]:
            return {"kind": "web_search", **kwargs}

    for profile in list_profiles():
        requires_toolbox = any(
            binding.operation_ref.id.startswith("foundry.toolbox.") for binding in profile.capability_bindings
        )
        if requires_toolbox:
            with pytest.raises(ConfigurationError, match="Toolbox"):
                tools_for_profile(profile, FakeClient())
        else:
            assert tools_for_profile(profile, FakeClient()) == []


def test_toolbox_bindings_match_deployed_operation_names() -> None:
    expected = {
        "dataset": {"code_interpreter"},
        "literature_online": {"web_search", "searchLiteratureMetadata"},
        "grant_online": {"web_search", "searchGrantOpportunities"},
        "matching_online": {"web_search", "searchMatchingMetadata"},
    }
    for profile_id, tool_names in expected.items():
        manifest = get_profile(profile_id)
        assert {binding.operation_ref.id.rsplit(".", 1)[-1] for binding in manifest.capability_bindings} == tool_names


def test_coordinator_routes_public_only_to_online_specialists() -> None:
    assert delegated_agent_name("literature", "public") == "literature-online-agent"
    assert delegated_agent_name("literature", "internal") == "literature-agent"
    assert delegated_agent_name("grant", "confidential") == "grant-agent"
    assert delegated_agent_name("institutional_qa", "restricted") == "institution-agent"


def test_azure_manifest_uses_current_hosted_agent_contract() -> None:
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    services = manifest["services"]
    assert manifest["hooks"]["predeploy"] == {
        "windows": {
            "shell": "pwsh",
            "run": "./scripts/predeploy.ps1",
        },
        "posix": {
            "shell": "sh",
            "run": "./scripts/predeploy.sh",
        },
    }
    agent_ignore = (ROOT / "agents" / ".agentignore").read_text(encoding="utf-8")
    assert "!.release/source-bundle.json" in agent_ignore
    agent_services = {name: config for name, config in services.items() if config.get("host") == "azure.ai.agent"}

    assert len(agent_services) == 9
    for name, config in agent_services.items():
        assert config["kind"] == "hosted", name
        assert config["codeConfiguration"]["runtime"] == "python_3_13"
        assert config["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
        assert "agent_reference" not in str(config)
        environment = {
            item["name"]: item["value"]
            for item in config["environmentVariables"]
        }
        assert environment["RESEARCH_WORKSPACE_TENANT_ID"] == "${AZURE_TENANT_ID}", name
        assert environment["RESEARCH_WORKSPACE_PROJECT_ID"] == "${AZURE_AI_PROJECT_NAME}", name
        entry_point = ROOT / "agents" / config["codeConfiguration"]["entryPoint"]
        source = entry_point.read_text(encoding="utf-8")
        assert "sys.path.insert" in source, name
        assert source.index("sys.path.insert") < source.index("factory import run"), name
    toolbox_variables = {
        name: next(
            (item["value"] for item in config["environmentVariables"] if item["name"] == "TOOLBOX_ENDPOINT"),
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
    deployed_versions = {
        deployment["name"]: deployment["model"]["version"] for deployment in services["ai-project"]["deployments"]
    }
    service_by_agent_name = {config["name"]: config for config in agent_services.values()}
    for profile in list_profiles():
        service = service_by_agent_name[profile.name]
        selected_model = next(
            item["value"]
            for item in service["environmentVariables"]
            if item["name"] == "AZURE_AI_MODEL_DEPLOYMENT_NAME"
        )
        assert profile.model_policy.deployment_name == selected_model
        assert profile.model_policy.pinned_model_version == deployed_versions[selected_model]


def test_online_agents_use_foundry_toolbox_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda *_args, **_kwargs: marker)
    settings = HarnessSettings.model_validate(
        {
            "foundry_project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
            "model_deployment_name": "gpt-5.4-mini",
            "source_bundle_hash": TEST_SOURCE_BUNDLE_HASH,
            "toolbox_endpoint": "https://foundry.example/toolboxes/test/mcp?api-version=v1",
        }
    )
    assert tools_for_profile(get_profile("grant_online"), settings=settings) is marker
    assert tools_for_profile(get_profile("dataset"), settings=settings) is marker


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
    assert container_apps.count(
        "name: 'RESEARCH_REQUIRE_APPROVAL_CONTEXT_RESOLVER'"
    ) == 1
    assert (
        "name: 'RESEARCH_REQUIRE_APPROVAL_CONTEXT_RESOLVER'\n"
        "              value: 'true'"
    ) in container_apps


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
    powershell = (ROOT / "scripts" / "preprovision.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "scripts" / "preprovision.sh").read_text(encoding="utf-8")

    assert "cognitiveservices usage list" in powershell
    assert "deployment.sku.capacity" in powershell
    assert "cognitiveservices usage list" in posix
    assert 'needed="$capacity"' in posix
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

    class Client:
        responses = Responses()

        def with_options(self, *, max_retries: int) -> Client:
            assert max_retries == 0
            return self

    monkeypatch.setattr("shared.invocation.time.sleep", sleeps.append)

    output = _invoke_specialist(
        Client(),
        "Analyze supplied evidence.",
        "literature-agent",
    )

    assert output == "Bounded specialist analysis"
    assert attempts == 3
    assert sleeps == [15, 2]


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
