import {
  activateProject,
  createProject,
  decideApproval,
  getWorkspaceData,
  ingestLibraryItem,
  listProjects,
  runStudio,
  testConnector,
  updateConnector,
  updateProject,
  updateSettings,
  uploadLibraryItem,
} from "./api";
import type {
  ConnectorSetting,
  ProjectSettings,
} from "./types";

const fetchMock = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: jest.fn().mockResolvedValue(body),
  } as unknown as Response;
}

function invalidJsonResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: jest.fn().mockRejectedValue(new SyntaxError("Invalid JSON")),
  } as unknown as Response;
}

describe("research API client", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    global.fetch = fetchMock;
  });

  it("loads every workspace collection in its stable contract order", async () => {
    const payloads = [
      { project_name: "Research" },
      [{ id: "library-1" }],
      [{ id: "run-1" }],
      [{ id: "approval-1" }],
      [{ id: "connector-1" }],
      { project_name: "Research" },
      [{ id: "agent-1" }],
      [{ id: "workflow-1" }],
    ];
    payloads.forEach((payload) =>
      fetchMock.mockResolvedValueOnce(jsonResponse(payload)),
    );

    const result = await getWorkspaceData();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/backend/api/workspace",
      "/api/backend/api/library",
      "/api/backend/api/runs",
      "/api/backend/api/approvals",
      "/api/backend/api/connectors",
      "/api/backend/api/settings",
      "/api/backend/api/agents",
      "/api/backend/api/workflows",
    ]);
    expect(result.summary).toEqual(payloads[0]);
    expect(result.workflows).toEqual(payloads[7]);
    expect(
      fetchMock.mock.calls.every(([, init]) => {
        const headers = init?.headers as Record<string, string>;
        return headers["Content-Type"] === "application/json";
      }),
    ).toBe(true);
  });

  it("scopes workspace reads and project lifecycle calls to the project contract", async () => {
    const projectId = "project-0123456789abcdef0123456789abcdef";
    const project = {
      id: projectId,
      name: "Cancer outcomes review",
      description: "A private workspace for a bounded evidence review.",
      active_runs: 0,
      source_count: 0,
      is_active: true,
    };
    Array.from({ length: 8 }, () => ({ project_name: "Research" })).forEach(
      (payload) => fetchMock.mockResolvedValueOnce(jsonResponse(payload)),
    );
    fetchMock.mockResolvedValueOnce(jsonResponse([project]));
    fetchMock.mockResolvedValueOnce(jsonResponse(project));
    fetchMock.mockResolvedValueOnce(jsonResponse(project));
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...project, is_active: false }));

    await getWorkspaceData(projectId);
    expect(
      fetchMock.mock.calls.slice(0, 7).every(([, init]) => {
        const headers = init?.headers as Record<string, string>;
        return headers["X-Research-Project-ID"] === projectId;
      }),
    ).toBe(true);
    expect(
      (fetchMock.mock.calls[7][1]?.headers as Record<string, string>)[
        "X-Research-Project-ID"
      ],
    ).toBeUndefined();

    await listProjects();
    await createProject({ name: project.name, description: project.description });
    await activateProject(projectId);
    await updateProject(projectId, { archive: true });

    expect(fetchMock.mock.calls[8][0]).toBe("/api/backend/api/projects");
    expect(fetchMock.mock.calls[9][1]?.body).toBe(
      JSON.stringify({ name: project.name, description: project.description }),
    );
    expect(fetchMock.mock.calls[10][0]).toBe(
      `/api/backend/api/projects/${projectId}/activate`,
    );
    expect(fetchMock.mock.calls[11][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ archive: true }),
    });
  });

  it("sends bounded studio, approval, connector, settings, and ingestion writes", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "result" }));
    const connector = {
      id: "connector-1",
      enabled: true,
      assigned_agents: ["literature-agent"],
    } as ConnectorSetting;
    const settings: ProjectSettings = {
      allowed_export_destinations: [],
      citation_coverage_threshold: 1,
      default_classification: "internal",
      description: "Evidence workspace",
      evaluation_policy: "required",
      model_profile: "bounded",
      name: "Research",
      online_research_default: false,
      project_id: "project-1",
      require_human_approval: true,
      retention_days: 30,
    };

    await runStudio("literature", "Bounded question");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/backend/api/studios/literature/run",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          objective: "Bounded question",
          online_research: false,
          inputs: {},
        }),
      }),
    );

    await runStudio("grant", "Current funding", {
      onlineResearch: true,
      inputs: { source: "grants.gov" },
    });
    await decideApproval("approval-1", "approved", "Evidence verified.");
    await testConnector("connector-1");
    await updateConnector(connector);
    await updateSettings(settings);
    await ingestLibraryItem({
      title: "Protocol",
      kind: "document",
      source: "upload",
      access: "project",
      license: "internal",
      description: "Bounded protocol",
    });

    expect(fetchMock).toHaveBeenCalledTimes(7);
    expect(fetchMock.mock.calls[1][1]?.body).toBe(
      JSON.stringify({
        objective: "Current funding",
        online_research: true,
        inputs: { source: "grants.gov" },
      }),
    );
    expect(fetchMock.mock.calls[2][1]?.body).toBe(
      JSON.stringify({
        decision: "approved",
        rationale: "Evidence verified.",
      }),
    );
    expect(fetchMock.mock.calls[4][1]?.body).toBe(
      JSON.stringify({
        enabled: true,
        assigned_agents: ["literature-agent"],
      }),
    );
  });

  it("surfaces structured and fallback API errors", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Approval is no longer pending." }, 409),
    );
    await expect(testConnector("connector-1")).rejects.toThrow(
      "Approval is no longer pending.",
    );

    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "denied" }, 403));
    await expect(testConnector("connector-1")).rejects.toThrow("denied");

    fetchMock.mockResolvedValueOnce(invalidJsonResponse(502));
    await expect(testConnector("connector-1")).rejects.toThrow(
      "Research API returned 502",
    );
  });

  it("uploads multipart bodies without forcing a JSON content type", async () => {
    const form = new FormData();
    form.set("title", "Dataset");
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ item: { id: "item-1" }, run: { id: "run-1" } }),
    );

    const result = await uploadLibraryItem(form);

    expect(result).toEqual({
      item: { id: "item-1" },
      run: { id: "run-1" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/backend/api/library/upload",
      { method: "POST", body: form },
    );

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "File is too large." }, 413),
    );
    await expect(uploadLibraryItem(form)).rejects.toThrow("File is too large.");

    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "blocked" }, 400));
    await expect(uploadLibraryItem(form)).rejects.toThrow("blocked");

    fetchMock.mockResolvedValueOnce(invalidJsonResponse(500));
    await expect(uploadLibraryItem(form)).rejects.toThrow(
      "Research API returned 500",
    );
  });
});
