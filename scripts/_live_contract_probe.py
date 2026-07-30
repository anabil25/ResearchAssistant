"""Throwaway probe: send a real contract envelope to a deployed agent."""

from __future__ import annotations

import json
import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT = sys.argv[1] if len(sys.argv) > 1 else "literature-agent"
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "What makes a synthesis auditable?"

envelope: dict[str, object] = {
    "query": QUESTION,
    "tenant_id": os.environ.get("AZURE_TENANT_ID", "tenant"),
    "project_id": os.environ.get("AZURE_AI_PROJECT_NAME", "project"),
    "principal_id": "probe-user",
    "session_id": "probe-session",
}
if AGENT.endswith("-online-agent"):
    envelope["authorized_connector_ids"] = []
else:
    envelope["sensitivity"] = "internal"
if AGENT == "dataset-agent":
    envelope["dataset_id"] = "probe-dataset"

project = AIProjectClient(
    endpoint=ENDPOINT, credential=DefaultAzureCredential(), allow_preview=True
)
client = project.get_openai_client(agent_name=AGENT)
conversation = client.conversations.create()

started = time.time()
response = client.responses.create(
    input=json.dumps(envelope, separators=(",", ":")),
    extra_body={"conversation": conversation.id},
)
elapsed = time.time() - started

print(f"agent={AGENT}  status={getattr(response, 'status', None)}  latency={elapsed:.1f}s")
if getattr(response, "error", None):
    print(f"ERROR={response.error}")
text = (response.output_text or "").strip()
print(f"chars={len(text)}")
print("--- OUTPUT ---")
print(text[:2500])
