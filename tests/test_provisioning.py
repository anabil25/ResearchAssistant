from __future__ import annotations

import json
from pathlib import Path
from time import time

import httpx
import pytest
from azure.core.credentials import AccessToken
from research_assistant_core.connector_catalog import connector_definitions

from scripts.azd_env import sync_canonical_azd_outputs
from scripts.configure_agent_rbac import (
    agent_environment_values,
    agent_instance_principal_id,
)
from scripts.postprovision import (
    RETRY_DELAYS,
    _reconcile_toolbox,
    apim_mcp_subscription_key,
    configure_agent_memory,
    configure_connector_connections,
    configure_connector_gateway,
    connector_connection_payload,
    connector_mcp_targets,
    expected_shared_tool_names,
    expected_toolbox_tool_names,
    load_documents,
    toolbox_version_payload,
    upload_source_artifacts,
    wait_for_acr_pull_roles,
)
from scripts.provider_onboarding import (
    APIM_READY_RETRY_DELAYS,
    ApimOnboarder,
    ApimRequestError,
)


def _connector_mcp_urls() -> str:
    return json.dumps(
        [
            {
                "id": connector.id,
                "endpoint": f"https://gateway.example/{connector.id}/mcp",
            }
            for connector in connector_definitions()
        ]
    )


def test_api_can_delegate_only_the_authenticated_user_identity_to_foundry() -> None:
    root = Path(__file__).parents[1]
    role = (root / "infra" / "modules" / "foundry-user-identity-role.bicep").read_text(
        encoding="utf-8"
    )
    resources = (root / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )

    assert role.count(
        "Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/"
        "UserIdentityImpersonation/action"
    ) == 1
    assert "actions: []" in role
    assert "notDataActions: []" in role
    assert "assignableScopes" in role
    assert "scope: foundryAccount" in resources
    assert "principalId: identities.outputs.apiPrincipalId" in resources
    assert "roleDefinitionId: apiUserIdentityImpersonationRole.outputs.roleDefinitionId" in resources


def test_sample_corpus_is_ready_for_search_indexing() -> None:
    documents = load_documents()

    assert len(documents) == 10
    assert len({document["id"] for document in documents}) == len(documents)
    assert all(
        document["tenant_ids"] == ["demo"] and document["content"] and document["checksum"] for document in documents
    )


def test_postprovision_writes_local_env_and_repoints_a_stale_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The endpoints only exist in ``.azure/<env>/.env``, which the app never
    reads, so a local API run after ``azd up`` would otherwise show an empty
    Agent Studio. Re-provisioning must also overwrite an endpoint left behind
    by a torn-down environment, while keeping a developer's own lines."""
    import scripts.postprovision as postprovision

    monkeypatch.setattr(postprovision, "ROOT", tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text(
        'RESEARCH_EXECUTION_MODE="mock"\n'
        f"{postprovision.LOCAL_ENV_BEGIN}\n"
        'FOUNDRY_PROJECT_ENDPOINT="https://deleted.example/api/projects/old"\n'
        f"{postprovision.LOCAL_ENV_END}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://live.example/api/projects/new")

    postprovision.write_local_env()

    written = env_path.read_text(encoding="utf-8")
    assert 'RESEARCH_EXECUTION_MODE="mock"' in written
    assert 'FOUNDRY_PROJECT_ENDPOINT="https://live.example/api/projects/new"' in written
    assert "deleted.example" not in written
    assert written.count(postprovision.LOCAL_ENV_BEGIN) == 1


def test_postprovision_allows_five_minutes_for_data_plane_rbac() -> None:
    assert RETRY_DELAYS[0] == 0
    assert sum(RETRY_DELAYS) >= 300


def test_apim_fresh_service_readiness_budget_exceeds_ten_minutes() -> None:
    assert APIM_READY_RETRY_DELAYS[0] == 0
    assert sum(APIM_READY_RETRY_DELAYS) >= 600


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"instance_identity": {"principal_id": "agent-principal-1"}}, "agent-principal-1"),
        ({"instanceIdentity": {"principalId": "agent-principal-2"}}, "agent-principal-2"),
    ],
)
def test_agent_identity_parser_selects_only_the_runtime_identity(
    payload: dict[str, object],
    expected: str,
) -> None:
    assert agent_instance_principal_id(payload) == expected


