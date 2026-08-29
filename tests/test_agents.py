from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import yaml
from agent_framework import Agent, ChatContext, ChatResponse, ChatResponseUpdate, Message, ResponseStream
from agent_framework.exceptions import ChatClientException
from agent_framework_foundry_hosting import FoundryToolbox  # type: ignore[import-untyped]
from azure.ai.agentserver.core import FoundryAgentRequestContext
from azure.ai.agentserver.core._request_context import (
    reset_request_context,
    set_request_context,
)
from openai import APIStatusError
from research_assistant_core.connector_catalog import connector_definitions
from screening.main import (
    _REQUEST_MODE,
    RateLimitRetryMiddleware,
    RequestMode,
    RetrievalModelMiddleware,
)
from shared import credentials
from shared.contracts import canonical_digest
from shared.errors import ConfigurationError, InvocationError, RetryableInvocationError
from shared.middleware import ConnectorToolExposureMiddleware
from shared.profiles import get_profile, list_profiles
from shared.settings import HarnessSettings
from shared.toolbox import _BearerRefresh
from shared.tools import (
    _invoke_specialist,
    build_delegate_tool,
    delegated_agent_name,
    request_tool_names_for_profile,
    tools_for_profile,
)

ROOT = Path(__file__).parents[1]


def _bounded_client(responses: object) -> object:
    class Client:
        def __init__(self) -> None:
            self.responses = responses

        def with_options(self, **kwargs: object) -> Client:
            assert kwargs == {"max_retries": 0}
            return self

    return Client()


def test_screening_toolbox_forwards_only_current_foundry_call_context() -> None:
    async def token_provider() -> str:
        return "managed-identity-token"

    async def collect() -> list[httpx.Request]:
        auth = _BearerRefresh(token_provider)
        observed: list[httpx.Request] = []
        for call_id in ("call-first", "call-second"):
            token = set_request_context(
                FoundryAgentRequestContext(
                    call_id=call_id,
                    user_id="container-only-user",
                    session_id="container-only-session",
                )
            )
            try:
                request = httpx.Request("POST", "https://foundry.example/toolbox")
                observed.append(await anext(auth.async_auth_flow(request)))
            finally:
                reset_request_context(token)
        return observed

    observed = asyncio.run(collect())
    assert [item.headers["x-agent-foundry-call-id"] for item in observed] == [
        "call-first",
        "call-second",
    ]
    assert all(item.headers["authorization"] == "Bearer managed-identity-token" for item in observed)
    assert all("x-agent-user-id" not in item.headers for item in observed)
    assert all("x-agent-session-id" not in item.headers for item in observed)


def test_screening_retrieval_model_is_bound_outside_rebuilt_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = RetrievalModelMiddleware()
    context = SimpleNamespace(
        messages=[Message(role="user", contents=["Loop feedback without a mode digest."])],
        options={"model": "gpt-5.6-sol"},
    )
    called: list[bool] = []

    async def exercise() -> None:
        token = _REQUEST_MODE.set(RequestMode.RESEARCH)
        try:
            await middleware.process(cast(ChatContext, context), lambda: _mark_called(called))
        finally:
            _REQUEST_MODE.reset(token)

    monkeypatch.setenv("SCREENING_RETRIEVAL_MODEL", "gpt-5.4-mini")
    asyncio.run(exercise())

    assert called == [True]
    assert context.options["model"] == "gpt-5.4-mini"


async def _mark_called(called: list[bool]) -> None:
    called.append(True)


