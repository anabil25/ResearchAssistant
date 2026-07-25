import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import { ResearchWorkbench } from "@/components/research-workbench";
import {
  decideApproval,
  getWorkspaceData,
  runStudio,
  testConnector,
  updateConnector,
  updateSettings,
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";

jest.mock("@/lib/api", () => ({
  getWorkspaceData: jest.fn(),
  runStudio: jest.fn(),
  decideApproval: jest.fn(),
  testConnector: jest.fn(),
  updateConnector: jest.fn(),
  updateSettings: jest.fn(),
  uploadLibraryItem: jest.fn(),
}));

const capabilities = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "institutional_qa",
  "orchestration",
] as const;

const workspaceData: WorkspaceData = {
  summary: {
    project: {
      project_id: "demo-project",
      name: "V2 test workspace",
      description: "A governed test workspace.",
      default_classification: "internal",
      online_research_default: false,
      retention_days: 2555,
      citation_coverage_threshold: 1,
      require_human_approval: true,
      allowed_export_destinations: ["Workspace Library"],
      model_profile: "Balanced quality",
      evaluation_policy: "Block unresolved citations",
    },
    library_items: 1,
    active_runs: 1,
    pending_approvals: 1,
    connector_ready: 1,
    connector_total: 1,
    last_activity_at: "2026-07-16T12:00:00Z",
    persistence: "in-memory demo",
  },
  library: [
    {
      id: "paper-1",
      title: "Evidence workflow study",
      kind: "Paper",
      source: "PubMed",
      status: "ready",
      access: "public",
      version: "1.0",
      checksum: "sha256:test",
      license: "CC BY 4.0",
      added_at: "2026-07-16T12:00:00Z",
      evidence_count: 4,
      connector: "PubMed",
      provider: "PubMed",
      publication_year: 2025,
      description: "Verified test paper.",
      tags: ["evidence"],
    },
  ],
  runs: [
    {
      id: "run-1",
      durable_instance_id: "research-run-1",
      project_id: "demo-project",
      capability: "grant",
      title: "Grant package review",
      status: "waiting_for_approval",
      progress: 86,
      current_stage: "Review & export",
      owner: "Dr. Maya Chen",
      started_at: "2026-07-16T12:00:00Z",
      completed_at: null,
      artifact_count: 4,
      approval_id: "approval-1",
      estimated_cost_usd: 0,
      scheduler_managed: false,
      scheduling_state: "not_managed",
      orchestration_input: null,
      stages: [],
    },
  ],
  approvals: [
    {
      id: "approval-1",
      run_id: "run-1",
      title: "Release grant package",
      state: "pending",
      risk: "High",
      gated_action: "Release exact package v0.8.",
      destination: "SharePoint research site",
      requested_by: "grant-agent",
      requested_at: "2026-07-16T12:00:00Z",
      evidence_summary: "Requirements checked.",
      idempotency_key: "grant-run-1-v08",
      approver_id: null,
      approver_name: null,
      decided_at: null,
      rationale: null,
      event_delivery: "not_requested",
      decision_event_id: null,
    },
  ],
  connectors: [
    {
      id: "pubmed",
      name: "PubMed",
      category: "Literature",
      description: "Biomedical citations and abstracts.",
      auth_kind: "None",
      secret_status: "Not required",
      enabled: true,
      test_status: "ready",
      last_tested_at: null,
      assigned_agents: ["literature"],
      terms_url: "https://www.ncbi.nlm.nih.gov/home/about/policies/",
      data_boundary: "Public metadata only.",
      capabilities: ["Search", "Metadata"],
    },
  ],
  settings: {
    project_id: "demo-project",
    name: "V2 test workspace",
    description: "A governed test workspace.",
    default_classification: "internal",
    online_research_default: false,
    retention_days: 2555,
    citation_coverage_threshold: 1,
    require_human_approval: true,
    allowed_export_destinations: ["Workspace Library"],
    model_profile: "Balanced quality",
    evaluation_policy: "Block unresolved citations",
  },
  agents: [
    {
      id: "literature",
      name: "Literature synthesis",
      model_tier: "Primary",
      status: "Active",
      web_access: "Opt-in public only",
      workflow_steps: ["Protocol", "Search", "Screen", "Audit"],
      deployment: "Foundry Hosted Agent",
    },
  ],
  workflows: capabilities.map((capability) => ({
    capability,
    title: `${capability} workflow`,
    purpose: "A distinct workflow.",
    primary_artifact: "Verified artifact",
    online_research_policy: "optional-public-only",
    stages: [
      {
        id: "plan",
        label: "Plan",
        description: "Plan this workflow.",
        owner: "researcher",
        human_checkpoint: false,
      },
      {
        id: "verify",
        label: "Verify",
        description: "Verify this workflow.",
        owner: "validator",
        human_checkpoint: false,
      },
    ],
  })),
};

