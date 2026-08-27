from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from research_assistant_core.connector_catalog import connector_definitions

from scripts import postprovision
from scripts.postprovision import (
    AmbiguousToolboxCreate,
    FoundryProjectUnavailable,
    ToolboxProjectUnavailable,
    _assert_mcp_success,
)
from scripts.verify_deployment import (
    PLACEHOLDER_IMAGE,
    revision_status,
    wait_for_http,
    wait_for_revision,
)

ROOT = Path(__file__).parents[1]
AGENTS = {
    "dataset-agent",
    "grant-agent",
    "institution-agent",
    "literature-agent",
    "matching-agent",
    "research-coordinator",
    "screening-agent",
}
SPECIALISTS = AGENTS - {"research-coordinator"}


def _azure_yaml() -> dict[str, Any]:
    payload = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _healthy_state(service: str, image: str) -> tuple[dict[str, Any], dict[str, Any]]:
    port = 8000 if service == "api" else 3000
    readiness_path = "/ready" if service == "api" else "/health"
    revision_name = f"ca-{service}-test--release"
    app = {
        "properties": {
            "latestRevisionName": revision_name,
            "latestReadyRevisionName": revision_name,
            "configuration": {"activeRevisionsMode": "Single"},
            "template": {
                "containers": [
                    {
                        "name": service,
                        "image": image,
                        "probes": [
                            {"type": "Startup", "httpGet": {"path": "/health", "port": port}},
                            {"type": "Liveness", "httpGet": {"path": "/health", "port": port}},
                            {"type": "Readiness", "httpGet": {"path": readiness_path, "port": port}},
                        ],
                    }
                ]
            },
        }
    }
    revision = {
        "name": revision_name,
        "properties": {
            "healthState": "Healthy",
            "runningState": "Running",
            "replicas": 1,
            "template": {
                "containers": [
                    {
                        "name": service,
                        "image": image,
                        "probes": [
                            {"type": "Startup", "httpGet": {"path": "/health", "port": port}},
                            {"type": "Liveness", "httpGet": {"path": "/health", "port": port}},
                            {"type": "Readiness", "httpGet": {"path": readiness_path, "port": port}},
                        ],
                    }
                ]
            },
        },
    }
    return app, revision


def test_azure_yaml_declares_the_release_dependency_graph() -> None:
    config = _azure_yaml()
    services = config["services"]

    for specialist in SPECIALISTS:
        assert services[specialist]["uses"] == ["ai-project"]
    assert set(services["research-coordinator"]["uses"]) == {"ai-project", *SPECIALISTS}
    assert set(services["api"]["uses"]) == AGENTS
    assert services["web"]["uses"] == ["api"]
    assert services["api"]["module"] == "app/api"
    assert services["web"]["module"] == "app/web"
    assert services["api"]["apiVersion"] == "2026-01-01"
    assert services["web"]["apiVersion"] == "2026-01-01"
    assert config["hooks"]["predeploy"]["windows"]["run"] == "./scripts/predeploy.ps1"
    assert "prepackage" not in config["hooks"]


def test_container_apps_exist_only_in_deploy_time_modules() -> None:
    bicep_files = tuple((ROOT / "infra").rglob("*.bicep"))
    containing_apps = {
        path.relative_to(ROOT).as_posix()
        for path in bicep_files
        if "Microsoft.App/containerApps@" in path.read_text(encoding="utf-8")
    }

    assert containing_apps == {"infra/app/api.bicep", "infra/app/web.bicep"}
    assert all(PLACEHOLDER_IMAGE not in path.read_text(encoding="utf-8") for path in bicep_files)


def test_acr_pins_the_managed_identity_pull_contract() -> None:
    module = (ROOT / "infra" / "modules" / "acr.bicep").read_text(encoding="utf-8")

    assert "Microsoft.ContainerRegistry/registries@2025-11-01" in module
    assert "roleAssignmentMode: 'LegacyRegistryPermissions'" in module
    assert "azureADAuthenticationAsArmPolicy" in module
    assert "status: 'enabled'" in module


@pytest.mark.parametrize("service", ["api", "web"])
def test_service_modules_bind_the_published_image(service: str) -> None:
    module = (ROOT / "infra" / "app" / f"{service}.bicep").read_text(encoding="utf-8")
    parameters = json.loads(
        (ROOT / "infra" / "app" / f"{service}.parameters.json").read_text(encoding="utf-8")
    )["parameters"]

    assert "param imageName string" in module
    assert "image: imageName" in module
    assert "activeRevisionsMode: 'Single'" in module
    assert parameters["imageName"]["value"] == f"${{SERVICE_{service.upper()}_IMAGE_NAME}}"
    assert parameters["tags"]["value"] == "${AZURE_TAGS}"
    assert "base64ToJson(tags)" in module
    assert "'azd-service-name'" in module


