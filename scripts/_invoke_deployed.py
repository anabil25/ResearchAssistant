import json, sys
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
ep = "https://cog-cini62qt3a3oc.services.ai.azure.com/api/projects/researchAssistant"
req = {"query":"Screen the library for randomised trials of AI triage in adult emergency care. Exclude editorials, protocols without results, and paediatric-only studies.","tenant_id":"demo","project_id":"researchAssistant","principal_id":"smoke","session_id":"tb-1","sensitivity":"internal"}
with AIProjectClient(endpoint=ep, credential=DefaultAzureCredential(), allow_preview=True) as p:
    c = p.get_openai_client(agent_name="screening-agent")
    r = c.responses.create(input=json.dumps(req))
    d = r.model_dump()
    print("status:", d.get("status"), "| error:", json.dumps(d.get("error"))[:400])
    print(r.output_text[-1200:])
