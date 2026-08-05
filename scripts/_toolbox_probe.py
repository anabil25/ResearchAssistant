import asyncio, httpx
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from agent_framework import MCPStreamableHTTPTool

endpoint = "https://cog-cini62qt3a3oc.services.ai.azure.com/api/projects/researchAssistant"
name, version = "research-shared", "2"
url = f"{endpoint}/toolboxes/{name}/versions/{version}/mcp?api-version=v1"

class Auth(httpx.Auth):
    def __init__(self, tp): self._tp = tp
    def auth_flow(self, r):
        r.headers["Authorization"] = f"Bearer {self._tp()}"
        yield r

async def main():
    cred = DefaultAzureCredential()
    tp = get_bearer_token_provider(cred, "https://ai.azure.com/.default")
    hc = httpx.AsyncClient(auth=Auth(tp), headers={"Foundry-Features": "Toolboxes=V1Preview"}, timeout=120.0)
    tb = MCPStreamableHTTPTool(name=name, url=url, http_client=hc, load_prompts=False)
    try:
        await tb.connect()
        await tb.load_tools()
        for f in tb.functions:
            print(" -", getattr(f, "name", f))
    finally:
        await tb.close()

asyncio.run(main())
