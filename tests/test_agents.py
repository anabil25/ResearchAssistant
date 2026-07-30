from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from openai import APIStatusError
from shared import credentials, runtime
from shared.profiles import get_profile, list_profiles
from shared.tools import (
    _invoke_specialist,
    build_delegate_tool,
    delegated_agent_name,
    tools_for_profile,
)
from shared.errors import ConfigurationError
from shared.settings import HarnessSettings
from shared.tools import _invoke_specialist, delegated_agent_name, tools_for_profile
from scripts.build_agent_source_tree import source_tree_digest

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
    assert parameters["parameters"]["location"]["value"] == "${AZURE_LOCATION}"
    assert parameters["parameters"]["resourceGroupName"]["value"] == "rg-${AZURE_ENV_NAME}"
    assert parameters["parameters"]["foundryProjectName"]["value"] == "${AZURE_ENV_NAME}"


def test_accelerator_infrastructure_has_no_region_or_migration_pin() -> None:
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(encoding="utf-8")
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")

    assert "searchLocation" not in parameters["parameters"]
    assert "-v2" not in container_apps
    # ``resources.bicep`` intentionally re-introduces a Key Vault module, but
    # only as an explicit, RBAC-authorized, opt-in delivery mechanism for the
    # Agent Studio release-attestation signing key (see
    # ``tests/test_apim_infrastructure.py::
    # test_resources_module_wires_key_vault_and_threads_entra_params_through``
    # and ``infra/modules/keyvault.bicep``). Guard against a regression back
    # to an unconditional/default template Key Vault by requiring exactly one
    # module declaration, gated on ``includeAttestationKeyVault`` (default
    # false), with every other reference limited to that same symbol's
    # conditional outputs.
    assert resources.count("module keyVault ") == 1
    assert "module keyVault 'keyvault.bicep' = if (includeAttestationKeyVault)" in resources
    assert all(
        "keyVault!.outputs." in line or "module keyVault " in line
        for line in resources.splitlines()
        if "keyVault" in line
    )


def test_accelerator_public_poc_data_and_ci_principal_contracts() -> None:
    storage = (ROOT / "infra" / "modules" / "storage.bicep").read_text(encoding="utf-8")
    cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
    search = (ROOT / "infra" / "modules" / "search.bicep").read_text(encoding="utf-8")

    assert "publicNetworkAccess: 'Enabled'" in storage
    assert "publicNetworkAccess: 'Enabled'" in cosmos
    assert "defaultAction: 'Allow'" in storage
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
    assert "azd env set AZURE_TENANT_ID" in powershell
    assert "azd env set AZURE_PRINCIPAL_ID" in posix
    assert "azd env set AZURE_PRINCIPAL_TYPE" in posix
    assert "azd env set AZURE_TENANT_ID" in posix
    assert "account get-access-token" in powershell
    assert "account get-access-token" in posix
    assert "az ad signed-in-user show" not in powershell
    assert "az ad signed-in-user show" not in posix


def test_accelerator_uses_one_environment_scoped_durable_task_hub() -> None:
    module = (ROOT / "infra" / "modules" / "durable-task.bicep").read_text(encoding="utf-8")

    assert "resource legacyTaskHub" not in module
    assert "name: 'research'" in module
    assert "output taskHubName string = taskHub.name" in module


def test_azd_up_deploys_every_service_sequentially() -> None:
    """One-click `azd up` must deploy services one per step.

    azd deploys in parallel by default, but the Foundry agent extension
    read-modify-writes azure.yaml per agent; concurrent agents truncate it and
    the deploy fails with "unable to parse azure.yaml file. File is empty."
    azd rewrites a plain ``azd: deploy api`` step into ``{args: [...]}``, so
    accept either spelling.
    """
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))

    steps = []
    for step in manifest["workflows"]["up"]["steps"]:
        command = step["azd"]
        steps.append(command.split() if isinstance(command, str) else command["args"])

    assert steps[0] == ["provision"]
    deployed = [service for verb, service in steps[1:] if verb == "deploy"]
    assert "--all" not in deployed
    assert len(deployed) == len(set(deployed))
    assert set(deployed) == set(manifest["services"])


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


def test_coordinator_specialist_invocation_rejects_non_retryable_and_empty_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr("shared.tools.time.sleep", sleeps.append)
    status_error = APIStatusError(
        "upstream failure",
        response=httpx.Response(
            500,
            request=httpx.Request(
                "POST",
                "https://foundry.example.test/responses",
            ),
        ),
        body={"error": {"code": "session_not_ready"}},
    )

    class FailedResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            raise status_error

    with pytest.raises(APIStatusError) as raised:
        _invoke_specialist(
            SimpleNamespace(responses=FailedResponses()),
            "Analyze supplied evidence.",
            "literature-agent",
        )
    assert raised.value is status_error
    assert sleeps == []

    attempts = 0

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            return SimpleNamespace(output_text=None)

    with pytest.raises(
        RuntimeError,
        match="Hosted specialist literature-agent returned no output after bounded retries",
    ):
        _invoke_specialist(
            SimpleNamespace(responses=EmptyResponses()),
            "Analyze supplied evidence.",
            "literature-agent",
        )
    assert attempts == 3
    assert sleeps == [2, 5]


