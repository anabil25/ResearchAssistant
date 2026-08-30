from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import yaml
from research_assistant_core.connector_catalog import connector_definitions

from scripts import build_agent_source_tree, deploy_sequential, postprovision
from scripts.deployment_incarnation import (
    DeploymentIdentity,
    ensure_deployment_identity,
    rotate_deployment_identity,
)
from scripts.postprovision import (
    AmbiguousToolboxCreate,
    FoundryProjectUnavailable,
    ToolboxProjectUnavailable,
    _assert_mcp_success,
)
from scripts.provider_onboarding import (
    apim_tool_resource_name,
    connector_project_connection_ids,
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
# The only values azd itself resolves before it substitutes infra/main.parameters.json
# on a first `azd up`: the environment name, subscription, and region prompts.
NATIVE_AZD_INPUTS = {
    "AZURE_ENV_NAME": "researchassistant-first-run",
    "AZURE_LOCATION": "eastus2",
    "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
}
_BICEP_PARAMETER = re.compile(
    r"^param\s+(?P<name>\w+)\s+[\w\[\]]+(?P<default>\s*=.*)?$", re.MULTILINE
)
_AZD_SUBSTITUTION = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:=(?P<default>[^}]*))?\}"
)
# A shell invocation of the Azure CLI, never of `azd` and never of a path segment.
_SHELL_AZ_CALL = re.compile(r"(?<![\w./$-])az\s+[a-z]")
_SCRIPT_MODULE = re.compile(r"scripts\.(\w+)|[/\\](\w+)\.py")
_AZ_STUB = '#!/bin/sh\nprintf \'{{ "azure-cli": "{version}" }}\\n\'\n'


def _azure_yaml() -> dict[str, Any]:
    payload = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def _posix_hook_scripts() -> set[str]:
    """Repo-relative POSIX hook targets azd executes directly, derived from azure.yaml."""
    config = _azure_yaml()
    groups: list[tuple[Path, Any]] = [(ROOT, config.get("hooks") or {})]
    for service in (config.get("services") or {}).values():
        groups.append((ROOT / service.get("project", "."), service.get("hooks") or {}))

    targets: set[str] = set()
    for base, hooks in groups:
        for hook in hooks.values():
            run = (hook.get("posix") or {}).get("run")
            if not run:
                continue
            target = shlex.split(run)[0]
            if not target.startswith("."):
                # An interpreter invocation such as `sh foo.sh` carries no mode contract.
                continue
            targets.add((base / target).resolve().relative_to(ROOT).as_posix())
    return targets


def _hook_scripts() -> set[Path]:
    """Every hook script azd runs, on both platforms, derived from azure.yaml."""
    config = _azure_yaml()
    groups: list[tuple[Path, Any]] = [(ROOT, config.get("hooks") or {})]
    for service in (config.get("services") or {}).values():
        groups.append((ROOT / service.get("project", "."), service.get("hooks") or {}))

    scripts: set[Path] = set()
    for base, hooks in groups:
        for hook in hooks.values():
            for platform in ("windows", "posix"):
                run = (hook.get(platform) or {}).get("run")
                if run:
                    scripts.add((base / shlex.split(run)[0]).resolve())
    return scripts


def _uncommented(body: str) -> str:
    """Both hook shells comment with `#`; prose about `az` is not a call to it."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def _azure_cli_consumers() -> set[str]:
    """scripts/ modules that shell out to `az` or authenticate through it."""
    consumers: set[str] = set()
    for module in (ROOT / "scripts").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        if "AZ_CLI" in source or "AzureCliCredential" in source:
            consumers.add(module.stem)
    return consumers


def _first_azure_cli_use(body: str, consumers: set[str]) -> int | None:
    positions = [match.start() for match in _SHELL_AZ_CALL.finditer(body)]
    positions += [
        match.start()
        for match in _SCRIPT_MODULE.finditer(body)
        if (match.group(1) or match.group(2)) in consumers
    ]
    return min(positions) if positions else None


def _write_posix_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _posix_stub_bin(tmp_path: Path) -> Path:
    """A hermetic PATH: the stubs plus only the utilities the guard and stubs use."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for utility in ("chmod", "cp", "head", "sed", "sort"):
        source = shutil.which(utility)
        assert source is not None, f"{utility} is required to exercise the guard"
        os.symlink(source, stub_bin / utility)
    return stub_bin


