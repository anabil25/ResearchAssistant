"""Throwaway probe: exercise every chat agent with its real contract envelope."""

from __future__ import annotations

import json
import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from research_assistant_core.connector_catalog import connector_definitions

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
TENANT = os.environ.get("AZURE_TENANT_ID", "tenant")
PROJECT = os.environ.get("AZURE_AI_PROJECT_NAME", "project")

AGENTS = sys.argv[1].split(",") if len(sys.argv) > 1 else [
    "literature-agent",
    "grant-agent",
    "matching-agent",
    "dataset-agent",
    "literature-online-agent",
    "grant-online-agent",
    "matching-online-agent",
]


def connectors_for(agent_name: str) -> list[str]:
    agent_id = agent_name.removesuffix("-online-agent").removesuffix("-agent")
    return [c.id for c in connector_definitions() if agent_id in c.assigned_agents]


def envelope_for(agent_name: str) -> str:
    body: dict[str, object] = {
        "query": "Give one short sentence about auditable research evidence.",
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "principal_id": "probe-user",
        "session_id": "probe-session",
    }
    if agent_name.endswith("-online-agent"):
        body["authorized_connector_ids"] = connectors_for(agent_name)
    else:
        body["sensitivity"] = "internal"
    if agent_name == "dataset-agent":
        body["dataset_id"] = "probe-dataset"
    return json.dumps(body, separators=(",", ":"))


project = AIProjectClient(
    endpoint=ENDPOINT, credential=DefaultAzureCredential(), allow_preview=True
)

for agent in AGENTS:
    client = project.get_openai_client(agent_name=agent)
    started = time.time()
    try:
        conversation = client.conversations.create()
        response = client.responses.create(
            input=envelope_for(agent),
            extra_body={"conversation": conversation.id},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"{agent:26} EXCEPTION {type(exc).__name__}: {str(exc)[:180]}")
        continue
    elapsed = time.time() - started
    status = getattr(response, "status", None)
    text = (response.output_text or "").strip()
    err = getattr(response, "error", None)
    verdict = "OK  " if text else "EMPTY"
    print(f"{agent:26} {verdict} status={status} {elapsed:5.1f}s chars={len(text)}")
    if err:
        print(f"{'':26}   error={err}")