def test_agent_environment_outputs_are_canonical_and_complete() -> None:
    values = agent_environment_values(
        "grant-online-agent",
        "3",
        "https://foundry.example/api/projects/research",
    )

    assert values == {
        "AGENT_GRANT_ONLINE_AGENT_NAME": "grant-online-agent",
        "AGENT_GRANT_ONLINE_AGENT_VERSION": "3",
        "AGENT_GRANT_ONLINE_AGENT_ENDPOINT": (
            "https://foundry.example/api/projects/research/"
            "agents/grant-online-agent/versions/3"
        ),
        "AGENT_GRANT_ONLINE_AGENT_RESPONSES_ENDPOINT": (
            "https://foundry.example/api/projects/research/agents/"
            "grant-online-agent/endpoint/protocols/openai/responses?api-version=v1"
        ),
    }


def test_azd_output_sync_canonicalizes_provider_casing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = '{"azurE_SEARCH_ENDPOINT":"https://example.search.windows.net"}'

    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        del kwargs
        calls.append(command)
        return Completed()

    monkeypatch.setattr("scripts.azd_env.subprocess.run", fake_run)
    values = sync_canonical_azd_outputs()

    assert values["AZURE_SEARCH_ENDPOINT"] == "https://example.search.windows.net"
    assert calls[1][:4] == ["azd", "env", "set", "AZURE_SEARCH_ENDPOINT"]


def test_azd_output_sync_rejects_conflicting_case_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        stdout = '{"Azure_Search_Endpoint":"https://one.test","azure_search_endpoint":"https://two.test"}'

    monkeypatch.setattr(
        "scripts.azd_env.subprocess.run",
        lambda *_args, **_kwargs: Completed(),
    )

    with pytest.raises(RuntimeError, match="Conflicting azd values"):
        sync_canonical_azd_outputs()


def test_blob_archival_is_explicitly_skipped_when_policy_disables_public_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.postprovision.storage_public_network_access",
        lambda: "Disabled",
    )

    assert upload_source_artifacts(object()) is False  # type: ignore[arg-type]


def test_postprovision_waits_for_each_container_app_acr_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-test",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": "/subscriptions/test/acr",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "acrtest.azurecr.io",
        "SERVICE_WEB_NAME": "web",
        "SERVICE_API_NAME": "api",
    }
    role_checks: list[str] = []

    class Completed:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        if command[1:3] == ["containerapp", "show"]:
            return Completed("system\n")
        if command[1:4] == ["containerapp", "identity", "show"]:
            app_name = command[command.index("--name") + 1]
            return Completed(f"principal-{app_name}\n")
        if command[1:4] == ["role", "assignment", "list"]:
            role_checks.append(command[command.index("--assignee-object-id") + 1])
            return Completed("AcrPull\n")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(
        "scripts.postprovision.required_env",
        values.__getitem__,
    )
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)

    wait_for_acr_pull_roles()

    assert role_checks == [
        "principal-web",
        "principal-api",
    ]


def test_connector_toolbox_connection_uses_apim_subscription_key() -> None:
    assert connector_connection_payload(
        target="https://gateway.example/mcp",
        subscription_key="secret-key",
    ) == {
        "properties": {
            "authType": "CustomKeys",
            "category": "RemoteTool",
            "target": "https://gateway.example/mcp",
            "credentials": {
                "keys": {"Ocp-Apim-Subscription-Key": "secret-key"},
            },
            "metadata": {"type": "generic_mcp"},
        }
    }


