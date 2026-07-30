from __future__ import annotations

import json
import runpy
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from research_assistant_core.connector_catalog import connector_definitions

from scripts import configure_agent_rbac as agent_rbac
from scripts import postprovision


class Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


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


def test_agent_rbac_run_json_parses_cli_output_and_propagates_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        calls.append((command, kwargs))
        return Completed('{"agent": "research-coordinator"}')

    monkeypatch.setattr("scripts.configure_agent_rbac.subprocess.run", fake_run)

    assert agent_rbac.run_json(["azd", "ai", "agent", "show"]) == {
        "agent": "research-coordinator"
    }
    assert calls == [
        (
            ["azd", "ai", "agent", "show"],
            {
                "check": True,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
            },
        )
    ]

    monkeypatch.setattr(
        "scripts.configure_agent_rbac.subprocess.run",
        lambda *_args, **_kwargs: Completed("not-json"),
    )
    with pytest.raises(json.JSONDecodeError):
        agent_rbac.run_json(["azd", "bad-json"])


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "not a JSON object"),
        ({}, "no instance identity"),
        ({"instance_identity": []}, "no instance identity"),
        ({"instance_identity": {"unexpected": "value"}}, "no principal id"),
        ({"instance_identity": {"principal_id": 7}}, "no principal id"),
        ({"instance_identity": {"principal_id": ""}}, "no principal id"),
    ],
)
def test_agent_identity_parser_rejects_malformed_runtime_identity(
    payload: object,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        agent_rbac.agent_instance_principal_id(payload)


def test_agent_rbac_required_env_accepts_values_and_rejects_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RBAC_TEST_VALUE", "configured")
    assert agent_rbac.required_env("RBAC_TEST_VALUE") == "configured"

    monkeypatch.delenv("RBAC_TEST_VALUE")
    with pytest.raises(RuntimeError, match="Missing azd environment value"):
        agent_rbac.required_env("RBAC_TEST_VALUE")


def test_agent_environment_values_normalize_a_trailing_project_slash() -> None:
    values = agent_rbac.agent_environment_values(
        "literature-agent",
        "4",
        "https://foundry.example/projects/research/",
    )

    assert values["AGENT_LITERATURE_AGENT_ENDPOINT"] == (
        "https://foundry.example/projects/research/agents/literature-agent/versions/4"
    )
    assert values["AGENT_LITERATURE_AGENT_RESPONSES_ENDPOINT"].count("//") == 1


def test_agent_output_sync_rejects_inactive_latest_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(
        name=agent_rbac.COORDINATOR,
        versions=SimpleNamespace(
            latest=SimpleNamespace(status="failed", version="2"),
        ),
    )
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example")
    monkeypatch.setattr(
        agent_rbac,
        "AIProjectClient",
        lambda **_kwargs: SimpleNamespace(
            agents=SimpleNamespace(list=lambda: [agent]),
        ),
    )
    monkeypatch.setattr(agent_rbac, "AzureCliCredential", object)

    with pytest.raises(
        RuntimeError,
        match="research-coordinator latest version is failed",
    ):
        agent_rbac.sync_agent_environment_outputs()


def test_agent_output_sync_reports_missing_deployments_and_ignores_unknown_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = SimpleNamespace(name="unmanaged-agent")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://foundry.example")
    monkeypatch.setattr(
        agent_rbac,
        "AIProjectClient",
        lambda **_kwargs: SimpleNamespace(
            agents=SimpleNamespace(list=lambda: [unknown]),
        ),
    )
    monkeypatch.setattr(agent_rbac, "AzureCliCredential", object)

    with pytest.raises(RuntimeError, match="Hosted Agent deployments are missing"):
        agent_rbac.sync_agent_environment_outputs()


def test_configure_agent_rbac_entrypoint_syncs_agents_and_grants_coordinator_role(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sync_calls: list[str] = []
    commands: list[list[str]] = []
    active_agents = [
        SimpleNamespace(
            name=name,
            versions=SimpleNamespace(
                latest=SimpleNamespace(
                    status=SimpleNamespace(value="active"),
                    version=index,
                )
            ),
        )
        for index, name in enumerate(agent_rbac.AGENT_NAMES, start=1)
    ]

    class ProjectClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["endpoint"] == "https://foundry.example/projects/research"
            assert kwargs["allow_preview"] is True
            self.agents = SimpleNamespace(list=lambda: active_agents)

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        commands.append(command)
        if command[:4] == ["azd", "ai", "agent", "show"]:
            assert kwargs["capture_output"] is True
            return Completed(
                '{"instance_identity":{"principal_id":"coordinator-principal"}}'
            )
        return Completed()

    def fake_sync() -> dict[str, str]:
        sync_calls.append("sync")
        return {}

    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://foundry.example/projects/research",
    )
    monkeypatch.setenv(
        "AZURE_AI_PROJECT_ID",
        "/subscriptions/test/projects/research",
    )
    monkeypatch.setattr(
        "scripts.azd_env.sync_canonical_azd_outputs",
        fake_sync,
    )
    monkeypatch.setattr("azure.ai.projects.AIProjectClient", ProjectClient)
    monkeypatch.setattr("azure.identity.AzureCliCredential", object)
    monkeypatch.setattr("subprocess.run", fake_run)

    runpy.run_path(str(Path(agent_rbac.__file__)), run_name="__main__")

    env_sets = [
        command
        for command in commands
        if command[:3] == ["azd", "env", "set"]
    ]
    role_commands = [
        command
        for command in commands
        if command[1:4] == ["role", "assignment", "create"]
    ]
    assert sync_calls == ["sync"]
    assert len(env_sets) == len(agent_rbac.AGENT_NAMES) * 4
    # Every hosted agent reads its own model deployment at startup, so each
    # instance identity needs the grant -- not just the coordinator's.
    assert role_commands == [
        [
            agent_rbac.AZ_CLI,
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            "coordinator-principal",
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            agent_rbac.FOUNDRY_USER_ROLE_ID,
            "--scope",
            "/subscriptions/test/projects/research",
            "--output",
            "none",
        ]
        for _ in agent_rbac.AGENT_NAMES
    ]
    output = capsys.readouterr().out
    for agent_name in agent_rbac.AGENT_NAMES:
        assert f"Granted Foundry User to {agent_name}" in output


def test_postprovision_required_env_accepts_values_and_rejects_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTPROVISION_TEST_VALUE", "configured")
    assert postprovision.required_env("POSTPROVISION_TEST_VALUE") == "configured"

    monkeypatch.delenv("POSTPROVISION_TEST_VALUE")
    with pytest.raises(RuntimeError, match="Required azd environment value is missing"):
        postprovision.required_env("POSTPROVISION_TEST_VALUE")


def test_rbac_retry_waits_after_authorization_errors_then_returns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class AuthorizationError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    attempts = 0
    sleeps: list[int] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AuthorizationError(401)
        if attempts == 2:
            raise AuthorizationError(403)
        return "ready"

    monkeypatch.setattr(postprovision, "HttpResponseError", AuthorizationError)
    monkeypatch.setattr(postprovision, "RETRY_DELAYS", (0, 2, 4))
    monkeypatch.setattr("scripts.postprovision.time.sleep", sleeps.append)

    assert postprovision.with_rbac_retry("Search", operation) == "ready"
    assert attempts == 3
    assert sleeps == [2, 4]
    assert "Search: waiting 4s for RBAC propagation (3/3)" in capsys.readouterr().out


def test_rbac_retry_immediately_propagates_non_authorization_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ServiceError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    error = ServiceError(500)
    monkeypatch.setattr(postprovision, "HttpResponseError", ServiceError)

    with pytest.raises(ServiceError) as raised:
        postprovision.with_rbac_retry("Search", lambda: (_ for _ in ()).throw(error))

    assert raised.value is error


def test_rbac_retry_raises_bounded_error_with_last_failure_as_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AuthorizationError(Exception):
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    failures: list[AuthorizationError] = []

    def operation() -> None:
        error = AuthorizationError(403)
        failures.append(error)
        raise error

    monkeypatch.setattr(postprovision, "HttpResponseError", AuthorizationError)
    monkeypatch.setattr(postprovision, "RETRY_DELAYS", (0, 0))

    with pytest.raises(RuntimeError, match="failed after bounded RBAC retries") as raised:
        postprovision.with_rbac_retry("Blob upload", operation)

    assert len(failures) == 2
    assert raised.value.__cause__ is failures[-1]


@pytest.mark.parametrize("payload", ["{}", "[]"])
def test_load_documents_rejects_missing_or_non_array_corpus(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    class JsonPath:
        def __truediv__(self, _part: str) -> JsonPath:
            return self

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            return payload

    monkeypatch.setattr(postprovision, "ROOT", JsonPath())

    with pytest.raises(RuntimeError, match="non-empty JSON array"):
        postprovision.load_documents()


def test_create_index_defines_filter_vector_and_semantic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class IndexClient:
        def __init__(self, *, endpoint: str, credential: object) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential

        def create_or_update_index(self, index: object) -> str:
            captured["index"] = index
            return "created"

    def invoke_retry(label: str, operation: Callable[[], object]) -> object:
        captured["label"] = label
        return operation()

    credential = object()
    monkeypatch.setattr(postprovision, "SearchIndexClient", IndexClient)
    monkeypatch.setattr(postprovision, "with_rbac_retry", invoke_retry)

    postprovision.create_index(
        "https://search.example",
        "research-evidence",
        credential,  # type: ignore[arg-type]
    )

    index = captured["index"]
    fields = {field.name: field for field in index.fields}  # type: ignore[attr-defined]
    assert captured["label"] == "Create Search index"
    assert captured["endpoint"] == "https://search.example"
    assert fields["id"].key is True
    assert fields["tenant_ids"].filterable is True
    assert fields["content_vector"].vector_search_dimensions == 3072
    assert index.vector_search.profiles[0].name == "research-vector-profile"  # type: ignore[attr-defined]
    assert index.semantic_search.default_configuration_name == "research-semantic"  # type: ignore[attr-defined]


def test_embed_documents_uses_managed_identity_token_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    credential = object()
    documents: list[dict[str, Any]] = [
        {"content": "first"},
        {"content": "second"},
    ]

    def token_provider(received: object, scope: str) -> str:
        assert received is credential
        assert scope == "https://cognitiveservices.azure.com/.default"
        return "token-provider"

    def create_embeddings(*, model: str, input: list[str]) -> object:
        captured["model"] = model
        captured["input"] = input
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[1.0, 0.0]),
                SimpleNamespace(embedding=[0.0, 1.0]),
            ]
        )

    def openai_client(**kwargs: object) -> object:
        captured["client"] = kwargs
        return SimpleNamespace(
            embeddings=SimpleNamespace(create=create_embeddings),
        )

    values = {
        "AZURE_OPENAI_ENDPOINT": "https://openai.example",
        "AZURE_AI_EMBEDDING_DEPLOYMENT_NAME": "embedding-model",
    }
    monkeypatch.setattr(postprovision, "get_bearer_token_provider", token_provider)
    monkeypatch.setattr(postprovision, "AzureOpenAI", openai_client)
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)

    postprovision.embed_documents(
        documents,
        credential,  # type: ignore[arg-type]
    )

    assert captured["client"] == {
        "azure_endpoint": "https://openai.example",
        "azure_ad_token_provider": "token-provider",
        "api_version": "2024-10-21",
    }
    assert captured["model"] == "embedding-model"
    assert captured["input"] == ["first", "second"]
    assert [document["content_vector"] for document in documents] == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]


