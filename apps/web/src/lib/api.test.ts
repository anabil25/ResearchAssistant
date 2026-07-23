/**
 * Direct tests of `lib/api.ts` against a mocked `global.fetch`. Every
 * component test in this project mocks the whole `@/lib/api` module, so this
 * is the only place that exercises the real request shapes (URL/method/body)
 * and the shared `apiFetch`/`ApiError` error-classification logic — both are
 * new/changed Round 2 logic and must be covered independently of the UI.
 */
import {
  ApiError,
  applyBuilderProposal,
  createAgentDraft,
  decideApproval,
  forgetAgentMemoryScope,
  forkAgent,
  getAgentConnections,
  getAgentDeployment,
  getAgentDraft,
  getAgentEvaluation,
  getAgentHealth,
  getAgentMemory,
  getAgentRelease,
  getAgentReleases,
  getAgentStudioCatalog,
  getAgentTraces,
  getCapabilityDescriptors,
  getCapabilityDiscovery,
  getCapabilityInstances,
  getProjectModels,
  getWorkspaceData,
  ingestLibraryItem,
  postBuilderMessage,
  runStudio,
  testConnector,
  updateAgentMemoryScope,
  updateConnector,
  updateSettings,
  uploadLibraryItem,
} from "@/lib/api";

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 500): Response {
  return {
    ok,
    status,
    json: async () => body,
  } as Response;
}

describe("apiFetch / ApiError", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it("resolves with the parsed JSON body on a 2xx response", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ hello: "world" }));
    await expect(getAgentStudioCatalog()).resolves.toEqual({ hello: "world" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents",
      expect.objectContaining({
        headers: expect.objectContaining({ "Content-Type": "application/json" }),
      }),
    );
  });

  it("throws an ApiError carrying the real status and the payload's detail message", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "not found" }, false, 404));
    const error = await getAgentDraft("literature").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).message).toBe("not found");
    expect((error as ApiError).name).toBe("ApiError");
  });

  it("falls back to the payload's error field when detail is absent", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: "denied" }, false, 403));
    const error = await getAgentDraft("literature").catch((e: unknown) => e);
    expect((error as ApiError).message).toBe("denied");
  });

  it("falls back to a generic status message when the error body has neither detail nor error", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 502));
    const error = await getAgentDraft("literature").catch((e: unknown) => e);
    expect((error as ApiError).message).toBe("Research API returned 502");
  });

  it("falls back to a generic status message when the error body is not valid JSON", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    const error = await getAgentDraft("literature").catch((e: unknown) => e);
    expect((error as ApiError).message).toBe("Research API returned 500");
    expect((error as ApiError).status).toBe(500);
  });
});

describe("legacy workspace + studio + connector + settings + library endpoints", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn().mockResolvedValue(jsonResponse({}));
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it("fetches all eight workspace resources in parallel and assembles WorkspaceData", async () => {
    const responses: Record<string, unknown> = {
      "/api/backend/api/workspace": { name: "ws" },
      "/api/backend/api/library": [{ id: "l1" }],
      "/api/backend/api/runs": [{ id: "r1" }],
      "/api/backend/api/approvals": [{ id: "a1" }],
      "/api/backend/api/connectors": [{ id: "c1" }],
      "/api/backend/api/settings": { id: "s1" },
      "/api/backend/api/agents": [{ id: "ag1" }],
      "/api/backend/api/workflows": [{ id: "w1" }],
    };
    fetchMock.mockImplementation(async (url: string) => jsonResponse(responses[url]));

    const data = await getWorkspaceData();
    expect(data).toEqual({
      summary: { name: "ws" },
      library: [{ id: "l1" }],
      runs: [{ id: "r1" }],
      approvals: [{ id: "a1" }],
      connectors: [{ id: "c1" }],
      settings: { id: "s1" },
      agents: [{ id: "ag1" }],
      workflows: [{ id: "w1" }],
    });
    expect(fetchMock).toHaveBeenCalledTimes(8);
  });

  it("runs a studio capability with explicit options", async () => {
    await runStudio("literature", "find papers", {
      onlineResearch: true,
      inputs: { limit: 5 },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/studios/literature/run",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          objective: "find papers",
          online_research: true,
          inputs: { limit: 5 },
        }),
      }),
    );
  });

  it("runs a studio capability with default options omitted", async () => {
    await runStudio("literature", "find papers");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/studios/literature/run",
      expect.objectContaining({
        body: JSON.stringify({
          objective: "find papers",
          online_research: false,
          inputs: {},
        }),
      }),
    );
  });

  it("decides an approval", async () => {
    await decideApproval("appr-1", "approved", "looks good");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/approvals/appr-1/decision",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision: "approved", rationale: "looks good" }),
      }),
    );
  });

  it("tests a connector", async () => {
    await testConnector("conn-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/connectors/conn-1/test",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("updates a connector, sending only enabled and assigned_agents", async () => {
    await updateConnector({
      id: "conn-1",
      name: "Connector",
      kind: "search",
      enabled: true,
      assigned_agents: ["literature"],
      status: "ready",
    } as unknown as Parameters<typeof updateConnector>[0]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/connectors/conn-1",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ enabled: true, assigned_agents: ["literature"] }),
      }),
    );
  });

  it("updates project settings", async () => {
    const settings = { id: "s1" } as unknown as Parameters<typeof updateSettings>[0];
    await updateSettings(settings);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/settings",
      expect.objectContaining({ method: "PUT", body: JSON.stringify(settings) }),
    );
  });

  it("ingests a library item", async () => {
    const payload = {
      title: "t",
      kind: "paper",
      source: "s",
      access: "public",
      license: "cc",
      description: "d",
    };
    await ingestLibraryItem(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/library/ingest",
      expect.objectContaining({ method: "POST", body: JSON.stringify(payload) }),
    );
  });

  it("uploads a library item via FormData and resolves the parsed body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ item: { id: "l1" }, run: { id: "r1" } }));
    const formData = new FormData();
    formData.append("file", new Blob(["x"]), "x.txt");
    await expect(uploadLibraryItem(formData)).resolves.toEqual({
      item: { id: "l1" },
      run: { id: "r1" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/library/upload",
      expect.objectContaining({ method: "POST", body: formData }),
    );
  });

  it("throws a plain Error (not ApiError) with the detail message when the upload fails", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "too large" }, false, 413));
    const error = await uploadLibraryItem(new FormData()).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(Error);
    expect(error).not.toBeInstanceOf(ApiError);
    expect((error as Error).message).toBe("too large");
  });

  it("throws a plain Error with the error field when detail is absent on upload failure", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: "denied" }, false, 403));
    const error = await uploadLibraryItem(new FormData()).catch((e: unknown) => e);
    expect((error as Error).message).toBe("denied");
  });

  it("falls back to a generic status message when the upload error body has neither field", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 500));
    const error = await uploadLibraryItem(new FormData()).catch((e: unknown) => e);
    expect((error as Error).message).toBe("Research API returned 500");
  });

  it("falls back to a generic status message when the upload error body is not valid JSON", async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    const error = await uploadLibraryItem(new FormData()).catch((e: unknown) => e);
    expect((error as Error).message).toBe("Research API returned 500");
  });
});