def test_connector_gateway_preserves_existing_api_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.provider_onboarding as onboarding

    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "id": "semantic_scholar",
                    "apiId": "research-semantic-scholar-mcp-v1",
                    "path": "research-semantic-scholar-mcp",
                    "displayName": "Semantic Scholar MCP",
                    "description": "Literature metadata",
                    "credentialNamedValue": "research-semantic-scholar-key",
                    "tools": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    openapi = tmp_path / "openapi.json"
    openapi.write_text('{"openapi":"3.0.1","info":{"title":"test","version":"1"},"paths":{}}', encoding="utf-8")
    policies = tmp_path / "policies.json"
    policies.write_text("[]", encoding="utf-8")
    tools = tmp_path / "tools.json"
    tools.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(onboarding, "CONNECTOR_MCP_CATALOG", catalog)
    monkeypatch.setattr(onboarding, "CONNECTOR_OPENAPI", openapi)
    monkeypatch.setattr(onboarding, "CONNECTOR_OPERATION_POLICIES", policies)
    monkeypatch.setattr(onboarding, "CONNECTOR_MCP_TOOLS", tools)

    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    writes: list[str] = []
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        onboarder,
        "_get",
        lambda *_args, **_kwargs: {
            "properties": {
                "publisherEmail": "publisher@example.test",
                "gatewayUrl": "https://gateway.example.test",
            }
        },
    )
    monkeypatch.setattr(
        onboarder,
        "_exists",
        lambda path, **_kwargs: path == "/namedValues/research-semantic-scholar-key",
    )

    class Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    def put(path: str, _body: object, **_kwargs: object) -> Response:
        writes.append(path)
        events.append(("put", path))
        return Response()

    monkeypatch.setattr(onboarder, "_put", put)
    monkeypatch.setattr(
        onboarder,
        "_await_async_operation",
        lambda _response, label: events.append(("await-async", label)),
    )
    monkeypatch.setattr(
        onboarder,
        "_await_api",
        lambda api_id: events.append(("await-api", api_id)),
    )
    monkeypatch.setattr(onboarder, "_await_operations", lambda *_args: None)
    monkeypatch.setattr(onboarder, "_reconcile_connector_tools", lambda *_args: {})

    result = onboarder.reconcile_connector_gateway(
        tenant_id="tenant",
        api_principal_id="api-principal",
        foundry_principal_id="foundry-principal",
        apim_principal_id="apim-principal",
    )

    assert "/namedValues/research-semantic-scholar-key" not in writes
    assert writes.index("/apis/research-connectors-v1") < writes.index(
        "/apis/research-semantic-scholar-mcp-v1"
    )
    assert events.index(("await-api", "research-semantic-scholar-mcp-v1")) < events.index(
        ("put", "/apis/research-semantic-scholar-mcp-v1/policies/policy")
    )
    assert result["subscriptionId"] == "foundry-agent-tools"


def test_connector_tools_precede_mcp_policy_and_product_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.provider_onboarding as onboarding

    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "id": "pubmed",
                    "apiId": "research-pubmed-mcp-v1",
                    "path": "research-pubmed-mcp",
                    "displayName": "PubMed MCP",
                    "description": "PubMed",
                    "credentialNamedValue": "",
                    "tools": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    openapi = tmp_path / "openapi.json"
    openapi.write_text(
        '{"openapi":"3.0.1","info":{"title":"test","version":"1"},"paths":{}}',
        encoding="utf-8",
    )
    policies = tmp_path / "policies.json"
    policies.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(onboarding, "CONNECTOR_MCP_CATALOG", catalog)
    monkeypatch.setattr(onboarding, "CONNECTOR_OPENAPI", openapi)
    monkeypatch.setattr(onboarding, "CONNECTOR_OPERATION_POLICIES", policies)

    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    events: list[str] = []

    class Response:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    monkeypatch.setattr(
        onboarder,
        "_get",
        lambda *_args, **_kwargs: {
            "properties": {
                "publisherEmail": "publisher@example.test",
                "gatewayUrl": "https://gateway.example.test",
            }
        },
    )
    monkeypatch.setattr(onboarder, "_exists", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        onboarder,
        "_put",
        lambda path, *_args, **_kwargs: events.append(f"put:{path}") or Response(),
    )
    monkeypatch.setattr(onboarder, "_await_async_operation", lambda *_args: None)
    monkeypatch.setattr(onboarder, "_await_api", lambda *_args: None)
    monkeypatch.setattr(onboarder, "_await_operations", lambda *_args: None)
    repairs: list[str] = []
    monkeypatch.setattr(
        onboarder,
        "_repair_obsolete_connector_facade",
        lambda *_args: repairs.append("facade"),
    )
    monkeypatch.setattr(
        onboarder,
        "_reconcile_connector_tools",
        lambda *_args: events.append("tools") or {"research-pubmed-mcp-v1": 1},
    )

    onboarder.reconcile_connector_gateway(
        tenant_id="tenant",
        api_principal_id="api",
        foundry_principal_id="foundry",
        apim_principal_id="apim",
    )

    assert repairs == ["facade"]
    assert events.index("tools") < events.index(
        "put:/apis/research-pubmed-mcp-v1/policies/policy"
    )
    assert events.index("tools") < events.index(
        "put:/products/research-agent-tools/apis/research-pubmed-mcp-v1"
    )