@pytest.mark.parametrize("failed_keys", [[], ["doc-2"]])
def test_search_upload_reports_failed_document_keys(
    monkeypatch: pytest.MonkeyPatch,
    failed_keys: list[str],
) -> None:
    documents = [{"id": "doc-1"}, {"id": "doc-2"}]
    results = [
        SimpleNamespace(key=document["id"], succeeded=document["id"] not in failed_keys)
        for document in documents
    ]
    captured: dict[str, object] = {}

    class Client:
        def __init__(
            self,
            *,
            endpoint: str,
            index_name: str,
            credential: object,
        ) -> None:
            captured["connection"] = (endpoint, index_name, credential)

        def upload_documents(self, *, documents: list[dict[str, Any]]) -> object:
            captured["documents"] = documents
            return results

    monkeypatch.setattr(postprovision, "SearchClient", Client)
    credential = object()

    if failed_keys:
        with pytest.raises(RuntimeError, match=r"Search upload failed for \['doc-2'\]"):
            postprovision.upload_search_documents(
                "https://search.example",
                "evidence",
                documents,
                credential,  # type: ignore[arg-type]
            )
    else:
        postprovision.upload_search_documents(
            "https://search.example",
            "evidence",
            documents,
            credential,  # type: ignore[arg-type]
        )

    assert captured["connection"] == (
        "https://search.example",
        "evidence",
        credential,
    )
    assert captured["documents"] is documents


