from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError
from azure.identity import AzureCliCredential

from scripts.azd_env import sync_canonical_azd_outputs
from scripts.build_agent_source_tree import (
    DEFAULT_OUTPUT,
    build_source_tree_manifest,
    validate_release_worktree_is_clean,
    validate_worktree_matches_commit,
    write_source_tree_manifest,
)
from scripts.configure_agent_rbac import agent_environment_values

AGENT_SERVICES = (
    "literature-agent",
    "grant-agent",
    "matching-agent",
    "dataset-agent",
    "institution-agent",
    "screening-agent",
    "research-coordinator",
)
DEPLOYMENT_ORDER = ("ai-project", *AGENT_SERVICES, "api", "web")
AGENT_SETTLE_ATTEMPTS = 120
AGENT_SETTLE_DELAY_SECONDS = 5.0
SERVICE_DEPLOY_TIMEOUT_SECONDS = 1800.0
CREATED_AT_CLOCK_SKEW_SECONDS = 30.0
RETRYABLE_STATUS_CODES = {404, 408, 409, 429, 500, 502, 503, 504}


class AgentOperations(Protocol):
    def list_versions(
        self,
        agent_name: str,
        *,
        order: str,
    ) -> Iterable[Any]: ...


@dataclass(frozen=True)
class AgentVersionState:
    version: str
    status: str
    created_at: float


@dataclass(frozen=True)
class DeployAttempt:
    returncode: int
    output: str


def _created_at_timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _version_state(value: Any) -> AgentVersionState:
    status = getattr(value, "status", "")
    return AgentVersionState(
        version=str(value.version),
        status=str(getattr(status, "value", status)).lower(),
        created_at=_created_at_timestamp(getattr(value, "created_at", 0)),
    )


def latest_agent_version(
    operations: AgentOperations,
    agent_name: str,
) -> AgentVersionState | None:
    try:
        versions = list(operations.list_versions(agent_name, order="desc"))
    except HttpResponseError as exc:
        if exc.status_code in RETRYABLE_STATUS_CODES:
            return None
        raise
    if not versions:
        return None
    return max((_version_state(version) for version in versions), key=lambda item: int(item.version))


def _is_new_version(
    candidate: AgentVersionState,
    before: AgentVersionState | None,
    started_at: float,
) -> bool:
    if before is not None:
        return int(candidate.version) > int(before.version)
    return candidate.created_at >= started_at - CREATED_AT_CLOCK_SKEW_SECONDS