def test_connector_tool_reconcile_targets_mcp_compatible_operations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.provider_onboarding as onboarding

    tools = tmp_path / "tools.json"
    tools.write_text(
        json.dumps(
            [
                {
                    "apiId": "research-pubmed-mcp-v1",
                    "name": "pubmedSearch",
                    "displayName": "search",
                    "description": "PubMed search",
                    "operationId": "pubmedSearch",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "CONNECTOR_MCP_TOOLS", tools)
    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    writes: list[tuple[str, dict[str, object]]] = []
    deletes: list[tuple[str, str]] = []
    reads = 0

    def get_inventory(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"value": [] if reads == 1 else [{"name": "pubmedSearch"}]}
    monkeypatch.setattr(
        onboarder,
        "_put_with_retry",
        lambda path, body, **_kwargs: writes.append((path, body)),
    )
    monkeypatch.setattr(
        onboarder,
        "_get",
        get_inventory,
    )
    monkeypatch.setattr(
        onboarder,
        "_delete_resource",
        lambda path, **kwargs: deletes.append((path, kwargs["api_version"])),
    )

    result = onboarder._reconcile_connector_tools(
        [{"apiId": "research-pubmed-mcp-v1"}],
    )

    assert result == {"research-pubmed-mcp-v1": 1}
    assert writes == [
        (
            "/apis/research-pubmed-mcp-v1/tools/pubmedSearch",
            {
                "properties": {
                    "displayName": "search",
                    "description": "PubMed search",
                    "operationId": (
                        "https://management.azure.com/subscriptions/sub/"
                        "resourceGroups/rg/providers/Microsoft.ApiManagement/"
                        "service/apim/apis/research-connectors-v1/operations/"
                        "pubmedSearch"
                    ),
                }
            },
        )
    ]
    assert deletes == []


def test_complete_connector_tool_inventory_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import scripts.provider_onboarding as onboarding

    tools = tmp_path / "tools.json"
    tools.write_text(
        json.dumps(
            [
                {
                    "apiId": "research-pubmed-mcp-v1",
                    "name": "pubmedSearch",
                    "displayName": "search",
                    "description": "PubMed search",
                    "operationId": "pubmedSearch",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(onboarding, "CONNECTOR_MCP_TOOLS", tools)
    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        onboarder,
        "_get",
        lambda *_args, **_kwargs: {"value": [{"name": "pubmedSearch"}]},
    )
    monkeypatch.setattr(
        onboarder,
        "_delete_resource",
        lambda *_args, **_kwargs: pytest.fail("complete inventory must not delete policies"),
    )
    monkeypatch.setattr(
        onboarder,
        "_put_with_retry",
        lambda *_args, **_kwargs: pytest.fail("complete inventory must not rewrite tools"),
    )

    assert onboarder._reconcile_connector_tools(
        [{"apiId": "research-pubmed-mcp-v1"}],
    ) == {"research-pubmed-mcp-v1": 1}


@pytest.mark.parametrize("exists", [True, False])
def test_superseded_backing_api_is_removed_after_its_tools(
    monkeypatch: pytest.MonkeyPatch,
    exists: bool,
) -> None:
    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    calls: list[str] = []
    monkeypatch.setattr(onboarder, "_exists", lambda *_args, **_kwargs: exists)
    monkeypatch.setattr(
        onboarder,
        "_get",
        lambda *_args, **_kwargs: {"value": [{"name": "pubmedSearch"}]},
    )
    monkeypatch.setattr(
        onboarder,
        "_delete_resource",
        lambda path, **_kwargs: calls.append(f"tool:{path}"),
    )
    monkeypatch.setattr(onboarder, "_delete_api", lambda api: calls.append(f"api:{api}"))

    onboarder._remove_obsolete_backing_api([{"apiId": "research-pubmed-mcp-v1"}])

    if not exists:
        assert calls == []
    else:
        assert calls == [
            "tool:/apis/research-pubmed-mcp-v1/tools/pubmedSearch",
            "api:research-connectors-mcp-backing-v1",
        ]


@pytest.mark.parametrize(
    ("operations", "deleted"),
    [
        ([{"name": "pubmedSearch", "properties": {"urlTemplate": "/search"}}], False),
        ([{"name": "pubmedSearchHttp", "properties": {"urlTemplate": "/search"}}], True),
        ([{"name": "pubmedSearch", "properties": {"urlTemplate": "/mcp/search"}}], True),
    ],
)
def test_only_obsolete_connector_facades_are_repaired(
    monkeypatch: pytest.MonkeyPatch,
    operations: list[dict[str, object]],
    deleted: bool,
) -> None:
    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    deletions: list[str] = []
    monkeypatch.setattr(onboarder, "_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        onboarder,
        "_get",
        lambda *_args, **_kwargs: {"value": operations},
    )
    monkeypatch.setattr(onboarder, "_delete_api", deletions.append)
    monkeypatch.setattr(onboarder, "_delete_connector_tools", lambda *_args: None)

    onboarder._repair_obsolete_connector_facade([])

    assert deletions == (["research-connectors-v1"] if deleted else [])


@pytest.mark.parametrize(("status", "retries"), [(400, False), (502, True)])
def test_apim_tool_retry_classifies_failures(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    retries: bool,
) -> None:
    import scripts.provider_onboarding as onboarding

    onboarder = ApimOnboarder(
        object(),  # type: ignore[arg-type]
        subscription_id="sub",
        resource_group="rg",
        service_name="apim",
        client=object(),  # type: ignore[arg-type]
    )
    request = httpx.Request("PUT", "https://management.azure.com/tool")
    failed = httpx.Response(
        status,
        headers={"x-ms-request-id": "request-1"},
        text="failure",
        request=request,
    )
    error = ApimRequestError("PUT", "/tool", failed)
    attempts = 0
    sleeps: list[int] = []

    def put(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return httpx.Response(200, request=request)

    monkeypatch.setattr(onboarding, "APIM_TOOL_RETRY_DELAYS", (0, 5))
    monkeypatch.setattr(onboarding.time, "sleep", sleeps.append)
    monkeypatch.setattr(onboarder, "_put", put)

    if not retries:
        with pytest.raises(ApimRequestError, match="request-id=request-1"):
            onboarder._put_with_retry("/tool", {}, label="tool")
        assert attempts == 1
        assert sleeps == []
    else:
        assert onboarder._put_with_retry("/tool", {}, label="tool").status_code == 200
        assert attempts == 2
        assert sleeps == [5]


def test_configure_connector_gateway_publishes_reconciled_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_SUBSCRIPTION_ID": "sub",
        "AZURE_RESOURCE_GROUP": "rg",
        "AZURE_API_MANAGEMENT_NAME": "apim",
        "AZURE_TENANT_ID": "tenant",
        "AZURE_API_MANAGED_IDENTITY_PRINCIPAL_ID": "api-principal",
        "AZURE_FOUNDRY_PROJECT_PRINCIPAL_ID": "foundry-principal",
        "AZURE_API_MANAGEMENT_PRINCIPAL_ID": "apim-principal",
    }
    calls: list[list[str]] = []
    result = {
        "subscriptionId": "foundry-agent-tools",
        "mcpUrls": [{"id": "pubmed", "endpoint": "https://gateway.example/pubmed/mcp"}],
    }
    monkeypatch.setattr("scripts.postprovision.required_env", values.__getitem__)
    monkeypatch.setattr(
        "scripts.postprovision._resource_manager_endpoint",
        lambda: "https://management.azure.com",
    )
    monkeypatch.setattr(
        "scripts.postprovision.reconcile_connector_gateway",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        "scripts.postprovision.subprocess.run",
        lambda command, **_kwargs: calls.append(command),
    )

    assert configure_connector_gateway(object()) == result  # type: ignore[arg-type]
    assert calls == [
        [
            "azd.exe",
            "env",
            "set",
            "AZURE_CONNECTOR_MCP_URLS",
            '[{"id":"pubmed","endpoint":"https://gateway.example/pubmed/mcp"}]',
        ],
        [
            "azd.exe",
            "env",
            "set",
            "AZURE_API_MANAGEMENT_MCP_SUBSCRIPTION_ID",
            "foundry-agent-tools",
        ],
    ]


def test_apim_mcp_subscription_key_uses_list_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
        "AZURE_RESOURCE_GROUP": "resource-group",
        "AZURE_API_MANAGEMENT_NAME": "apim-name",
        "AZURE_API_MANAGEMENT_MCP_SUBSCRIPTION_ID": "foundry-agent-tools",
    }
    calls: list[dict[str, object]] = []

    def arm_request(_credential: object, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"primaryKey": "secret-key"}

    monkeypatch.setattr("scripts.postprovision.required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision._arm_json_request", arm_request)

    assert apim_mcp_subscription_key(
        object(),
        resource_manager_endpoint="https://management.azure.com",
    ) == "secret-key"
    assert calls == [
        {
            "method": "POST",
            "url": (
                "https://management.azure.com/subscriptions/subscription-id/"
                "resourceGroups/resource-group/providers/Microsoft.ApiManagement/"
                "service/apim-name/subscriptions/foundry-agent-tools/"
                "listSecrets?api-version=2024-05-01"
            ),
            "resource_manager_endpoint": "https://management.azure.com",
        }
    ]


def test_connector_mcp_targets_require_the_complete_governed_catalog() -> None:
    targets = connector_mcp_targets(_connector_mcp_urls())

    assert targets == {
        connector.id: f"https://gateway.example/{connector.id}/mcp"
        for connector in connector_definitions()
    }
    with pytest.raises(RuntimeError, match="does not match the governed catalog"):
        connector_mcp_targets("[]")


def test_agent_memory_reuses_the_existing_named_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_AI_CHAT_DEPLOYMENT_NAME", "chat")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT_NAME", "embedding")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test/api/projects/p")
    monkeypatch.setattr(
        "scripts.postprovision._toolbox_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "Foundry Toolbox request failed with HTTP 400: "
                "Memory Store with Name research_shared_memory already exists!"
            )
        ),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "scripts.postprovision.subprocess.run",
        lambda command, **_kwargs: calls.append(command),
    )

    assert configure_agent_memory(object()) == "research_shared_memory"
    assert len(calls) == 1
    assert calls[0][1:] == ["env", "set", "MEMORY_STORE_NAME", "research_shared_memory"]


def test_agent_memory_does_not_hide_unrelated_bad_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_AI_CHAT_DEPLOYMENT_NAME", "chat")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT_NAME", "embedding")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example.test/api/projects/p")
    monkeypatch.setattr(
        "scripts.postprovision._toolbox_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Foundry Toolbox request failed with HTTP 400: invalid model")
        ),
    )

    with pytest.raises(RuntimeError, match="invalid model"):
        configure_agent_memory(object())