def test_storage_network_query_returns_trimmed_policy_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-research",
        "AZURE_STORAGE_ACCOUNT_NAME": "researchstorage",
    }
    captured: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured.append(command)
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
        }
        return Completed("Enabled\n")

    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)

    assert postprovision.storage_public_network_access() == "Enabled"
    assert captured[0][1:4] == ["storage", "account", "show"]


def test_blob_archival_uploads_only_files_with_deterministic_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploads: list[dict[str, object]] = []
    credential = object()

    class Artifact:
        def __init__(self, name: str, *, file: bool) -> None:
            self.name = name
            self.file = file

        def is_file(self) -> bool:
            return self.file

        def __lt__(self, other: Artifact) -> bool:
            return self.name < other.name

        def read_bytes(self) -> bytes:
            return f"content:{self.name}".encode()

    class SampleDirectory:
        def iterdir(self) -> list[Artifact]:
            return [
                Artifact("zeta.txt", file=True),
                Artifact("nested", file=False),
                Artifact("alpha.json", file=True),
            ]

    class Root:
        def __truediv__(self, part: str) -> SampleDirectory:
            assert part == "sample_data"
            return SampleDirectory()

    class ContainerClient:
        def upload_blob(self, **kwargs: object) -> None:
            uploads.append(kwargs)

    class BlobClient:
        def __init__(self, *, account_url: str, credential: object) -> None:
            assert account_url == "https://storage.example"
            assert credential is not None

        def get_container_client(self, container: str) -> ContainerClient:
            assert container == "source"
            return ContainerClient()

    values = {
        "AZURE_STORAGE_BLOB_ENDPOINT": "https://storage.example",
        "AZURE_STORAGE_SOURCE_CONTAINER": "source",
    }
    monkeypatch.setattr(
        postprovision,
        "storage_public_network_access",
        lambda: "Enabled",
    )
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr(postprovision, "BlobServiceClient", BlobClient)
    monkeypatch.setattr(postprovision, "ROOT", Root())

    assert postprovision.upload_source_artifacts(credential) is True  # type: ignore[arg-type]
    assert [upload["name"] for upload in uploads] == [
        "sample/alpha.json",
        "sample/zeta.txt",
    ]
    assert all(
        upload["overwrite"] is True
        and upload["metadata"] == {
            "fixture": "true",
            "license": "CC0-synthetic",
        }
        for upload in uploads
    )