describe("agent studio endpoints (/agent-studio/...)", () => {
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn().mockResolvedValue(jsonResponse({}));
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it("gets the released-agent catalog", async () => {
    await getAgentStudioCatalog();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents",
      expect.anything(),
    );
  });

  it("gets one exact release contract by version", async () => {
    await getAgentRelease("literature", "1.2.0");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/releases/1.2.0",
      expect.anything(),
    );
  });

  it("gets the full release history for an agent", async () => {
    await getAgentReleases("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/releases",
      expect.anything(),
    );
  });

  it("gets the mutable draft contract", async () => {
    await getAgentDraft("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/draft",
      expect.anything(),
    );
  });

  it("gets the capability descriptor catalog", async () => {
    await getCapabilityDescriptors();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/capabilities/descriptors",
      expect.anything(),
    );
  });

  it("gets the discovered capability instance catalog", async () => {
    await getCapabilityInstances();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/capabilities/instances",
      expect.anything(),
    );
  });

  it("gets the combined capability discovery aggregate", async () => {
    await getCapabilityDiscovery();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/capabilities/discovery",
      expect.anything(),
    );
  });

  it("gets discovered project model deployments", async () => {
    await getProjectModels();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/models",
      expect.anything(),
    );
  });

  it("gets an agent's bound connections", async () => {
    await getAgentConnections("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/connections",
      expect.anything(),
    );
  });

  it("gets an agent's health summary", async () => {
    await getAgentHealth("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/health",
      expect.anything(),
    );
  });

  it("gets an agent's advisory evaluation summary", async () => {
    await getAgentEvaluation("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/evaluation",
      expect.anything(),
    );
  });

  it("gets an agent's deployment status", async () => {
    await getAgentDeployment("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/deployment",
      expect.anything(),
    );
  });

  it("gets an agent's recent traces", async () => {
    await getAgentTraces("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/traces",
      expect.anything(),
    );
  });

  it("gets an agent's per-scope memory view", async () => {
    await getAgentMemory("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/memory",
      expect.anything(),
    );
  });

  it("updates one memory scope with a partial patch", async () => {
    await updateAgentMemoryScope("literature", "conversation", {
      enabled: true,
      retention_days: 14,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/memory/conversation",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ enabled: true, retention_days: 14 }),
      }),
    );
  });

  it("forgets one memory scope", async () => {
    await forgetAgentMemoryScope("literature", "project");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/memory/project/forget",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("posts a builder message with the message and base etag", async () => {
    await postBuilderMessage("draft-1", "add a citation tool", "etag-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/drafts/draft-1/builder/messages",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ message: "add a citation tool", base_etag: "etag-1" }),
      }),
    );
  });

  it("applies a builder proposal with the proposal id and base etag", async () => {
    await applyBuilderProposal("draft-1", "proposal-1", "etag-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/drafts/draft-1/proposals/proposal-1/apply",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ base_etag: "etag-1" }),
      }),
    );
  });

  it("creates a new draft from an intent", async () => {
    const intent = { kind: "blank" } as unknown as Parameters<typeof createAgentDraft>[0];
    await createAgentDraft(intent);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/drafts",
      expect.objectContaining({ method: "POST", body: JSON.stringify(intent) }),
    );
  });

  it("forks an agent without a version, omitting the query string", async () => {
    await forkAgent("literature");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/fork",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("forks a specific released version, url-encoding the version", async () => {
    await forkAgent("literature", "1.2.0+build");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/agent-studio/agents/literature/fork?version=1.2.0%2Bbuild",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
