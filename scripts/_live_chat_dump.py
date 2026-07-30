"""Throwaway probe: dump the full raw response shape from a deployed agent."""

from __future__ import annotations

import json
import os
import sys

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT = sys.argv[1] if len(sys.argv) > 1 else "literature-agent"
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "What makes a synthesis auditable?"

project = AIProjectClient(
    endpoint=ENDPOINT, credential=DefaultAzureCredential(), allow_preview=True
)
client = project.get_openai_client(agent_name=AGENT)

conversation = client.conversations.create()
response = client.responses.create(
    input=QUESTION, extra_body={"conversation": conversation.id}
)

print(f"status={getattr(response, 'status', None)}")
print(f"output_text={response.output_text!r}")
print(f"incomplete={getattr(response, 'incomplete_details', None)}")
print(f"error={getattr(response, 'error', None)}")

for index, item in enumerate(response.output or []):
    kind = getattr(item, "type", "?")
    print(f"\n[{index}] type={kind}")
    for attr in ("status", "name", "arguments", "role"):
        if hasattr(item, attr):
            print(f"    {attr}={getattr(item, attr)!r}")
    for part in getattr(item, "content", None) or []:
        ptype = getattr(part, "type", "?")
        text = getattr(part, "text", None)
        refusal = getattr(part, "refusal", None)
        print(f"    part.type={ptype} text={str(text)[:600]!r} refusal={refusal!r}")

dumped = response.model_dump()
print("\n--- RAW (truncated) ---")
print(json.dumps(dumped, indent=2, default=str)[:2500])