def test_acr_role_wait_rejects_container_app_without_registry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-research",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": "/subscriptions/test/acr",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.example",
        "SERVICE_WEB_NAME": "web",
        "SERVICE_API_NAME": "api",
    }
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr(
        "scripts.postprovision.subprocess.run",
        lambda *_args, **_kwargs: Completed(" \n"),
    )

    with pytest.raises(RuntimeError, match="Container App web has no ACR identity"):
        postprovision.wait_for_acr_pull_roles()


def test_acr_role_wait_retries_until_assignment_is_visible(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-research",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": "/subscriptions/test/acr",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.example",
        "SERVICE_WEB_NAME": "web",
        "SERVICE_API_NAME": "api",
    }
    role_attempts: dict[str, int] = {}
    sleeps: list[int] = []

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        app_name = command[command.index("--name") + 1] if "--name" in command else ""
        if command[1:3] == ["containerapp", "show"]:
            return Completed(f"/subscriptions/test/identities/{app_name}")
        if command[1:3] == ["identity", "show"]:
            identity_name = command[command.index("--ids") + 1].rsplit("/", 1)[-1]
            return Completed(f"principal-{identity_name}")
        principal = command[command.index("--assignee-object-id") + 1]
        role_attempts[principal] = role_attempts.get(principal, 0) + 1
        role = "" if role_attempts[principal] == 1 else "AcrPull"
        return Completed(role)

    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)
    monkeypatch.setattr("scripts.postprovision.time.sleep", sleeps.append)

    postprovision.wait_for_acr_pull_roles()

    assert role_attempts == {
        "principal-web": 2,
        "principal-api": 2,
    }
    assert sleeps == [60, 60]
    assert capsys.readouterr().out.count("Waiting 60s") == 2