def test_delegate_tool_validates_policy_before_remote_invocation() -> None:
    delegate = build_delegate_tool()

    unsupported = json.loads(
        delegate.func(
            capability="unknown",
            request="Analyze supplied evidence.",
            sensitivity="internal",
        )
    )
    assert unsupported == {
        "error": "unsupported_capability",
        "allowed": ["dataset", "grant", "institutional_qa", "literature", "matching"],
    }

    invalid_sensitivity = json.loads(
        delegate.func(
            capability="literature",
            request="Analyze supplied evidence.",
            sensitivity="secret",
        )
    )
    assert invalid_sensitivity == {
        "error": "invalid_sensitivity",
        "allowed": ["public", "internal", "confidential", "restricted"],
    }


def test_delegate_tool_constructs_the_bound_specialist_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = object()
    specialist_client = object()
    calls: dict[str, Any] = {}

    class FakeProjectClient:
        def __init__(
            self,
            *,
            endpoint: str,
            credential: object,
            allow_preview: bool,
        ) -> None:
            calls["project"] = (endpoint, credential, allow_preview)

        def get_openai_client(self, *, agent_name: str) -> object:
            calls["agent_name"] = agent_name
            return specialist_client

    def invoke_specialist(client: object, request: str, agent_name: str) -> str:
        calls["invocation"] = (client, request, agent_name)
        return "Bounded specialist analysis"

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setattr("shared.tools.get_credential", lambda: credential)
    monkeypatch.setattr("shared.tools.AIProjectClient", FakeProjectClient)
    monkeypatch.setattr("shared.tools._invoke_specialist", invoke_specialist)

    result = build_delegate_tool().func(
        capability="literature",
        request="Analyze supplied evidence.",
        sensitivity="internal",
    )

    assert result == "Bounded specialist analysis"
    assert calls == {
        "project": ("https://foundry.example.test", credential, True),
        "agent_name": "literature-agent",
        "invocation": (
            specialist_client,
            "Analyze supplied evidence.",
            "literature-agent",
        ),
    }


def test_coordinator_and_specialist_names_are_stable() -> None:
    assert get_profile("coordinator").name == "research-coordinator"
    assert get_profile("literature").name == "literature-agent"


def test_unknown_agent_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown research agent profile"):
        get_profile("not-a-profile")


def test_agent_credential_selection_is_environment_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_calls: list[str | None] = []
    managed = object()
    default = object()

    def build_managed_credential(*, client_id: str | None) -> object:
        managed_calls.append(client_id)
        return managed

    monkeypatch.setattr(
        credentials,
        "ManagedIdentityCredential",
        build_managed_credential,
    )
    monkeypatch.setattr(credentials, "DefaultAzureCredential", lambda: default)
    for name in ("AZURE_CLIENT_ID", "IDENTITY_ENDPOINT", "MSI_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)

    assert credentials.get_credential() is default

    monkeypatch.setenv("AZURE_CLIENT_ID", "managed-client")
    assert credentials.get_credential() is managed
    assert managed_calls == ["managed-client"]

    monkeypatch.delenv("AZURE_CLIENT_ID")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://identity.test")
    assert credentials.get_credential() is managed
    assert managed_calls[-1] is None


def test_agent_runtime_builds_and_hosts_the_selected_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[bool] = []
    hosted: list[object] = []
    built_agents: list[dict[str, Any]] = []
    client = object()
    tool = object()
    profile = get_profile("dataset")
    monkeypatch.setattr(runtime, "load_dotenv", lambda *, override: loaded.append(override))
    monkeypatch.setattr(runtime, "tools_for_profile", lambda selected, selected_client: [tool])
    def build_fake_agent(**kwargs: Any) -> object:
        built_agents.append(kwargs)
        return object()

    monkeypatch.setattr(runtime, "Agent", build_fake_agent)

    runtime.build_agent("dataset", client=client)

    assert loaded == [False]
    assert built_agents == [
        {
            "client": client,
            "name": profile.name,
            "instructions": profile.instructions,
            "tools": [tool],
            "default_options": {"store": False},
        }
    ]

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "test-model")
    credential = object()
    monkeypatch.setattr(runtime, "get_credential", lambda: credential)
    monkeypatch.setattr(
        runtime,
        "FoundryChatClient",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    built_client = runtime._build_foundry_client()
    assert built_client.project_endpoint == "https://foundry.example.test"
    assert built_client.model == "test-model"
    assert built_client.credential is credential

    monkeypatch.setattr(runtime, "build_agent", lambda profile_id: profile_id)
    monkeypatch.setattr(
        runtime,
        "ResponsesHostServer",
        lambda selected: SimpleNamespace(run=lambda: hosted.append(selected)),
    )
    runtime.run_profile("dataset")
    assert hosted == ["dataset"]
    assert runtime.describe_profile("dataset") is profile


def test_hosted_agent_entrypoints_are_import_safe_and_dispatch_exact_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_profiles = [
        "coordinator",
        "dataset",
        "grant",
        "grant_online",
        "institution",
        "literature",
        "literature_online",
        "matching",
        "matching_online",
    ]
    dispatched: list[str] = []
    monkeypatch.setattr(runtime, "run_profile", dispatched.append)

    for profile_id in expected_profiles:
        entrypoint = ROOT / "agents" / profile_id / "main.py"
        dispatch_count = len(dispatched)
        runpy.run_path(str(entrypoint), run_name=f"coverage.{profile_id}")
        assert len(dispatched) == dispatch_count
        runpy.run_path(str(entrypoint), run_name="__main__")

    assert dispatched == expected_profiles


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