def test_screening_retries_rate_limit_only_before_stream_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = RateLimitRetryMiddleware()
    context = SimpleNamespace(stream=True, result=None)
    attempts = 0
    sleeps: list[float] = []
    update = ChatResponseUpdate(role="assistant")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def call_next() -> None:
        nonlocal attempts
        attempts += 1

        async def response_stream() -> Any:
            if attempts == 1:
                raise ChatClientException("429 rate limit")
            yield update

        context.result = ResponseStream(
            response_stream(),
            finalizer=ChatResponse.from_updates,
        )

    async def exercise() -> None:
        await middleware.process(cast(ChatContext, context), call_next)
        assert isinstance(context.result, ResponseStream)
        assert [item async for item in context.result] == [update]
        await context.result.get_final_response()

    monkeypatch.setattr("screening.main.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("screening.main.random.uniform", lambda _low, _high: 0.0)
    asyncio.run(exercise())

    assert attempts == 2
    assert sleeps == [5.0]


def test_screening_does_not_retry_partial_model_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = RateLimitRetryMiddleware()
    context = SimpleNamespace(stream=True, result=None)
    attempts = 0
    sleeps: list[float] = []

    async def call_next() -> None:
        nonlocal attempts
        attempts += 1

        async def response_stream() -> Any:
            yield ChatResponseUpdate(role="assistant")
            raise ChatClientException("429 rate limit")

        context.result = ResponseStream(
            response_stream(),
            finalizer=ChatResponse.from_updates,
        )

    async def exercise() -> None:
        await middleware.process(cast(ChatContext, context), call_next)
        assert isinstance(context.result, ResponseStream)
        with pytest.raises(ChatClientException, match="rate limit"):
            _ = [item async for item in context.result]

    monkeypatch.setattr("screening.main.asyncio.sleep", lambda delay: sleeps.append(delay))
    asyncio.run(exercise())

    assert attempts == 1
    assert sleeps == []


def test_canonical_agent_profiles_are_packaged() -> None:
    profiles = list_profiles()

    assert {profile.id for profile in profiles} == {
        "coordinator",
        "literature",
        "grant",
        "matching",
        "dataset",
        "institution",
    }
    for profile in profiles:
        assert "Evidence over fluency" in profile.instructions
        assert "untrusted data" in profile.instructions
        assert profile.workflow_steps
        assert profile.output_contract.endswith("V2")
        has_toolbox_binding = any(
            binding.operation_ref.id.startswith("foundry.toolbox.")
            for binding in profile.capability_bindings
        )
        if has_toolbox_binding:
            with pytest.raises(ConfigurationError, match="Toolbox endpoint"):
                tools_for_profile(profile)
        else:
            assert tools_for_profile(profile) == []
    assert len({profile.output_contract for profile in profiles}) == len(profiles)
    assert len({profile.workflow_steps for profile in profiles}) == len(profiles)


def test_dataset_instructions_do_not_affirm_unsupported_sensitive_propositions() -> None:
    instructions = get_profile("dataset").instructions

    assert "causality, statistical significance, approval or authorization" in instructions
    assert "fabricated data" in instructions
    assert "never restate it affirmatively when it is unsupported" in instructions
    assert "support=unsupported does not make affirmative wording acceptable" in instructions


def test_institution_instructions_preserve_source_coordinates_and_disclaimer_boundary() -> None:
    instructions = get_profile("institution").instructions

    assert "document version, effective date, page, section" in instructions
    assert "in each summary" in instructions.lower()
    assert "generic legal and IRB disclaimers separate and uncited" in instructions
    assert "unless the source itself states the disclaimer" in instructions


def test_grant_instructions_use_available_sources_and_verify_opportunities() -> None:
    instructions = get_profile("grant").instructions

    assert "supplied records, uploaded files, and enabled research sources" in instructions
    assert "Grants.gov lookup" in instructions
    assert "mark missing facts as explicit placeholders" in instructions


def test_web_search_is_declared_only_for_current_source_agents() -> None:
    enabled = {"literature", "grant", "matching", "dataset"}
    for profile in list_profiles():
        has_web = any(
            binding.operation_ref.id == "foundry.toolbox.web_search"
            for binding in profile.capability_bindings
        )
        assert has_web is (profile.id in enabled)


def test_coordinator_routes_public_and_internal_to_canonical_specialists() -> None:
    assert delegated_agent_name("literature", "public") == "literature-agent"
    assert delegated_agent_name("literature", "internal") == "literature-agent"
    assert delegated_agent_name("grant", "confidential") == "grant-agent"
    assert delegated_agent_name("institutional_qa", "restricted") == "institution-agent"


def test_azure_manifest_uses_current_hosted_agent_contract(
) -> None:
    azure_manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    services = azure_manifest["services"]
    agent_services = {name: config for name, config in services.items() if config.get("host") == "azure.ai.agent"}

    assert len(agent_services) == 7
    definitions: dict[str, dict[str, Any]] = {}
    for name, config in agent_services.items():
        definition = yaml.safe_load((ROOT / config["$ref"]).read_text(encoding="utf-8"))
        definitions[name] = definition
        assert definition["kind"] == "hosted", name
        assert definition["codeConfiguration"]["runtime"] == "python_3_13"
        assert definition["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
        assert "agent_reference" not in str(definition)
        entry_point = ROOT / "agents" / definition["codeConfiguration"]["entryPoint"]
        source = entry_point.read_text(encoding="utf-8")
        # Self-contained agents carry no shared factory, so there is no path to bootstrap.
        factory_import = f"from {entry_point.parent.name}.factory import run"
        if factory_import not in source:
            continue
        assert "sys.path.insert" in source, name
        assert source.index("sys.path.insert") < source.index(factory_import), name
    toolbox_variables = {
        name: next(
            (
                item["value"]
                    for item in definition["environmentVariables"]
                if item["name"] == "TOOLBOX_ENDPOINT"
            ),
            None,
        )
        for name, definition in definitions.items()
        if any(item["name"] == "TOOLBOX_ENDPOINT" for item in definition.get("environmentVariables", ()))
    }
    assert toolbox_variables == {
        "literature-agent": "${TOOLBOX_SHARED_MCP_ENDPOINT}",
        "grant-agent": "${TOOLBOX_SHARED_MCP_ENDPOINT}",
        "matching-agent": "${TOOLBOX_SHARED_MCP_ENDPOINT}",
        "dataset-agent": "${TOOLBOX_SHARED_MCP_ENDPOINT}",
    }


def test_profile_toolboxes_attach_the_complete_policy_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeToolbox:
        allowed_tools: frozenset[str] | None = None

    constructed: list[FakeToolbox] = []

    def build_toolbox(*_args: object, **_kwargs: object) -> FakeToolbox:
        toolbox = FakeToolbox()
        constructed.append(toolbox)
        return toolbox

    settings = HarnessSettings.model_validate(
        {
            "foundry_project_endpoint": "https://project.example",
            "model_deployment_name": "gpt-5.4-mini",
            "model_deployment_version": "2026-03-17",
            "source_tree_digest": "0" * 64,
            "toolbox_endpoint": "https://foundry.example/toolboxes/test/mcp?api-version=v1",
        }
    )
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", build_toolbox)

    profile = get_profile("grant")
    assert tools_for_profile(profile, settings=settings) is constructed[0]
    assert constructed[0].allowed_tools == frozenset(
        binding.operation_ref.id.removeprefix("foundry.toolbox.")
        for binding in profile.capability_bindings
    )
    assert tools_for_profile(get_profile("dataset"), settings=settings) is constructed[1]
    assert constructed[1].allowed_tools == frozenset(
        binding.operation_ref.id.removeprefix("foundry.toolbox.")
        for binding in get_profile("dataset").capability_bindings
    )


def test_agent_owns_toolbox_connection_lifecycle_exactly_once() -> None:
    events: list[str] = []

    class RecordingToolbox(FoundryToolbox):  # type: ignore[misc]
        async def connect(self, *, reset: bool = False) -> None:
            assert reset is False
            events.append("connect")
            self.is_connected = True
            await self.load_tools()

        async def load_tools(self) -> None:
            events.append("load")

        async def close(self) -> None:
            events.append("close")
            self.is_connected = False
            http_client = getattr(self, "_httpx_client", None)
            if http_client is not None:
                await http_client.aclose()

    toolbox = RecordingToolbox(
        cast(Any, object()),
        url="https://toolbox.example/mcp?api-version=v1",
    )
    agent = Agent(client=cast(Any, object()), tools=toolbox)

    async def exercise() -> None:
        async with agent:
            assert events == ["connect", "load"]
        assert events == ["connect", "load", "close"]

    asyncio.run(exercise())


def test_request_tool_names_reject_connectors_outside_profile_policy() -> None:
    with pytest.raises(ConfigurationError, match="outside the profile Toolbox surface"):
        request_tool_names_for_profile(
            get_profile("literature"),
            ("grants_gov",),
        )


def test_connector_tool_exposure_checks_availability_and_isolates_concurrent_requests() -> None:
    profile = get_profile("literature")
    middleware = ConnectorToolExposureMiddleware(profile)
    shared_tools = [
        SimpleNamespace(name="pubmed___search"),
        SimpleNamespace(name="pubmed___lookup"),
        SimpleNamespace(name="crossref___search"),
        SimpleNamespace(name="crossref___lookup"),
        SimpleNamespace(name="web_search"),
    ]
    shared_options = {"tools": shared_tools}
    observed: dict[str, frozenset[str]] = {}

    async def invoke(label: str, connector_ids: tuple[str, ...]) -> None:
        context = ChatContext(
            client=cast(Any, object()),
            messages=[],
            options=shared_options,
            function_invocation_kwargs={"authorized_connector_ids": connector_ids},
        )

        async def capture() -> None:
            await asyncio.sleep(0)
            assert context.options is not None
            observed[label] = frozenset(tool.name for tool in context.options["tools"])

        await middleware.process(context, capture)

    async def exercise() -> None:
        await asyncio.gather(
            invoke("pubmed", ("pubmed",)),
            invoke("crossref", ("crossref",)),
        )
        missing_context = ChatContext(
            client=cast(Any, object()),
            messages=[],
            options={"tools": [SimpleNamespace(name="web_search")]},
            function_invocation_kwargs={"authorized_connector_ids": ("pubmed",)},
        )
        with pytest.raises(ConfigurationError, match="missing authorized tools"):
            await middleware.process(missing_context, lambda: _mark_called([]))

    asyncio.run(exercise())

    assert observed == {
        "pubmed": frozenset({"pubmed___search", "pubmed___lookup", "web_search"}),
        "crossref": frozenset({"crossref___search", "crossref___lookup", "web_search"}),
    }
    assert shared_options["tools"] is shared_tools
    assert {tool.name for tool in shared_tools} == {
        "pubmed___search",
        "pubmed___lookup",
        "crossref___search",
        "crossref___lookup",
        "web_search",
    }


def test_bicep_model_parameters_match_azure_manifest() -> None:
    azure_manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))

    assert parameters["parameters"]["deployments"]["value"] == (
        azure_manifest["services"]["ai-project"]["deployments"]
    )
    assert parameters["parameters"]["location"]["value"] == "${AZURE_LOCATION}"
    # A first-ever provision has no local azd config, so every required parameter
    # must resolve from the environment azd seeds or from a default.
    assert parameters["parameters"]["resourceGroupName"]["value"] == "${AZURE_ENV_NAME}"
    assert parameters["parameters"]["foundryProjectName"]["value"] == "${FOUNDRY_PROJECT_NAME=}"
    assert parameters["parameters"]["foundryAccountName"]["value"] == "${FOUNDRY_ACCOUNT_NAME=}"
    assert parameters["parameters"]["resourceTokenSalt"]["value"] == (
        "${AZURE_DEPLOYMENT_INCARNATION=}"
    )