def test_acr_role_wait_fails_after_fifth_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-research",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": "/subscriptions/test/acr",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.example",
        "SERVICE_WEB_NAME": "web",
        "SERVICE_API_NAME": "api",
    }
    checks = 0

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        nonlocal checks
        if command[1:3] == ["containerapp", "show"]:
            return Completed("system")
        if command[1:4] == ["containerapp", "identity", "show"]:
            return Completed("principal-web")
        checks += 1
        return Completed()

    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)
    monkeypatch.setattr("scripts.postprovision.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="after five minutes"):
        postprovision.wait_for_acr_pull_roles()

    assert checks == 5


def test_acr_role_wait_continues_to_later_apps_if_retry_iterator_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_RESOURCE_GROUP": "rg-research",
        "AZURE_CONTAINER_REGISTRY_RESOURCE_ID": "/subscriptions/test/acr",
        "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.example",
        "SERVICE_WEB_NAME": "web",
        "SERVICE_API_NAME": "api",
    }
    identities: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> Completed:
        if command[1:3] == ["containerapp", "show"]:
            app_name = command[command.index("--name") + 1]
            identities.append(app_name)
            return Completed("system")
        assert command[1:4] == ["containerapp", "identity", "show"]
        return Completed("principal")

    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr("scripts.postprovision.subprocess.run", fake_run)
    monkeypatch.setattr(postprovision, "range", lambda _start, _stop: [], raising=False)

    postprovision.wait_for_acr_pull_roles()

    assert identities == ["web", "api"]


def test_connector_connections_require_active_cloud_arm_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
        "AZURE_CONNECTOR_MCP_URLS": _connector_mcp_urls(),
    }
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr(
        "scripts.postprovision.subprocess.run",
        lambda *_args, **_kwargs: Completed(" \n"),
    )

    with pytest.raises(RuntimeError, match="no ARM endpoint"):
        postprovision.configure_connector_connections(object())


def test_connector_connections_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
        "AZURE_CONNECTOR_MCP_URLS": _connector_mcp_urls(),
    }
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr(
        postprovision,
        "_resource_manager_endpoint",
        lambda: "https://management.azure.com",
    )
    monkeypatch.setattr(
        postprovision,
        "apim_mcp_subscription_key",
        lambda _credential, *, resource_manager_endpoint: "secret-key",
    )
    monkeypatch.setattr(
        postprovision,
        "_arm_json_request",
        lambda _credential, **_kwargs: {},
    )
    targets = postprovision.configure_connector_connections(object())

    assert targets == {
        connector.id: f"https://gateway.example/{connector.id}/mcp"
        for connector in connector_definitions()
    }


def test_shared_toolbox_retries_when_foundry_project_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr(postprovision, "TOOLBOX_PROJECT_RETRY_DELAYS", (0, 2))
    monkeypatch.setattr("scripts.postprovision.time.sleep", sleeps.append)
    attempts = 0

    def reconcile() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise postprovision.ToolboxProjectUnavailable("project not ready")
        return "https://foundry.example/toolboxes/research-shared/mcp?api-version=v1"

    endpoint = postprovision.with_toolbox_project_retry("research-shared", reconcile)

    assert endpoint.endswith("/research-shared/mcp?api-version=v1")
    assert sleeps == [2]


def test_toolbox_project_retry_budget_covers_fresh_foundry_provisioning() -> None:
    assert sum(postprovision.TOOLBOX_PROJECT_RETRY_DELAYS) >= 900
    assert postprovision.TOOLBOX_PROJECT_RETRY_DELAYS[-1] >= 300


def test_connector_connections_reject_non_https_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "AZURE_AI_PROJECT_ID": "/subscriptions/test/projects/research",
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example",
        "AZURE_CONNECTOR_MCP_URLS": _connector_mcp_urls(),
    }

    inventory = json.loads(_connector_mcp_urls())
    inventory[0]["endpoint"] = "http://insecure.example/mcp"
    values["AZURE_CONNECTOR_MCP_URLS"] = json.dumps(inventory)
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)

    with pytest.raises(RuntimeError, match="must be an HTTPS URL"):
        postprovision.configure_connector_connections(object())