def test_toolbox_version_payloads_match_the_governed_connector_catalog() -> None:
    targets = connector_mcp_targets(_connector_mcp_urls())
    payload = toolbox_version_payload("research-literature", targets)

    assert payload["tools"][0] == {
        "type": "web_search",
        "name": "web_search",
        "require_approval": "never",
    }
    assert {
        tool["server_label"]
        for tool in payload["tools"]
        if tool["type"] == "mcp"
    } == {
        connector.id
        for connector in connector_definitions()
        if "literature" in connector.assigned_agents
    }
    assert expected_toolbox_tool_names("research-literature") == {
        "web_search",
        *{
            f"{connector.id}___{operation.mcp_tool_name}"
            for connector in connector_definitions()
            if "literature" in connector.assigned_agents
            for operation in connector.operations
            if operation.operation_class != "delete"
        },
    }
    assert toolbox_version_payload("research-dataset", targets)["tools"] == [
        {
            "type": "code_interpreter",
            "name": "code_interpreter",
            "require_approval": "never",
        }
    ]


def test_shared_toolbox_promotion_requires_every_pinned_governed_tool() -> None:
    assert expected_shared_tool_names() == {
        "tool_search",
        "call_tool",
        "web_search",
        "code_interpreter",
        *{
            f"{connector.id}___{operation.mcp_tool_name}"
            for connector in connector_definitions()
            for operation in connector.operations
            if operation.operation_class != "delete"
        },
    }