def _git_index_modes(paths: set[str]) -> dict[str, str]:
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--stage", "-z", "--", *sorted(paths)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:  # pragma: no cover - no git
        pytest.skip(f"git index unavailable: {error}")

    modes: dict[str, str] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        modes[path] = metadata.split()[0]
    return modes


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


def _bicep_parameters_without_defaults() -> set[str]:
    template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    return {
        match.group("name")
        for match in _BICEP_PARAMETER.finditer(template)
        if match.group("default") is None
    }


def _resolved_azd_parameters(values: Mapping[str, str]) -> dict[str, Any]:
    """Substitute azd environment values into the parameters file the way azd does."""

    def substitute(match: re.Match[str]) -> str:
        return values.get(match.group("name")) or (match.group("default") or "")

    document = (ROOT / "infra" / "main.parameters.json").read_text(encoding="utf-8")
    parameters = json.loads(_AZD_SUBSTITUTION.sub(substitute, document))["parameters"]
    return {name: body["value"] for name, body in parameters.items()}


def _parameters_azd_would_prompt_for(values: Mapping[str, str]) -> set[str]:
    """azd prompts for a template parameter that has neither a bound value nor a default."""
    resolved = _resolved_azd_parameters(values)
    return {
        name
        for name in _bicep_parameters_without_defaults()
        if resolved.get(name, "") == ""
    }


def _preup_identity(
    values: dict[str, str], incarnation: str
) -> DeploymentIdentity | None:
    return ensure_deployment_identity(
        values,
        set_value=values.__setitem__,
        token_factory=lambda: incarnation,
    )


def test_only_the_preup_identity_stands_between_native_inputs_and_provisioning() -> None:
    # resourceGroupName must never depend on a hook: azd can resolve it from the
    # environment name alone, exactly as preprovision and the down verifier do.
    assert _parameters_azd_would_prompt_for(NATIVE_AZD_INPUTS) == {"foundryProjectName"}
    assert _resolved_azd_parameters(NATIVE_AZD_INPUTS)["resourceGroupName"] == (
        NATIVE_AZD_INPUTS["AZURE_ENV_NAME"]
    )


def test_deployment_identity_is_written_before_azd_resolves_bicep_inputs() -> None:
    # azd substitutes infra/main.parameters.json before the preprovision hook runs,
    # so preprovision alone cannot keep foundryProjectName off the prompt surface.
    hooks = _azure_yaml()["hooks"]
    assert hooks["preup"]["windows"]["run"] == "./scripts/preup.ps1"
    assert hooks["preup"]["posix"]["run"] == "./scripts/preup.sh"
    assert hooks["preup"]["windows"]["interactive"] is False
    assert hooks["preup"]["posix"]["interactive"] is False
    for script in ("preup.ps1", "preup.sh"):
        body = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "scripts.deployment_incarnation ensure" in body
        assert "AZURE_ENV_NAME" in body


def test_first_azd_up_resolves_every_bicep_input_without_prompting() -> None:
    values = dict(NATIVE_AZD_INPUTS)
    identity = _preup_identity(values, "0123456789ab")
    assert identity is not None

    assert _parameters_azd_would_prompt_for(values) == set()
    resolved = _resolved_azd_parameters(values)
    assert resolved["resourceGroupName"] == values["AZURE_ENV_NAME"]
    assert resolved["foundryProjectName"] == identity.foundry_project_name
    assert resolved["foundryAccountName"] == identity.foundry_account_name
    assert resolved["resourceTokenSalt"] == identity.incarnation

    # An empty project name must stay unrepresentable rather than letting Bicep
    # invent a name the recorded identity does not know about.
    template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    assert "@minLength(3)\n@maxLength(32)\nparam foundryProjectName string\n" in template
    assert "@minLength(1)\n@maxLength(90)\nparam resourceGroupName string\n" in template