const mockedGetWorkspaceData = jest.mocked(getWorkspaceData);

describe("ResearchWorkbench", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    mockedGetWorkspaceData.mockResolvedValue(workspaceData);
    jest.mocked(runStudio).mockReset();
    jest.mocked(decideApproval).mockReset();
    jest.mocked(testConnector).mockReset();
    jest.mocked(updateConnector).mockReset();
    jest.mocked(updateSettings).mockReset();
    jest.mocked(uploadLibraryItem).mockReset();
  });

  it("exposes every requested research capability", async () => {
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    expect(
      screen.getByRole("button", { name: /literature review synthesis/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /grant application studio/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /pi and resource matching/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /dataset and notebook summary/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /institution-grounded q&a/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /research workflow orchestration/i }),
    ).toBeInTheDocument();
  });

  it("has no automated accessibility violations on the overview", async () => {
    const { container } = render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    expect(await axe(container)).toHaveNoViolations();
  });

  it("opens the distinct Literature Studio protocol", async () => {
    const user = userEvent.setup();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(
      screen.getByRole("button", { name: /literature review synthesis/i }),
    );

    expect(
      screen.getByRole("heading", { name: "Literature Studio" }),
    ).toBeInTheDocument();
    expect(
      (screen.getByLabelText("Research question") as HTMLTextAreaElement).value,
    ).toContain("auditable retrieval");
    expect(screen.getByText("Scholarly sources")).toBeInTheDocument();
  });

  it("renders real Library and Runs data", async () => {
    const user = userEvent.setup();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: /^Library 1$/i }));
    expect(
      screen.getByRole("heading", { name: "Library" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Evidence workflow study")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /runs & approvals 1/i }),
    );
    expect(
      screen.getByRole("heading", { name: "Runs & Approvals" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Grant package review").length).toBeGreaterThan(0);
    expect(screen.getByText("Exact gated action")).toBeInTheDocument();
  });

  it("opens functional project settings and connector setup", async () => {
    const user = userEvent.setup();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(
      screen.getByRole("button", { name: "Open project settings" }),
    );
    expect(
      screen.getByRole("heading", { name: "Project Settings" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Connections 1/i }));
    expect(
      screen.getByRole("heading", { name: "Connections" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("PubMed").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Test connection" }),
    ).toBeInTheDocument();
  });

  it("shows a complete mobile navigation close control", async () => {
    const user = userEvent.setup();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const closeControls = screen.getAllByRole("button", {
      name: "Close navigation",
    });
    expect(closeControls).toHaveLength(2);
    await user.click(closeControls[1]);
    await waitFor(() =>
      expect(
        screen.getByLabelText("Project navigation"),
      ).toHaveAttribute("data-open", "false"),
    );
  });

  it("restores URL-addressable views and follows browser history", async () => {
    window.history.replaceState(null, "", "/?view=dataset");
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    expect(
      screen.getByRole("heading", { name: "Dataset Lab" }),
    ).toBeInTheDocument();

    act(() => {
      window.history.pushState(null, "", "/?view=grant");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Grant Studio" }),
      ).toBeInTheDocument(),
    );
  });
});
