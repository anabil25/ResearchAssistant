from __future__ import annotations

import json
from pathlib import Path
from time import time

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
    configure_connector_toolboxes,
    connector_connection_payload,
    connector_mcp_targets,
    expected_toolbox_tool_names,
    load_documents,
    toolbox_version_payload,
    upload_source_artifacts,
    wait_for_acr_pull_roles,
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
        "SERVICE_WORKER_NAME": "worker",
        "SERVICE_CONNECTOR_ADAPTER_NAME": "connector-adapter",
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
        "principal-worker",
        "principal-connector-adapter",
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


def test_postprovision_creates_and_persists_toolbox_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example/api/projects/research",
        "AZURE_CONNECTOR_MCP_URLS": _connector_mcp_urls(),
    }
    created: set[str] = set()
    environment_updates: dict[str, str] = {}
    arm_requests: list[dict[str, object]] = []

    class Completed:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        if command[1:4] == ["ai", "toolbox", "create"]:
            created.add(command[4])
            return Completed()
        if command[1:4] == ["ai", "toolbox", "show"]:
            name = command[4]
            if name not in created:
                return Completed(returncode=1)
            return Completed(
                stdout=json.dumps(
                    {
                        "endpoint": (
                            "https://foundry.example/api/projects/research/"
                            f"toolboxes/{name}/mcp?api-version=v1"
                        )
                    }
                )
            )
        if command[1:3] == ["env", "set"]:
            environment_updates[command[3]] = command[4]
            return Completed()
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr("scripts.postprovision.required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)
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
    monkeypatch.setattr(
        "scripts.postprovision._reconcile_toolbox",
        lambda _credential, *, toolbox_name, project_endpoint, mcp_targets: (
            f"{project_endpoint}/toolboxes/{toolbox_name}/mcp?api-version=v1"
        ),
    )

    endpoints = configure_connector_toolboxes(object())

    assert set(endpoints) == {
        "research-literature",
        "research-grant",
        "research-matching",
        "research-dataset",
    }
    assert set(environment_updates) == {
        "TOOLBOX_LITERATURE_MCP_ENDPOINT",
        "TOOLBOX_GRANT_MCP_ENDPOINT",
        "TOOLBOX_MATCHING_MCP_ENDPOINT",
        "TOOLBOX_DATASET_MCP_ENDPOINT",
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
