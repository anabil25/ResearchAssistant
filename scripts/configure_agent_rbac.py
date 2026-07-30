from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from scripts.azd_env import sync_canonical_azd_outputs

COORDINATOR = "research-coordinator"
AGENT_NAMES = (
    COORDINATOR,
    "literature-agent",
    "grant-agent",
    "matching-agent",
    "dataset-agent",
    "institution-agent",
    "literature-online-agent",
    "grant-online-agent",
    "matching-online-agent",
)
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
AZ_CLI = "az.cmd" if os.name == "nt" else "az"


def run_json(command: list[str]) -> Any:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def agent_instance_principal_id(value: Any) -> str:
    if not isinstance(value, dict):
        raise RuntimeError("Hosted Agent details are not a JSON object")
    identity = value.get("instance_identity") or value.get("instanceIdentity")
    if not isinstance(identity, dict):
        raise RuntimeError("Hosted Agent details have no instance identity")
    principal_id = identity.get("principal_id") or identity.get("principalId")
    if not isinstance(principal_id, str) or not principal_id:
        raise RuntimeError("Hosted Agent instance identity has no principal id")
    return principal_id


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing azd environment value: {name}")
    return value


def agent_environment_values(
    name: str,
    version: str,
    project_endpoint: str,
) -> dict[str, str]:
    prefix = f"AGENT_{name.replace('-', '_').upper()}"
    base = f"{project_endpoint.rstrip('/')}/agents/{name}"
    return {
        f"{prefix}_NAME": name,
        f"{prefix}_VERSION": version,
        f"{prefix}_ENDPOINT": f"{base}/versions/{version}",
        f"{prefix}_RESPONSES_ENDPOINT": (
            f"{base}/endpoint/protocols/openai/responses?api-version=v1"
        ),
    }


def sync_agent_environment_outputs() -> None:
    endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT")
    client = AIProjectClient(
        endpoint=endpoint,
        credential=AzureCliCredential(),
        allow_preview=True,
    )
    found: set[str] = set()
    for agent in client.agents.list():
        if agent.name not in AGENT_NAMES:
            continue
        latest = agent.versions.latest
        status = str(getattr(latest.status, "value", latest.status))
        if status != "active":
            raise RuntimeError(
                f"Hosted Agent {agent.name} latest version is {status}"
            )
        found.add(agent.name)
        for key, value in agent_environment_values(
            agent.name,
            str(latest.version),
            endpoint,
        ).items():
            subprocess.run(
                ["azd", "env", "set", key, value],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
    missing = sorted(set(AGENT_NAMES) - found)
    if missing:
        raise RuntimeError(f"Hosted Agent deployments are missing: {missing}")


def main() -> None:
    sync_canonical_azd_outputs()
    sync_agent_environment_outputs()
    project_scope = required_env("AZURE_AI_PROJECT_ID")
    # Every hosted agent resolves its own model deployment at startup
    # (shared.factory._resolve_model_deployment_version), so each instance
    # identity -- not just the coordinator's -- needs project data-plane read.
    for agent_name in AGENT_NAMES:
        agent = run_json(
            [
                "azd",
                "ai",
                "agent",
                "show",
                agent_name,
                "--output",
                "json",
                "--no-prompt",
            ]
        )
        principal_id = agent_instance_principal_id(agent)
        subprocess.run(
            [
                AZ_CLI,
                "role",
                "assignment",
                "create",
                "--assignee-object-id",
                principal_id,
                "--assignee-principal-type",
                "ServicePrincipal",
                "--role",
                FOUNDRY_USER_ROLE_ID,
                "--scope",
                project_scope,
                "--output",
                "none",
            ],
            check=True,
        )
        print(f"Granted Foundry User to {agent_name} ({principal_id}).")


if __name__ == "__main__":
    main()