def test_postprovision_creates_connector_connections_for_shared_toolbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example/api/projects/research",
        "AZURE_CONNECTOR_MCP_URLS": _connector_mcp_urls(),
    }
    arm_requests: list[dict[str, object]] = []

    class Completed:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    monkeypatch.setattr("scripts.postprovision.required_env", values.__getitem__)
    monkeypatch.setattr(
        "scripts.postprovision._resource_manager_endpoint",
        lambda: "https://management.azure.com",
    )
    monkeypatch.setattr(
        "scripts.postprovision.apim_mcp_subscription_key",
        lambda _credential, *, resource_manager_endpoint: "secret-key",
    )
    monkeypatch.setattr(
        "scripts.postprovision._arm_json_request",
        lambda _credential, **kwargs: arm_requests.append(kwargs) or {},
    )
    targets = configure_connector_connections(object())

    assert targets == {
        connector.id: f"https://gateway.example/{connector.id}/mcp"
        for connector in connector_definitions()
    }
    assert len(arm_requests) == len(connector_definitions())
    for request in arm_requests:
        assert request["method"] == "PUT"
        properties = request["payload"]["properties"]
        assert properties["authType"] == "CustomKeys"
        assert properties["credentials"] == {
            "keys": {"Ocp-Apim-Subscription-Key": "secret-key"}
        }
        assert "audience" not in properties


