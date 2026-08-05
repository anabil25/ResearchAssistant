import json, os, sys
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
sys.path.insert(0, "scripts")
from smoke_screening_agent import REQUEST
ep = "https://cog-cini62qt3a3oc.services.ai.azure.com/api/projects/researchAssistant"
with AIProjectClient(endpoint=ep, credential=DefaultAzureCredential(), allow_preview=True) as p:
    c = p.get_openai_client(agent_name="screening-agent")
    r = c.responses.create(input=json.dumps(REQUEST))
    print(r.output_text[:2000])