def wait_for_agent_version(
    operations: AgentOperations,
    agent_name: str,
    predicate: Callable[[AgentVersionState], bool],
    *,
    attempts: int = AGENT_SETTLE_ATTEMPTS,
    delay_seconds: float = AGENT_SETTLE_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> AgentVersionState | None:
    for attempt in range(1, attempts + 1):
        try:
            latest = latest_agent_version(operations, agent_name)
        except HttpResponseError as exc:
            if exc.status_code not in RETRYABLE_STATUS_CODES:
                raise
            latest = None
        if latest is not None and predicate(latest):
            return latest
        if attempt < attempts:
            sleep(delay_seconds)
    return None


def run_azd_deploy(
    service: str,
    *,
    timeout_seconds: float = SERVICE_DEPLOY_TIMEOUT_SECONDS,
) -> DeployAttempt:
    command = ["azd", "deploy", service, "--no-prompt"]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout or ""
        output = (
            captured.decode("utf-8", errors="replace")
            if isinstance(captured, bytes)
            else captured
        )
        output += f"\nazd deploy {service} timed out after {timeout_seconds:.0f}s.\n"
        print(output, end="")
        return DeployAttempt(returncode=124, output=output)
    print(completed.stdout, end="")
    return DeployAttempt(returncode=completed.returncode, output=completed.stdout)


def prepare_agent_source_identity() -> str:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    validate_release_worktree_is_clean(Path(repo_root))
    validate_worktree_matches_commit(Path(repo_root))
    manifest = build_source_tree_manifest(Path(repo_root))
    write_source_tree_manifest(manifest, Path(repo_root) / DEFAULT_OUTPUT)
    os.environ["AGENT_SOURCE_TREE_DIGEST"] = manifest.source_tree_digest
    subprocess.run(
        ["azd", "env", "set", "AGENT_SOURCE_TREE_DIGEST", manifest.source_tree_digest],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return manifest.source_tree_digest


def persist_agent_version(
    service: str,
    state: AgentVersionState,
    project_endpoint: str,
) -> None:
    for key, value in agent_environment_values(service, state.version, project_endpoint).items():
        subprocess.run(
            ["azd", "env", "set", key, value],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


def _is_existing_agent_conflict(attempt: DeployAttempt) -> bool:
    output = attempt.output.lower()
    return attempt.returncode != 0 and "409" in output and "already exists" in output


def _failure_tail(attempt: DeployAttempt) -> str:
    lines = [line for line in attempt.output.splitlines() if line.strip()]
    return "\n".join(lines[-20:]) or f"azd exited with code {attempt.returncode}"


def _active_after_attempt(
    operations: AgentOperations,
    service: str,
    before: AgentVersionState | None,
    started_at: float,
    *,
    attempts: int,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> AgentVersionState | None:
    return wait_for_agent_version(
        operations,
        service,
        lambda candidate: candidate.status == "active" and _is_new_version(candidate, before, started_at),
        attempts=attempts,
        delay_seconds=delay_seconds,
        sleep=sleep,
    )


def deploy_agent_service(
    service: str,
    operations: AgentOperations,
    project_endpoint: str,
    *,
    run_deploy: Callable[[str], DeployAttempt] = run_azd_deploy,
    persist: Callable[[str, AgentVersionState, str], None] = persist_agent_version,
    attempts: int = AGENT_SETTLE_ATTEMPTS,
    delay_seconds: float = AGENT_SETTLE_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> AgentVersionState:
    before = latest_agent_version(operations, service)
    started_at = now()
    result = run_deploy(service)
    active: AgentVersionState | None = None

    if result.returncode == 0:
        if "already active" in result.output.lower():
            active = wait_for_agent_version(
                operations,
                service,
                lambda candidate: candidate.status == "active",
                attempts=attempts,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
        else:
            active = _active_after_attempt(
                operations,
                service,
                before,
                started_at,
                attempts=attempts,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
    else:
        settled = wait_for_agent_version(
            operations,
            service,
            lambda candidate: candidate.status in {"active", "failed"}
            and _is_new_version(candidate, before, started_at),
            attempts=attempts,
            delay_seconds=delay_seconds,
            sleep=sleep,
        )
        if settled is not None and settled.status == "active":
            active = settled
            print(
                f"Recovered {service} version {active.version}: Foundry reports active "
                "after azd returned an early failure."
            )
        elif settled is not None and _is_existing_agent_conflict(result):
            retry_started_at = now()
            retry = run_deploy(service)
            active = _active_after_attempt(
                operations,
                service,
                settled,
                retry_started_at,
                attempts=attempts,
                delay_seconds=delay_seconds,
                sleep=sleep,
            )
            if active is None:
                result = retry

    if active is None:
        raise RuntimeError(f"Sequential deployment failed for {service}:\n{_failure_tail(result)}")

    persist(service, active, project_endpoint)
    print(f"Sequential deployment confirmed {service} version {active.version} active.")
    return active


def deploy_required_service(
    service: str,
    *,
    run_deploy: Callable[[str], DeployAttempt] = run_azd_deploy,
) -> None:
    result = run_deploy(service)
    if result.returncode != 0:
        raise RuntimeError(f"Sequential deployment failed for {service}:\n{_failure_tail(result)}")


def main() -> None:
    sync_canonical_azd_outputs()
    source_digest = prepare_agent_source_identity()
    print(f"Sequential deployment source-tree digest: {source_digest}")
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    client = AIProjectClient(
        endpoint=project_endpoint,
        credential=AzureCliCredential(),
        allow_preview=True,
    )
    try:
        for service in DEPLOYMENT_ORDER:
            print(f"\n=== Sequential stage: {service} ===")
            if service in AGENT_SERVICES:
                deploy_agent_service(service, client.agents, project_endpoint)
            else:
                deploy_required_service(service)
    finally:
        client.close()


if __name__ == "__main__":
    main()