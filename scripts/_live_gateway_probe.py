"""Reproduce the API's exact hosted-agent call path (session + conversation + delegated identity)."""

from __future__ import annotations

import json
import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT = os.environ.get("PROBE_AGENT", "dataset-agent")
IDENTITY = "ra:probe0000000000000000000000000000000000000000000000000000000000"

body = {
    "query": "Summarize what a dataset analysis agent can do for reproducible research.",
    "tenant_id": os.environ.get("PROBE_TENANT", "probe-tenant"),
    "project_id": "probe-project",
    "principal_id": "probe-user",
    "session_id": "probe-session",
    "sensitivity": "internal",
}
if AGENT == "dataset-agent":
    body["dataset_id"] = "probe-dataset"

project = AIProjectClient(endpoint=ENDPOINT, credential=DefaultAzureCredential(), allow_preview=True)
headers = {"x-ms-user-identity": IDENTITY}

session = project.agents.create_session(agent_name=AGENT, body={}, headers=headers)
client = project.get_openai_client(agent_name=AGENT)
conversation = client.conversations.create(extra_headers=headers)
session_id = getattr(session, "agent_session_id", None) or getattr(session, "id", None)
print(f"session={session_id} conversation={conversation.id}")

response = client.responses.create(
    input=json.dumps(body, separators=(",", ":")),
    extra_body={"conversation": conversation.id, "agent_session_id": session_id},
    extra_headers=headers,
)
print(f"status={getattr(response, 'status', None)}")
print(f"error={getattr(response, 'error', None)}")
text = (response.output_text or "").strip()
print(f"chars={len(text)}")
print(text[:1500])