def test_repeat_and_post_down_azd_up_stay_off_the_prompt_surface() -> None:
    values = dict(NATIVE_AZD_INPUTS)
    first = _preup_identity(values, "0123456789ab")
    assert first is not None

    def _unexpected_token() -> str:
        raise AssertionError("A repeat up must reuse the committed incarnation.")

    repeated = ensure_deployment_identity(
        values, set_value=values.__setitem__, token_factory=_unexpected_token
    )
    assert repeated == first
    assert _parameters_azd_would_prompt_for(values) == set()

    rotated = rotate_deployment_identity(
        values, set_value=values.__setitem__, token_factory=lambda: "cafebabe0123"
    )
    assert rotated.foundry_account_name != first.foundry_account_name
    assert rotated.foundry_project_name != first.foundry_project_name
    assert _parameters_azd_would_prompt_for(values) == set()
    resolved = _resolved_azd_parameters(values)
    assert resolved["resourceGroupName"] == values["AZURE_ENV_NAME"]
    assert resolved["foundryProjectName"] == rotated.foundry_project_name
    assert resolved["foundryAccountName"] == rotated.foundry_account_name
    assert resolved["resourceTokenSalt"] == rotated.incarnation


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
    assert parameters["parameters"]["resourceGroupName"]["value"] == "${AZURE_ENV_NAME}"
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
    assert "$resourceGroup = $environmentName" in preprovision_windows
    assert "get-value AZURE_RESOURCE_GROUP" not in preprovision_windows
    assert "subscriptions/$subscription/locations?api-version=2022-12-01" in preprovision_windows
    assert "account list-locations" not in preprovision_windows
    assert "--subscription $subscription" in preprovision_windows
    assert "$existingCapacity" in preprovision_windows
    assert "deleted model quota to be released" in preprovision_windows
    assert "quota_attempts=20" in preprovision_posix
    assert 'resource_group="$environment_name"' in preprovision_posix
    assert "get-value AZURE_RESOURCE_GROUP" not in preprovision_posix
    assert "subscriptions/$subscription/locations?api-version=2022-12-01" in preprovision_posix
    assert "account list-locations" not in preprovision_posix
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


def test_posix_hooks_are_executable_in_the_git_index() -> None:
    # A fresh Linux clone runs these by relative path, so a 100644 blob mode fails the
    # hook with "Permission denied" (exit 126) before azd reaches Azure. Windows clones
    # report core.filemode=false, so only the recorded index mode is a portable contract.
    targets = _posix_hook_scripts()
    # A root hook plus a service hook whose run line carries `../../` and an argument.
    assert {"scripts/preup.sh", "scripts/verify-deployment.sh"} <= targets

    assert _git_index_modes(targets) == {target: "100755" for target in targets}


def test_azd_up_bootstraps_the_azure_cli_before_any_hook_needs_it() -> None:
    # A pristine machine has azd and nothing else, so the first hook that shells out
    # to `az` died with "az: not found" (exit 127). Every hook that reaches the CLI —
    # directly or through a scripts/ module that uses it — must load the bootstrap
    # first, because azd starts each hook as its own process.
    consumers = _azure_cli_consumers()
    assert {
        "configure_agent_rbac",
        "deploy_sequential",
        "deployment_incarnation",
        "postprovision",
        "verify_deployment",
        "verify_release",
    } <= consumers

    guarded: set[str] = set()
    for script in sorted(_hook_scripts()):
        body = _uncommented(script.read_text(encoding="utf-8"))
        first_use = _first_azure_cli_use(body, consumers)
        if first_use is None:
            continue
        guard = body.find("ensure-azure-cli")
        assert guard != -1, f"{script.name} needs the Azure CLI but never bootstraps it"
        assert guard < first_use, f"{script.name} uses the Azure CLI before bootstrapping it"
        guarded.add(script.name)

    assert {
        "preprovision.ps1",
        "preprovision.sh",
        "preup.ps1",
        "preup.sh",
    } <= guarded


