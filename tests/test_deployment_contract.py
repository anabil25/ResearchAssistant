from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from research_assistant_core.connector_catalog import connector_definitions

from scripts import build_agent_source_tree, deploy_sequential, postprovision
from scripts.postprovision import (
    AmbiguousToolboxCreate,
    FoundryProjectUnavailable,
    ToolboxProjectUnavailable,
    _assert_mcp_success,
)
from scripts.provider_onboarding import connector_project_connection_ids
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
    parameters = json.loads(
        (ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8")
    )
    workflow_steps = [
        step["azd"]["args"] for step in config["workflows"]["up"]["steps"]
    ]

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
    assert workflow_steps == [["provision"]]
    assert config["hooks"]["postup"]["windows"]["run"] == "./scripts/postup.ps1"
    assert config["hooks"]["postup"]["posix"]["run"] == "./scripts/postup.sh"
    assert config["hooks"]["postup"]["windows"]["interactive"] is False
    assert config["hooks"]["postup"]["posix"]["interactive"] is False
    assert config["hooks"]["postdown"]["windows"]["run"] == "./scripts/postdown.ps1"
    assert config["hooks"]["postdown"]["posix"]["run"] == "./scripts/postdown.sh"
    assert config["hooks"]["postdown"]["windows"]["interactive"] is False
    assert config["hooks"]["postdown"]["posix"]["interactive"] is False
    assert parameters["parameters"]["foundryProjectName"]["value"] == "${FOUNDRY_PROJECT_NAME}"
    assert parameters["parameters"]["foundryAccountName"]["value"] == "${FOUNDRY_ACCOUNT_NAME=}"
    assert parameters["parameters"]["resourceTokenSalt"]["value"] == (
        "${AZURE_DEPLOYMENT_INCARNATION=}"
    )
    preprovision_windows = (ROOT / "scripts" / "preprovision.ps1").read_text(encoding="utf-8")
    preprovision_posix = (ROOT / "scripts" / "preprovision.sh").read_text(encoding="utf-8")
    for preprovision in (preprovision_windows, preprovision_posix):
        assert "deployment_incarnation.py" in preprovision
        assert "ensure" in preprovision
    assert ") | while" not in preprovision_posix
    assert "python3 -m scripts.build_agent_source_tree" in preprovision_posix
    assert "if ! (cd \"$repo_root\" && python3 - \"$existing_deployments\" <<'PY'" in preprovision_posix
    assert 'if [ ! -s "$model_rows" ]' in preprovision_posix
    assert 'done < "$model_rows"' in preprovision_posix
    assert "$quotaAttempts = 20" in preprovision_windows
    assert "--subscription $subscription" in preprovision_windows
    assert "$existingCapacity" in preprovision_windows
    assert "deleted model quota to be released" in preprovision_windows
    assert "quota_attempts=20" in preprovision_posix
    assert '--subscription "$subscription"' in preprovision_posix
    assert "existing_capacity" in preprovision_posix
    assert "deleted model quota to be released" in preprovision_posix
    cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")
    assert "defaultConsistencyLevel: 'Strong'" in cosmos
    postdown_windows = (ROOT / "scripts" / "postdown.ps1").read_text(encoding="utf-8")
    postdown_posix = (ROOT / "scripts" / "postdown.sh").read_text(encoding="utf-8")
    for postdown in (postdown_windows, postdown_posix):
        assert "scripts.deployment_incarnation rotate" in postdown
    postup_windows = (ROOT / "scripts" / "postup.ps1").read_text(encoding="utf-8")
    postup_posix = (ROOT / "scripts" / "postup.sh").read_text(encoding="utf-8")
    for postup in (postup_windows, postup_posix):
        assert "scripts.deploy_sequential" in postup
        assert "scripts.verify_release" in postup
    release_verifier = (ROOT / "scripts" / "verify_release.py").read_text(
        encoding="utf-8"
    )
    assert "verify_platform_release()" in release_verifier
    assert "validate_agent_inventory" in release_verifier
    assert "validate_connection_inventory" in release_verifier
    assert "_shared_toolbox_tool_names" in release_verifier
    assert 'verify_container("api")' in release_verifier
    assert 'verify_container("web")' in release_verifier
    web_package = json.loads(
        (ROOT / "apps" / "web" / "package.json").read_text(encoding="utf-8")
    )
    assert web_package["scripts"]["test:release"] == (
        "playwright test e2e/live-grant-release.spec.ts --project=chromium"
    )
    live_grant_gate = (
        ROOT / "apps" / "web" / "e2e" / "live-grant-release.spec.ts"
    ).read_text(encoding="utf-8")
    assert "api.grants.gov/v1/api/fetchOpportunity" in live_grant_gate
    assert "/messages/stream" in live_grant_gate
    assert "Verified grant opportunities" in live_grant_gate
    assert deploy_sequential.DEPLOYMENT_ORDER == (
        "ai-project",
        "literature-agent",
        "grant-agent",
        "matching-agent",
        "dataset-agent",
        "institution-agent",
        "screening-agent",
        "research-coordinator",
        "api",
        "web",
    )


def test_sequential_agent_deploy_recovers_new_version_after_early_failure() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    version3_building = SimpleNamespace(version="3", status="provisioning", created_at=100)
    version3_active = SimpleNamespace(version="3", status="active", created_at=100)
    responses = iter([[version2], [version3_building], [version3_active]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )
    persisted: list[tuple[str, deploy_sequential.AgentVersionState, str]] = []

    state = deploy_sequential.deploy_agent_service(
        "literature-agent",
        operations,
        "https://example.test/projects/research",
        run_deploy=lambda _service: deploy_sequential.DeployAttempt(1, "ImageError"),
        persist=lambda service, version, endpoint: persisted.append(
            (service, version, endpoint)
        ),
        attempts=2,
        delay_seconds=0,
        sleep=lambda _delay: None,
        now=lambda: 100,
    )

    assert state == deploy_sequential.AgentVersionState("3", "active", 100)
    assert persisted == [
        (
            "literature-agent",
            state,
            "https://example.test/projects/research",
        )
    ]


def test_sequential_deploy_prepares_one_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    manifest = SimpleNamespace(source_tree_digest="a" * 64)
    monkeypatch.setattr(
        deploy_sequential,
        "validate_release_worktree_is_clean",
        lambda _root: events.append("release-clean"),
    )
    monkeypatch.setattr(
        deploy_sequential,
        "validate_worktree_matches_commit",
        lambda _root: events.append("validated"),
    )
    monkeypatch.setattr(
        deploy_sequential,
        "build_source_tree_manifest",
        lambda _root: manifest,
    )
    monkeypatch.setattr(
        deploy_sequential,
        "write_source_tree_manifest",
        lambda actual, _path: events.append(
            "manifest-written" if actual is manifest else "wrong-manifest"
        ),
    )

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        assert command == [
            "azd",
            "env",
            "set",
            "AGENT_SOURCE_TREE_DIGEST",
            "a" * 64,
        ]
        events.append("azd-persisted")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("scripts.deploy_sequential.subprocess.run", run)
    monkeypatch.delenv("AGENT_SOURCE_TREE_DIGEST", raising=False)

    digest = deploy_sequential.prepare_agent_source_identity()

    assert digest == "a" * 64
    assert events == [
        "release-clean",
        "validated",
        "manifest-written",
        "azd-persisted",
    ]
    assert os.environ["AGENT_SOURCE_TREE_DIGEST"] == digest


def test_release_identity_rejects_any_dirty_or_untracked_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        build_agent_source_tree,
        "_git",
        lambda _root, *_arguments: b" M services/api/app.py\n?? scripts/new_hook.py\n",
    )

    with pytest.raises(
        build_agent_source_tree.SourceIdentityBuildError,
        match="complete release",
    ):
        build_agent_source_tree.validate_release_worktree_is_clean(ROOT)


def test_agent_source_identity_includes_deployment_definitions() -> None:
    manifest = build_agent_source_tree.build_source_tree_manifest(ROOT)
    _commit, entries = build_agent_source_tree.committed_source_entries(ROOT)
    paths = {path for path, _content in entries}

    assert manifest.inclusion_policy_version == "2"
    assert ".agentignore" in paths
    assert {f"{agent.removesuffix('-agent')}/agent.yaml" for agent in SPECIALISTS} <= paths
    assert "coordinator/agent.yaml" in paths


def test_azd_child_deploy_has_a_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        assert command == ["azd", "deploy", "grant-agent", "--no-prompt"]
        assert kwargs["timeout"] == 12.0
        raise subprocess.TimeoutExpired(command, 12.0, output="partial output")

    monkeypatch.setattr("scripts.deploy_sequential.subprocess.run", run)

    result = deploy_sequential.run_azd_deploy(
        "grant-agent",
        timeout_seconds=12.0,
    )

    assert result.returncode == 124
    assert "partial output" in result.output
    assert "timed out after 12s" in result.output


def test_sequential_agent_deploy_rejects_unchanged_old_version() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    responses = iter([[version2], [version2], [version2]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )

    with pytest.raises(RuntimeError, match="Sequential deployment failed"):
        deploy_sequential.deploy_agent_service(
            "literature-agent",
            operations,
            "https://example.test/projects/research",
            run_deploy=lambda _service: deploy_sequential.DeployAttempt(1, "ImageError"),
            attempts=2,
            delay_seconds=0,
            sleep=lambda _delay: None,
            now=lambda: 100,
        )


def test_sequential_agent_deploy_nominal_success_rejects_stale_old_version() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    responses = iter([[version2], [version2], [version2]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )

    with pytest.raises(RuntimeError, match="Sequential deployment failed"):
        deploy_sequential.deploy_agent_service(
            "literature-agent",
            operations,
            "https://example.test/projects/research",
            run_deploy=lambda _service: deploy_sequential.DeployAttempt(0, "Done"),
            attempts=2,
            delay_seconds=0,
            sleep=lambda _delay: None,
            now=lambda: 100,
        )


def test_sequential_agent_deploy_accepts_explicit_active_version_reuse() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: [version2]
    )

    state = deploy_sequential.deploy_agent_service(
        "literature-agent",
        operations,
        "https://example.test/projects/research",
        run_deploy=lambda _service: deploy_sequential.DeployAttempt(
            0,
            "Agent version 2 is already active.",
        ),
        persist=lambda _service, _version, _endpoint: None,
        attempts=1,
        delay_seconds=0,
        sleep=lambda _delay: None,
        now=lambda: 100,
    )

    assert state.version == "2"


def test_sequential_agent_deploy_accepts_conflicting_version_that_becomes_active() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    version3_building = SimpleNamespace(version="3", status="provisioning", created_at=100)
    version3_active = SimpleNamespace(version="3", status="active", created_at=100)
    responses = iter([[version2], [version3_building], [version3_active]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )
    deploy_count = 0

    def run_deploy(_service: str) -> deploy_sequential.DeployAttempt:
        nonlocal deploy_count
        deploy_count += 1
        return deploy_sequential.DeployAttempt(1, "409 Conflict: agent already exists")

    state = deploy_sequential.deploy_agent_service(
        "literature-agent",
        operations,
        "https://example.test/projects/research",
        run_deploy=run_deploy,
        persist=lambda _service, _version, _endpoint: None,
        attempts=2,
        delay_seconds=0,
        sleep=lambda _delay: None,
        now=lambda: 100.0,
    )

    assert deploy_count == 1
    assert state.version == "3"


def test_non_conflict_early_failure_waits_for_remote_version_to_become_active() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    version3_building = SimpleNamespace(version="3", status="provisioning", created_at=100)
    version3_active = SimpleNamespace(version="3", status="active", created_at=100)
    responses = iter([[version2], [version3_building], [version3_active]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )

    state = deploy_sequential.deploy_agent_service(
        "literature-agent",
        operations,
        "https://example.test/projects/research",
        run_deploy=lambda _service: deploy_sequential.DeployAttempt(1, "ImageError"),
        persist=lambda _service, _version, _endpoint: None,
        attempts=2,
        delay_seconds=0,
        sleep=lambda _delay: None,
        now=lambda: 100.0,
    )

    assert state.version == "3"


def test_sequential_agent_deploy_retries_after_conflicting_version_fails() -> None:
    version2 = SimpleNamespace(version="2", status="active", created_at=50)
    version3_building = SimpleNamespace(version="3", status="provisioning", created_at=100)
    version3_failed = SimpleNamespace(version="3", status="failed", created_at=100)
    version4_active = SimpleNamespace(version="4", status="active", created_at=200)
    responses = iter([[version2], [version3_building], [version3_failed], [version4_active]])
    operations = SimpleNamespace(
        list_versions=lambda _name, **_kwargs: next(responses)
    )
    deploy_attempts = iter(
        [
            deploy_sequential.DeployAttempt(1, "409 Conflict: agent already exists"),
            deploy_sequential.DeployAttempt(0, "Done"),
        ]
    )
    deploy_count = 0

    def run_deploy(_service: str) -> deploy_sequential.DeployAttempt:
        nonlocal deploy_count
        deploy_count += 1
        return next(deploy_attempts)

    state = deploy_sequential.deploy_agent_service(
        "literature-agent",
        operations,
        "https://example.test/projects/research",
        run_deploy=run_deploy,
        persist=lambda _service, _version, _endpoint: None,
        attempts=2,
        delay_seconds=0,
        sleep=lambda _delay: None,
        now=iter([100.0, 200.0]).__next__,
    )

    assert deploy_count == 2
    assert state.version == "4"


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
    brownfield = (ROOT / "infra" / "brownfield.bicep").read_text(encoding="utf-8")

    assert "Microsoft.ContainerRegistry/registries@2025-11-01" in module
    assert "roleAssignmentMode: 'LegacyRegistryPermissions'" in module
    assert "azureADAuthenticationAsArmPolicy" in module
    assert "status: 'enabled'" in module
    assert "uniqueString(foundryAccount.id, foundryProjectName)" in module
    assert "name: acrConnectionName" in module
    assert "uniqueString(foundryAccount.id, projectName)" in brownfield
    assert "${accountName}/${projectName}/${acrConnectionName}" in brownfield


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
    monkeypatch.setenv("AZURE_WEB_MANAGED_IDENTITY_PRINCIPAL_ID", "web-principal-id")
    commands: list[list[str]] = []

    def completed(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(stdout="AcrPull\n")

    monkeypatch.setattr("scripts.postprovision.subprocess.run", completed)

    postprovision.wait_for_acr_pull_roles()

    assert len(commands) == 2
    assert all(command[1:4] == ["role", "assignment", "list"] for command in commands)
    assert all("containerapp" not in command for command in commands)
    assert "principal-id" in commands[0]
    assert "web-principal-id" in commands[1]


def test_web_uses_a_dedicated_pull_only_identity() -> None:
    identity = (ROOT / "infra" / "modules" / "identity.bicep").read_text(encoding="utf-8")
    environment = (
        ROOT / "infra" / "modules" / "container-apps-environment.bicep"
    ).read_text(encoding="utf-8")
    web = (ROOT / "infra" / "app" / "web.bicep").read_text(encoding="utf-8")
    web_parameters = json.loads(
        (ROOT / "infra" / "app" / "web.parameters.json").read_text(encoding="utf-8")
    )["parameters"]
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )

    assert "resource webIdentity" in identity
    assert "resource webIdentityAcrPull" in environment
    assert "param webIdentityResourceId string" in web
    assert "apiIdentityResourceId" not in web
    assert web_parameters["webIdentityResourceId"]["value"] == (
        "${AZURE_WEB_MANAGED_IDENTITY_RESOURCE_ID}"
    )
    assert "resource apiFoundryProjectManager" in resources
    assert "roleDefinitionId: foundryProjectManagerRoleId" in resources


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
    connector_connection_ids = connector_project_connection_ids(
        "/subscriptions/test/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/test/projects/research"
    )

    endpoint = postprovision._reconcile_shared_toolbox(
        cast(Any, object()),
        project_endpoint="https://example.test/api/projects/research",
        connector_targets=connector_targets,
        connector_connection_ids=connector_connection_ids,
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


def test_server_retry_after_is_capped() -> None:
    assert postprovision._parse_retry_after("3600") == 300

    response = SimpleNamespace(
        status_code=429,
        headers={"Retry-After": "3600", "x-ms-request-id": "request-1"},
        text="slow down",
    )
    error = __import__("scripts.provider_onboarding", fromlist=["ApimRequestError"]).ApimRequestError(
        "PUT",
        "/apis/test",
        response,
    )
    assert error.retry_after == 300