def test_all_hosted_agents_publish_the_baked_source_digest() -> None:
    for manifest_path in sorted((ROOT / "agents").glob("*/agent.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        environment = {
            item["name"]: item["value"]
            for item in manifest.get("environmentVariables", [])
        }
        assert environment["AGENT_SOURCE_TREE_DIGEST"] == (
            "${AGENT_SOURCE_TREE_DIGEST}"
        )


def test_apim_tool_resource_ids_are_globally_disjoint_from_operations() -> None:
    definitions = connector_definitions()
    tool_ids = [
        operation.apim_tool_name
        for connector in definitions
        for operation in connector.operations
    ]
    operation_ids = {
        "".join(character for character in operation.id.casefold() if character.isalnum())
        for connector in definitions
        for operation in connector.operations
    }

    assert len(tool_ids) == len(set(tool_ids))
    assert all(tool_id.startswith("research_") for tool_id in tool_ids)
    assert not {
        "".join(character for character in tool_id.casefold() if character.isalnum())
        for tool_id in tool_ids
    } & operation_ids
    assert not set(tool_ids) & {"search", "lookup"}


def test_hosted_agent_rejects_a_mismatched_deployed_source_digest(
    tmp_path: Path,
) -> None:
    identity = {
        "schema_version": "1",
        "inclusion_policy_version": "2",
        "producer": "research-assistant.git-source-tree",
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_root": "agents",
        "source_tree_digest": "c" * 64,
        "entry_count": 1,
    }
    manifest = {
        **identity,
        "source_manifest_digest": canonical_digest(identity),
    }
    manifest_path = tmp_path / "source-tree.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="configuration is invalid"):
        HarnessSettings.from_environment(
            {
                "FOUNDRY_PROJECT_ENDPOINT": "https://project.example",
                "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.4-mini",
                "AGENT_SOURCE_TREE_DIGEST": "d" * 64,
            },
            source_manifest_path=manifest_path,
        )


def test_accelerator_infrastructure_has_no_region_or_migration_pin() -> None:
    parameters = json.loads((ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8"))
    container_apps = (ROOT / "infra" / "modules" / "container-apps-environment.bicep").read_text(
        encoding="utf-8"
    )
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

    assert "searchLocation" not in parameters["parameters"]
    assert "applicationLocation" not in parameters["parameters"]
    assert "param applicationLocation string = location == 'centralus' ? 'eastus2' : location" in main
    assert "location: applicationLocation" in resources
    guardrail = resources.split("resource agenticGuardrail ", maxsplit=1)[1].split(
        "module acr ",
        maxsplit=1,
    )[0]
    assert "foundryAccount::project" in guardrail.split("dependsOn:", maxsplit=1)[1]
    assert "-v2" not in container_apps
    assert "keyVault" not in resources


def test_accelerator_private_data_and_ci_principal_contracts() -> None:
    storage = (ROOT / "infra" / "modules" / "storage.bicep").read_text(encoding="utf-8")
    cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
    search = (ROOT / "infra" / "modules" / "search.bicep").read_text(encoding="utf-8")

    assert "publicNetworkAccess: 'Disabled'" in storage
    assert "publicNetworkAccess: 'Disabled'" in cosmos
    assert "defaultAction: 'Deny'" in storage
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
    assert "$existingCapacity" in powershell
    assert "cognitiveservices usage list" in posix
    assert 'needed="$required"' in posix
    assert "existing_capacity" in posix
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


def test_accelerator_does_not_provision_durable_task_or_worker() -> None:
    assert not (ROOT / "infra" / "modules" / "durable-task.bicep").exists()
    assert not (ROOT / "services" / "worker" / "Dockerfile").exists()
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")
    containers = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "infra/modules/container-apps-environment.bicep",
            "infra/app/api.bicep",
            "infra/app/web.bicep",
        )
    )
    identities = (ROOT / "infra" / "modules" / "identity.bicep").read_text(
        encoding="utf-8"
    )
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    assert "Microsoft.DurableTask" not in resources
    assert "durableTask" not in resources
    assert "DURABLE_TASK_SCHEDULER_CONNECTION_STRING" not in containers
    assert "worker" not in manifest["services"]
    assert "resource worker " not in containers
    assert "workerIdentity" not in resources
    assert "workerIdentity" not in containers
    assert "workerIdentity" not in identities


def test_api_identity_owns_in_process_ingestion_dependencies() -> None:
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")
    containers = (ROOT / "infra" / "app" / "api.bicep").read_text(encoding="utf-8")
    storage = (ROOT / "infra" / "modules" / "storage.bicep").read_text(encoding="utf-8")
    search = (ROOT / "infra" / "modules" / "search.bicep").read_text(encoding="utf-8")
    cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
    document_intelligence = (
        ROOT / "infra" / "modules" / "document-intelligence.bicep"
    ).read_text(encoding="utf-8")

    assert "resource apiModelUser" in resources
    assert "principalId: identities.outputs.apiPrincipalId" in resources
    assert "principalId: apiPrincipalId" in storage
    assert "roleDefinitionId: blobDataContributorRoleId" in storage
    assert "principalId: apiPrincipalId" in search
    assert "roleDefinitionId: searchIndexDataContributorRoleId" in search
    assert "principalId: apiPrincipalId" in cosmos
    assert "roleDefinitionId: dataContributorRoleDefinitionId" in cosmos
    assert "principalId: apiPrincipalId" in document_intelligence
    assert "roleDefinitionId: cognitiveServicesUserRoleId" in document_intelligence
    for setting in (
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_AI_EMBEDDING_DEPLOYMENT_NAME",
        "AZURE_SEARCH_ENDPOINT",
        "AZURE_SEARCH_INDEX_NAME",
        "AZURE_COSMOS_ENDPOINT",
        "AZURE_STORAGE_BLOB_ENDPOINT",
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
    ):
        assert setting in containers


def test_azd_up_deploys_every_service_in_dependency_order_without_manifest_writes() -> None:
    manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))

    steps = [step["azd"]["args"] for step in manifest["workflows"]["up"]["steps"]]
    assert steps == [["provision"]]
    assert manifest["hooks"]["postup"]["windows"]["run"] == "./scripts/postup.ps1"
    assert manifest["hooks"]["postup"]["posix"]["run"] == "./scripts/postup.sh"
    assert manifest["requiredVersions"]["azd"] == "=1.32.0"
    assert manifest["requiredVersions"]["extensions"]["azure.ai.agents"] == "=1.0.0-beta.12"
    assert manifest["requiredVersions"]["extensions"]["azure.ai.projects"] == "=1.0.0-beta.7"

    agent_services = {
        name: service
        for name, service in manifest["services"].items()
        if service.get("host") == "azure.ai.agent"
    }
    assert len(agent_services) == 7
    for name, service in agent_services.items():
        assert set(service) == {"$ref", "host", "language", "project", "uses"}, name
        expected_uses = {"ai-project"}
        if name == "research-coordinator":
            expected_uses.update(set(agent_services) - {name})
        assert set(service["uses"]) == expected_uses, name
        definition = yaml.safe_load((ROOT / service["$ref"]).read_text(encoding="utf-8"))
        assert not {"docker", "image", "language", "project"} & definition.keys(), name
        assert definition["kind"] == "hosted", name
        assert definition["name"] == name, name
        assert definition["codeConfiguration"] == {
            "dependencyResolution": "remote_build",
            "entryPoint": definition["codeConfiguration"]["entryPoint"],
            "runtime": "python_3_13",
        }, name


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

    monkeypatch.setattr("shared.invocation.time.sleep", sleeps.append)

    output = _invoke_specialist(
        _bounded_client(Responses()),
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
    monkeypatch.setattr("shared.invocation.time.sleep", sleeps.append)
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

    with pytest.raises(InvocationError) as raised:
        _invoke_specialist(
            _bounded_client(FailedResponses()),
            "Analyze supplied evidence.",
            "literature-agent",
        )
    assert raised.value.__cause__ is status_error
    assert sleeps == []

    attempts = 0

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            nonlocal attempts
            attempts += 1
            return SimpleNamespace(output_text=None)

    with pytest.raises(
        InvocationError,
            match="Hosted Agent returned no output after bounded retries",
    ):
        _invoke_specialist(
            _bounded_client(EmptyResponses()),
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


def test_self_contained_specialists_export_build_and_run() -> None:
    for profile_id in (
        "dataset",
        "grant",
        "institution",
        "literature",
        "matching",
        "screening",
    ):
        module = importlib.import_module(f"{profile_id}.main")
        assert callable(module.build_agent), profile_id
        assert callable(module.run), profile_id


def test_hosted_agent_entrypoints_match_the_seven_canonical_services() -> None:
    azure_manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    expected = {
        "coordinator": "research-coordinator",
        "dataset": "dataset-agent",
        "grant": "grant-agent",
        "institution": "institution-agent",
        "literature": "literature-agent",
        "matching": "matching-agent",
        "screening": "screening-agent",
    }
    for profile_id, service_name in expected.items():
        module = importlib.import_module(f"{profile_id}.main")
        assert callable(module.run), profile_id
        service = azure_manifest["services"][service_name]
        definition = yaml.safe_load((ROOT / service["$ref"]).read_text(encoding="utf-8"))
        assert definition["name"] == service_name
        assert definition["codeConfiguration"]["entryPoint"] == f"{profile_id}/main.py"

    assert not tuple((ROOT / "agents").glob("*_online"))


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

    monkeypatch.setattr("shared.invocation.time.sleep", lambda _delay: None)
    with pytest.raises(RetryableInvocationError):
        _invoke_specialist(
            _bounded_client(NotReadyResponses()),
            "Analyze.",
            "dataset-agent",
        )

    class EmptyResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(output_text=" ")

    with pytest.raises(InvocationError, match="returned no output"):
        _invoke_specialist(
            _bounded_client(EmptyResponses()),
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
            assert agent_name == "literature-agent"
            return cast(SimpleNamespace, _bounded_client(Responses()))

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
    expected: dict[str, set[str]] = {}
    for profile_id, agent_id in {
        "literature": "literature",
        "grant": "grant",
        "matching": "matching",
        "dataset": "dataset",
    }.items():
        expected[profile_id] = {
            "web_search",
            *{
                f"{connector.id}___{operation.mcp_tool_name}"
                for connector in connector_definitions()
                if agent_id in connector.assigned_agents
                for operation in connector.operations
                if operation.operation_class != "delete"
            },
        }
    expected["dataset"].add("code_interpreter")
    for profile_id, tool_names in expected.items():
        manifest = get_profile(profile_id)
        assert {binding.operation_ref.id.rsplit(".", 1)[-1] for binding in manifest.capability_bindings} == tool_names