def test_azure_cli_bootstrap_uses_the_azd_tool_manifest() -> None:
    posix = (ROOT / "scripts" / "ensure-azure-cli.sh").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "ensure-azure-cli.ps1").read_text(encoding="utf-8")
    for body in (posix, windows):
        assert "azd tool install az-cli" in body
        # azd owns the per-platform recipe. A hand-rolled installer would drift from
        # it, pin nothing, and assume privileges the acceptance contract never grants.
        for hand_rolled in (
            "apt-get",
            "brew",
            "choco",
            "curl",
            "Invoke-RestMethod",
            "Invoke-WebRequest",
            "sudo",
            "winget",
        ):
            assert hand_rolled not in _uncommented(body)

    # `azd tool` ships in the azd release this project already pins.
    assert _azure_yaml()["requiredVersions"]["azd"] == "=1.32.0"
    # One declared Azure CLI floor: README prerequisites and both bootstrap scripts.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "| Azure CLI (`az`) | 2.84+ |" in readme
    assert "RESEARCH_AZURE_CLI_MINIMUM_VERSION='2.84.0'" in posix
    assert '$researchAzureCliMinimumVersion = [version]"2.84.0"' in windows


def test_windows_bootstrap_republishes_path_for_later_hooks() -> None:
    # winget records the new CLI directory in the machine and user PATH, which a
    # process azd already started never sees, so every Windows hook re-reads both.
    windows = (ROOT / "scripts" / "ensure-azure-cli.ps1").read_text(encoding="utf-8")
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in windows
    assert '[Environment]::GetEnvironmentVariable("Path", "User")' in windows
    assert "$env:PATH = ((@($scopes) + @($env:PATH)) -join [IO.Path]::PathSeparator)" in windows


@pytest.mark.parametrize(
    ("argument", "installed", "published", "azd_exit", "expected_code", "expected_message"),
    [
        ("--verify", None, "2.84.0", 0, 0, ""),
        ("--verify", "2.90.1", None, 0, 0, ""),
        ("--verify", "2.80.0", None, 0, 1, "older than the supported minimum"),
        ("", "2.80.0", None, 0, 0, ""),
        ("", None, None, 1, 1, "aka.ms/azure-cli"),
        ("", None, None, 0, 1, "still not on PATH"),
    ],
)
def test_posix_azure_cli_bootstrap(
    tmp_path: Path,
    argument: str,
    installed: str | None,
    published: str | None,
    azd_exit: int,
    expected_code: int,
    expected_message: str,
) -> None:
    shell = shutil.which("sh")
    if shell is None:  # pragma: no cover - Windows developer machines
        pytest.skip("POSIX shell unavailable")

    stub_bin = _posix_stub_bin(tmp_path)
    if installed is not None:
        _write_posix_stub(stub_bin / "az", _AZ_STUB.format(version=installed))
    published_az = tmp_path / "published-az"
    if published is not None:
        _write_posix_stub(published_az, _AZ_STUB.format(version=published))

    log = tmp_path / "azd.log"
    _write_posix_stub(
        stub_bin / "azd",
        "#!/bin/sh\n"
        f'echo "azd $*" >> "{log}"\n'
        f'if [ -f "{published_az}" ]; then\n'
        f'  cp "{published_az}" "{stub_bin}/az"\n'
        "fi\n"
        f"exit {azd_exit}\n",
    )
    driver = tmp_path / "driver.sh"
    _write_posix_stub(
        driver,
        "#!/bin/sh\nset -eu\n"
        f'. "{ROOT / "scripts" / "ensure-azure-cli.sh"}"\n'
        f"research_ensure_azure_cli {argument}\n"
        "command -v az\n",
    )

    completed = subprocess.run(
        [shell, str(driver)],
        capture_output=True,
        text=True,
        env={"PATH": str(stub_bin), "HOME": str(tmp_path)},
    )

    assert completed.returncode == expected_code, completed.stderr
    assert expected_message in completed.stderr
    called_azd = log.read_text(encoding="utf-8") if log.exists() else ""
    if installed is None:
        assert "tool install az-cli" in called_azd
    else:
        # An already-usable CLI must never be reinstalled on every hook.
        assert called_azd == ""


