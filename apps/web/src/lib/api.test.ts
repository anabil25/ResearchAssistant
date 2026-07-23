import {
  decideApproval,
  getWorkspaceData,
  ingestLibraryItem,
  runStudio,
  testConnector,
  updateConnector,
  updateSettings,
  uploadLibraryItem,
} from "./api";
import type { ConnectorSetting, ProjectSettings } from "./types";

const fetchMock = jest.fn();

function response(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: jest.fn().mockResolvedValue(payload),
  } as unknown as Response;
}

beforeEach(() => {
  fetchMock.mockReset();
  global.fetch = fetchMock;
});

it("loads every workspace surface in its stable response order", async () => {
  const payloads = [
    { project_id: "demo" },
    [{ id: "library" }],
    [{ id: "run" }],
    [{ id: "approval" }],
    [{ id: "connector" }],
    { name: "settings" },
    [{ id: "agent" }],
    [{ id: "workflow" }],
  ];
  payloads.forEach((payload) => fetchMock.mockResolvedValueOnce(response(payload)));

  await expect(getWorkspaceData()).resolves.toEqual({
    summary: payloads[0],
    library: payloads[1],
    runs: payloads[2],
    approvals: payloads[3],
    connectors: payloads[4],
    settings: payloads[5],
    agents: payloads[6],
    workflows: payloads[7],
  });
  expect(fetchMock).toHaveBeenCalledTimes(8);
});

it("sends typed studio, approval, connector, settings, and ingest mutations", async () => {
  fetchMock.mockResolvedValue(response({ ok: true }));
  await runStudio("dataset", "Analyze", {
    onlineResearch: true,
    inputs: { analysis_approved: true },
  });
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/studios/dataset/run",
    expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        objective: "Analyze",
        online_research: true,
        inputs: { analysis_approved: true },
      }),
    }),
  );

  await runStudio("literature", "Review");
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/studios/literature/run",
    expect.objectContaining({
      body: JSON.stringify({
        objective: "Review",
        online_research: false,
        inputs: {},
      }),
    }),
  );

  await decideApproval("approval-1", "approved", "Reviewed");
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/approvals/approval-1/decision",
    expect.objectContaining({
      body: JSON.stringify({
        decision: "approved",
        rationale: "Reviewed",
      }),
    }),
  );

  await testConnector("pubmed");
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/connectors/pubmed/test",
    expect.objectContaining({ method: "POST" }),
  );

  const connector = {
    id: "openalex",
    enabled: false,
    assigned_agents: ["literature"],
  } as ConnectorSetting;
  await updateConnector(connector);
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/connectors/openalex",
    expect.objectContaining({
      body: JSON.stringify({
        enabled: false,
        assigned_agents: ["literature"],
      }),
    }),
  );

  const settings = { online_research_default: false } as ProjectSettings;
  await updateSettings(settings);
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/settings",
    expect.objectContaining({ body: JSON.stringify(settings) }),
  );

  const ingest = {
    title: "Dataset",
    kind: "Dataset",
    source: "Upload",
    access: "internal",
    license: "Project supplied",
    description: "Bounded fixture",
  };
  await ingestLibraryItem(ingest);
  expect(fetchMock).toHaveBeenLastCalledWith(
    "/api/backend/api/library/ingest",
    expect.objectContaining({ body: JSON.stringify(ingest) }),
  );
});

it.each([
  [{ detail: "Detailed failure" }, "Detailed failure"],
  [{ error: "Provider failure" }, "Provider failure"],
])("surfaces structured API failures", async (payload, expected) => {
  fetchMock.mockResolvedValue(response(payload, 422));
  await expect(testConnector("broken")).rejects.toThrow(expected);
});

it("uses the HTTP status when an API error body is not JSON", async () => {
  fetchMock.mockResolvedValue({
    ok: false,
    status: 503,
    json: jest.fn().mockRejectedValue(new Error("not json")),
  });
  await expect(testConnector("broken")).rejects.toThrow(
    "Research API returned 503",
  );
});

it("uploads multipart data without forcing a JSON content type", async () => {
  fetchMock.mockResolvedValue(
    response({ item: { id: "item" }, run: { id: "run" } }),
  );
  const form = new FormData();
  form.append("title", "sample.csv");

  await uploadLibraryItem(form);

  expect(fetchMock).toHaveBeenCalledWith("/api/backend/api/library/upload", {
    method: "POST",
    body: form,
  });
});

it.each([
  [{ detail: "Upload rejected" }, "Upload rejected"],
  [{ error: "Storage failed" }, "Storage failed"],
])("surfaces structured upload failures", async (payload, expected) => {
  fetchMock.mockResolvedValue(response(payload, 422));
  await expect(uploadLibraryItem(new FormData())).rejects.toThrow(expected);
});

it("uses the HTTP status when an upload error body is not JSON", async () => {
  fetchMock.mockResolvedValue({
    ok: false,
    status: 502,
    json: jest.fn().mockRejectedValue(new Error("not json")),
  });
  await expect(uploadLibraryItem(new FormData())).rejects.toThrow(
    "Research API returned 502",
  );
});