def test_toolbox_reconciliation_creates_validates_and_promotes_a_full_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Credential:
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            del kwargs
            self.scopes.extend(scopes)
            return AccessToken("test-token", int(time()) + 3600)

    class Response:
        def __init__(self, payload: dict[str, object], session_id: str | None = None) -> None:
            self._payload = payload
            self.headers = {"Mcp-Session-Id": session_id} if session_id else {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    expected_tools = expected_toolbox_tool_names("research-literature")
    responses = [
        Response({"version": "2"}),
        Response({"result": {}}, session_id="session-1"),
        Response({}),
        Response(
            {
                "result": {
                    "tools": [
                        {
                            "name": name,
                            "description": "Governed tool",
                            "inputSchema": {"properties": {}},
                        }
                        for name in sorted(expected_tools)
                    ]
                }
            }
        ),
        Response({}),
    ]
    requests: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> Response:
        assert timeout == 30
        requests.append(request)
        return responses.pop(0)

    monkeypatch.setattr("scripts.postprovision.urlopen", fake_urlopen)
    credential = Credential()
    endpoint = _reconcile_toolbox(
        credential,
        toolbox_name="research-literature",
        project_endpoint="https://foundry.example/api/projects/research",
        mcp_targets=connector_mcp_targets(_connector_mcp_urls()),
    )

    assert endpoint == "https://foundry.example/api/projects/research/toolboxes/research-literature/mcp?api-version=v1"
    # One acquisition is reused across all five requests; the CLI credential does not cache.
    assert credential.scopes == ["https://ai.azure.com/.default"]
    assert [request.get_method() for request in requests] == ["POST", "POST", "POST", "POST", "PATCH"]
    created = json.loads(requests[0].data)
    assert {tool["server_label"] for tool in created["tools"] if tool["type"] == "mcp"} == {
        connector.id for connector in connector_definitions() if "literature" in connector.assigned_agents
    }
    assert json.loads(requests[1].data)["method"] == "initialize"
    assert requests[2].headers["Mcp-session-id"] == "session-1"
    assert json.loads(requests[3].data)["method"] == "tools/list"
    assert json.loads(requests[4].data) == {"default_version": "2"}