def test_windows_azure_cli_bootstrap_gates_version_and_refreshes_path(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None or os.name != "nt":  # pragma: no cover - non-Windows agents
        pytest.skip("Windows PowerShell host unavailable")

    guard = ROOT / "scripts" / "ensure-azure-cli.ps1"
    log = tmp_path / "azd.log"
    (tmp_path / "azd.ps1").write_text(
        f'Add-Content -Path "{log}" -Value "azd $args"\nexit 0\n', encoding="utf-8"
    )

    def run(version: str, verify: str) -> subprocess.CompletedProcess[str]:
        (tmp_path / "az.ps1").write_text(
            f"Write-Output '{{ \"azure-cli\": \"{version}\" }}'\n", encoding="utf-8"
        )
        return subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "$PSNativeCommandUseErrorActionPreference = $true; "
                f"$env:PATH = '{tmp_path}'; . '{guard}'{verify}",
            ],
            capture_output=True,
            text=True,
        )

    supported = run("2.90.1", " -Verify")
    assert supported.returncode == 0, supported.stderr
    assert not log.exists(), "an already-usable CLI must never be reinstalled"

    unsupported = run("2.80.0", " -Verify")
    assert unsupported.returncode == 1
    assert "older than the supported minimum" in unsupported.stderr

    assert run("2.80.0", "").returncode == 0

    republished = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$ErrorActionPreference = 'Stop'; $env:PATH = '{tmp_path}'; "
            f". '{guard}'; Update-ResearchPathFromMachine; $env:PATH",
        ],
        capture_output=True,
        text=True,
    )
    assert republished.returncode == 0, republished.stderr
    machine_path = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command",
         "[Environment]::GetEnvironmentVariable('Path', 'Machine')"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert republished.stdout.strip().startswith(machine_path)
    assert str(tmp_path) in republished.stdout


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


def test_apim_tool_put_retries_a_transient_transport_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.provider_onboarding import ApimOnboarder

    onboarder = ApimOnboarder.__new__(ApimOnboarder)
    attempts = 0
    sleeps: list[float] = []
    response = httpx.Response(200, request=httpx.Request("PUT", "https://example.test"))

    def put(_path: str, _body: dict[str, Any], *, api_version: str) -> httpx.Response:
        nonlocal attempts
        del api_version
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError(
                "connection reset",
                request=httpx.Request("PUT", "https://example.test"),
            )
        return response

    monkeypatch.setattr(onboarder, "_put", put)
    monkeypatch.setattr("scripts.provider_onboarding.APIM_TOOL_RETRY_DELAYS", (0, 0))
    monkeypatch.setattr("scripts.provider_onboarding.time.sleep", sleeps.append)

    result = onboarder._put_with_retry("/apis/test/tools/test", {}, label="test tool")

    assert result is response
    assert attempts == 2
    assert sleeps == []


def test_apim_tool_resource_name_rotates_with_the_service() -> None:
    first = apim_tool_resource_name(
        "research_arxiv_lookup",
        "apim-first-incarnation",
    )
    second = apim_tool_resource_name(
        "research_arxiv_lookup",
        "apim-second-incarnation",
    )

    assert first.startswith("research_arxiv_lookup_")
    assert second.startswith("research_arxiv_lookup_")
    assert first != second
    assert len(first) == len("research_arxiv_lookup_") + 12