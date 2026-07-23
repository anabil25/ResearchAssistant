from __future__ import annotations

import json

import pytest

from scripts.azd_env import sync_canonical_azd_outputs
from scripts.configure_agent_rbac import (
    agent_environment_values,
    agent_instance_principal_id,
)
from scripts.postprovision import (
    RETRY_DELAYS,
    configure_connector_toolboxes,
    connector_connection_payload,
    load_documents,
    upload_source_artifacts,
    wait_for_acr_pull_roles,
)


def test_sample_corpus_is_ready_for_search_indexing() -> None:
    documents = load_documents()

    assert len(documents) == 10
    assert len({document["id"] for document in documents}) == len(documents)
    assert all(
        document["tenant_ids"] == ["demo"] and document["content"] and document["checksum"] for document in documents
    )


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


def test_connector_toolbox_connection_uses_project_identity() -> None:
    assert connector_connection_payload(
        target="https://gateway.example/mcp",
        audience="https://management.azure.com/",
    ) == {
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": "https://gateway.example/mcp",
            "audience": "https://management.azure.com",
            "metadata": {
                "type": "generic_mcp",
                "audience": "https://management.azure.com",
            },
        }
    }


def test_postprovision_creates_and_persists_toolbox_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example/api/projects/research",
        "AZURE_CONNECTOR_MCP_URL": "https://gateway.example/connectors/mcp",
    }
    created: set[str] = set()
    environment_updates: dict[str, str] = {}

    class Completed:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        if command[1:3] == ["cloud", "show"]:
            return Completed(stdout="https://management.azure.com/\n")
        if command[1:3] == ["rest", "--method"]:
            payload = json.loads(command[command.index("--body") + 1])
            assert payload["properties"]["authType"] == "ProjectManagedIdentity"
            return Completed()
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

    endpoints = configure_connector_toolboxes()

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