def test_agent_reconciliation_bootstraps_dependencies_without_postprovision_mutation() -> None:
    powershell = (ROOT / "scripts" / "reconcile-agents.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "scripts" / "reconcile-agents.sh").read_text(encoding="utf-8")

    assert "ensure-provision-env.ps1" in powershell
    assert "postprovision.ps1" not in powershell
    assert 'sh "$script_dir/ensure-provision-env.sh"' in posix
    assert "postprovision.sh" not in posix


@pytest.mark.parametrize("service", ["api", "web"])
def test_revision_status_accepts_only_the_exact_healthy_release(service: str) -> None:
    image = f"registry.example/{service}:immutable"
    app, revision = _healthy_state(service, image)

    assert revision_status(service, app, revision, image)[0] is True

    app["properties"]["latestReadyRevisionName"] = "older"
    assert revision_status(service, app, revision, image)[0] is False


def test_revision_status_rejects_placeholder_and_wrong_probes() -> None:
    app, revision = _healthy_state("api", PLACEHOLDER_IMAGE)
    assert revision_status("api", app, revision, PLACEHOLDER_IMAGE) == (
        False,
        "placeholder image is still configured",
    )

    app, revision = _healthy_state("api", "registry.example/api:immutable")
    revision["properties"]["template"]["containers"][0]["probes"][2]["httpGet"]["port"] = 80
    ready, detail = revision_status("api", app, revision, "registry.example/api:immutable")
    assert ready is False
    assert "probe contract" in detail


def test_revision_status_rejects_image_health_and_replica_failures() -> None:
    image = "registry.example/api:immutable"
    app, revision = _healthy_state("api", image)
    ready, detail = revision_status("api", app, revision, "registry.example/api:other")
    assert ready is False
    assert "image mismatch" in detail

    app, revision = _healthy_state("api", image)
    revision["properties"]["healthState"] = "Unhealthy"
    assert revision_status("api", app, revision, image)[0] is False

    app, revision = _healthy_state("api", image)
    revision["properties"]["replicas"] = 0
    assert revision_status("api", app, revision, image)[0] is False


def test_wait_for_revision_accepts_the_exact_ready_image(monkeypatch: pytest.MonkeyPatch) -> None:
    image = "registry.example/api:immutable"
    state = _healthy_state("api", image)
    monkeypatch.setenv("SERVICE_API_IMAGE_NAME", image)

    revision = wait_for_revision(
        "api",
        timeout_seconds=1,
        load_state=lambda _service: state,
        sleep=lambda _delay: pytest.fail("ready revision must not sleep"),
    )

    assert revision == "ca-api-test--release"


def test_wait_for_revision_retries_transient_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    image = "registry.example/api:immutable"
    state = _healthy_state("api", image)
    attempts = 0
    sleeps: list[float] = []
    monkeypatch.setenv("SERVICE_API_IMAGE_NAME", image)

    def load_state(_service: str) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.CalledProcessError(1, ["az", "containerapp", "show"])
        return state

    wait_for_revision(
        "api",
        timeout_seconds=1,
        load_state=load_state,
        sleep=sleeps.append,
    )

    assert attempts == 2
    assert sleeps == [5]


def test_wait_for_http_retries_until_healthy() -> None:
    statuses = iter([503, 200])
    sleeps: list[float] = []

    wait_for_http(
        "https://example.test/health",
        timeout_seconds=1,
        get_status=lambda _url: next(statuses),
        sleep=sleeps.append,
    )

    assert sleeps == [5]


def test_postprovision_checks_workload_identity_without_reading_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CONTAINER_REGISTRY_RESOURCE_ID", "/subscriptions/test/acr")
    monkeypatch.setenv("AZURE_MANAGED_IDENTITY_PRINCIPAL_ID", "principal-id")
    commands: list[list[str]] = []

    def completed(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(stdout="AcrPull\n")

    monkeypatch.setattr("scripts.postprovision.subprocess.run", completed)

    postprovision.wait_for_acr_pull_roles()

    assert len(commands) == 1
    assert commands[0][1:4] == ["role", "assignment", "list"]
    assert "containerapp" not in commands[0]
    assert "principal-id" in commands[0]


def test_tools_list_retries_wrapped_builtin_source_404() -> None:
    failures = {
        "errors": [
            {
                "name": name,
                "type": name,
                "error": {
                    "code": "NOT_FOUND",
                    "message": "RAPI MCP endpoint returned HTTP 404. ClientRequestId: test",
                },
            }
            for name in ("web_search", "code_interpreter")
        ]
    }
    payload = {
        "error": {
            "code": -32007,
            "message": f"tools/list failed for 2 tool source(s) {json.dumps(failures, separators=(',', ':'))}",
        }
    }

    with pytest.raises(ToolboxProjectUnavailable):
        _assert_mcp_success(payload, "tools/list")


def test_shared_toolbox_creates_one_version_while_readiness_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_calls = 0
    patch_calls = 0
    readiness_calls = 0
    collection_calls = 0
    version_list_calls = 0
    created_payload: dict[str, Any] | None = None

    def request(
        _credential: object,
        *,
        method: str,
        url: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        del session_id
        nonlocal collection_calls, create_calls, created_payload, patch_calls, version_list_calls
        if method == "GET" and url.endswith("/versions?api-version=v1"):
            version_list_calls += 1
            if version_list_calls == 1:
                return {"data": []}, None
            assert created_payload is not None
            return {
                "data": [
                    {
                        **created_payload,
                        "version": "1",
                        "created_at": 1,
                    }
                ]
            }, None
        if method == "GET" and url.endswith("/toolboxes?api-version=v1"):
            collection_calls += 1
            if collection_calls == 1:
                raise ToolboxProjectUnavailable("collection route not ready")
            return {"value": []}, None
        if method == "POST" and url.endswith("/versions?api-version=v1"):
            create_calls += 1
            created_payload = payload
            raise AmbiguousToolboxCreate("create applied but response was lost")
        assert method == "PATCH"
        patch_calls += 1
        if patch_calls == 1:
            raise ToolboxProjectUnavailable("parent route not ready")
        return {}, None

    def tool_names(
        _credential: object,
        *,
        project_endpoint: str,
        version: str | None,
    ) -> frozenset[str]:
        del project_endpoint, version
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 1:
            raise ToolboxProjectUnavailable("not ready")
        return postprovision.expected_shared_tool_names()

    monkeypatch.setattr(postprovision, "_toolbox_json_request", request)
    monkeypatch.setattr(postprovision, "_shared_toolbox_tool_names", tool_names)
    retry = postprovision.with_toolbox_readiness_retry

    def immediate_retry(
        toolbox_name: str,
        operation: Callable[[], Any],
        *,
        phase: str = "version readiness",
    ) -> Any:
        return retry(
            toolbox_name,
            operation,
            phase=phase,
            delays=(0, 0),
            sleep=lambda _delay: None,
            jitter=lambda _start, _end: 0.0,
        )

    monkeypatch.setattr(postprovision, "with_toolbox_readiness_retry", immediate_retry)
    connector_targets = {
        connector.id: f"https://gateway.example/{connector.id}/mcp"
        for connector in connector_definitions()
    }

    endpoint = postprovision._reconcile_shared_toolbox(
        cast(Any, object()),
        project_endpoint="https://example.test/api/projects/research",
        connector_targets=connector_targets,
    )

    assert endpoint.endswith("/toolboxes/research-shared/mcp?api-version=v1")
    assert version_list_calls == 2
    assert collection_calls == 2
    assert create_calls == 1
    assert readiness_calls == 3
    assert patch_calls == 2


@pytest.mark.parametrize(
    ("operation", "source"),
    [("initialize", "web_search"), ("tools/list", "pubmed")],
)
def test_mcp_errors_outside_builtin_tools_list_readiness_remain_fatal(
    operation: str,
    source: str,
) -> None:
    details = {
        "errors": [
            {
                "name": source,
                "error": {
                    "code": "NOT_FOUND",
                    "message": "RAPI MCP endpoint returned HTTP 404.",
                },
            }
        ]
    }
    payload = {
        "error": {
            "code": -32007,
            "message": f"failure {json.dumps(details, separators=(',', ':'))}",
        }
    }

    with pytest.raises(RuntimeError, match="Foundry Toolbox MCP"):
        _assert_mcp_success(payload, operation)


def test_foundry_readiness_retry_honors_server_delay() -> None:
    attempts = 0
    sleeps: list[float] = []

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FoundryProjectUnavailable("project routing", retry_after_seconds=30)
        return "ready"

    result = postprovision.with_foundry_readiness_retry(
        "memory store research_shared_memory",
        operation,
        phase="upsert",
        delays=(0, 10),
        sleep=sleeps.append,
        jitter=lambda _start, _end: 0.0,
    )

    assert result == "ready"
    assert attempts == 2
    assert sleeps == [30]