def test_postprovision_main_orchestrates_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []
    credential = object()
    documents = [{"id": "one"}, {"id": "two"}]
    values = {
        "FOUNDRY_PROJECT_ENDPOINT": "https://foundry.example/projects/project-name",
        "AZURE_SEARCH_ENDPOINT": "https://search.example",
        "AZURE_SEARCH_INDEX_NAME": "research-index",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_AI_PROJECT_NAME": "project-name",
    }

    def sync_outputs() -> dict[str, str]:
        calls.append("sync")
        return {}

    def create_credential() -> object:
        calls.append("credential")
        return credential

    def load_documents(**kwargs: str) -> list[dict[str, str]]:
        calls.append(("load", kwargs))
        return documents

    def create_index(*args: object) -> None:
        calls.append(("index", args))

    def embed_documents(*args: object) -> None:
        calls.append(("embed", args))

    def upload_search_documents(*args: object) -> None:
        calls.append(("search-upload", args))

    def upload_source_artifacts(received: object) -> bool:
        calls.append(("blob-upload", received))
        return True

    def configure_tools(_credential: object) -> dict[str, int]:
        calls.append("connector-tools")
        return {"research-pubmed-mcp-v1": 2}

    connector_targets = {"pubmed": "https://gateway.example/pubmed/mcp"}

    def configure_connections(_credential: object) -> dict[str, str]:
        calls.append("connector-connections")
        return connector_targets

    def configure_providers(
        _credential: object,
        *,
        connector_targets: dict[str, str],
    ) -> str:
        assert connector_targets == {"pubmed": "https://gateway.example/pubmed/mcp"}
        calls.append("provider-apis")
        return "https://provider.example/mcp"

    def wait_for_roles() -> None:
        calls.append("acr-roles")

    monkeypatch.setattr(postprovision, "sync_canonical_azd_outputs", sync_outputs)
    monkeypatch.setattr(postprovision, "DefaultAzureCredential", create_credential)
    monkeypatch.setattr(postprovision, "required_env", values.__getitem__)
    monkeypatch.setattr(postprovision, "load_documents", load_documents)
    monkeypatch.setattr(postprovision, "create_index", create_index)
    monkeypatch.setattr(postprovision, "embed_documents", embed_documents)
    monkeypatch.setattr(
        postprovision,
        "upload_search_documents",
        upload_search_documents,
    )
    monkeypatch.setattr(
        postprovision,
        "upload_source_artifacts",
        upload_source_artifacts,
    )
    monkeypatch.setattr(
        postprovision,
        "configure_connector_mcp_tools",
        configure_tools,
    )
    monkeypatch.setattr(
        postprovision,
        "configure_connector_connections",
        configure_connections,
    )
    monkeypatch.setattr(
        postprovision,
        "configure_provider_apis",
        configure_providers,
    )
    monkeypatch.setattr(
        postprovision,
        "wait_for_acr_pull_roles",
        wait_for_roles,
    )

    postprovision.main()

    assert calls == [
        "sync",
        "credential",
        (
            "load",
            {"tenant_id": "tenant-id", "project_id": "project-name"},
        ),
        ("index", ("https://search.example", "research-index", credential)),
        ("embed", (documents, credential)),
        (
            "search-upload",
            ("https://search.example", "research-index", documents, credential),
        ),
        ("blob-upload", credential),
        "connector-tools",
        "connector-connections",
        "provider-apis",
        "acr-roles",
    ]
    assert "Provisioned 2 evidence records into research-index." in capsys.readouterr().out


def test_postprovision_entrypoint_dispatches_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path(postprovision.__file__).read_text(encoding="utf-8")
    entrypoint = 'if __name__ == "__main__":\n    main()\n'
    assert source.endswith(entrypoint)
    dispatched: list[str] = []

    compiled_entrypoint = compile(
        "\n" * (source.count("\n") - 2) + entrypoint,
        postprovision.__file__,
        "exec",
    )
    exec(
        compiled_entrypoint,
        {"__name__": "__main__", "main": lambda: dispatched.append("main")},
    )

    assert dispatched == ["main"]
