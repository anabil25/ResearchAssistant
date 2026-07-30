"""Throwaway probe: prove a real deployed Hosted Agent answers a real question."""

from __future__ import annotations

import os
import sys
import time

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT = sys.argv[1] if len(sys.argv) > 1 else "literature-agent"
QUESTION = (
    sys.argv[2]
    if len(sys.argv) > 2
    else "In two sentences, what makes a literature synthesis auditable?"
)
USER_IDENTITY = os.environ.get("PROBE_USER_IDENTITY")

credential = DefaultAzureCredential()
project = AIProjectClient(endpoint=ENDPOINT, credential=credential, allow_preview=True)
client = project.get_openai_client(agent_name=AGENT)

headers = {"x-ms-user-identity": USER_IDENTITY} if USER_IDENTITY else {}

print(f"agent={AGENT}")
print(f"delegated_identity={'yes' if USER_IDENTITY else 'no'}")

started = time.time()
try:
    conversation = client.conversations.create(extra_headers=headers)
except Exception as exc:  # noqa: BLE001
    print(f"CONVERSATION_FAILED {type(exc).__name__}: {str(exc)[:400]}")
    raise SystemExit(1) from exc
print(f"conversation={conversation.id} ({time.time() - started:.1f}s)")

started = time.time()
try:
    response = client.responses.create(
        input=QUESTION,
        extra_body={"conversation": conversation.id},
        extra_headers=headers,
    )
except Exception as exc:  # noqa: BLE001
    print(f"RESPONSE_FAILED {type(exc).__name__}: {str(exc)[:600]}")
    raise SystemExit(1) from exc

elapsed = time.time() - started
session_id = (response.model_extra or {}).get("agent_session_id")
text = (response.output_text or "").strip()

print(f"session={session_id}")
print(f"latency={elapsed:.1f}s")
print(f"chars={len(text)}")
print("--- OUTPUT ---")
print(text[:2000])
