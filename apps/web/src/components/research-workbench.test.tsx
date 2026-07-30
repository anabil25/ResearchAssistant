import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import { ResearchWorkbench } from "@/components/research-workbench";
import { CAPABILITY_CARDS } from "@/components/workspace-views";
import { openBlockingModal } from "@/lib/blocking-modal";
import {
  activateProject,
  createProject,
  decideApproval,
  getChatThread,
  getWorkspaceData,
  listChatAgents,
  listProjects,
  openChatThread,
  runStudio,
  sendChatMessage,
  testConnector,
  updateConnector,
  updateProject,
  updateSettings,
  uploadChatFile,
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";
import type {
  AutomationStudioResult,
  ChatThread,
  LiteratureStudioResult,
  StudioRun,
} from "@/lib/types";
import type { ComponentType } from "react";

/**
 * `userEvent.setup` with the artificial inter-event delay removed. See the
 * identical helper in `studio-components.test.tsx` for the full rationale.
 *
 * Short version: userEvent v14 defaults to `delay: 0`, which awaits a real
 * `setTimeout(..., 0)` between *every* dispatched event. The omnibus tests in
 * this file perform dozens of interactions each, so they accumulate enough of
 * those hops to sit at ~70% of Jest's 5s default budget under the full
 * `--runInBand` suite -- close enough that a slower machine tips them over,
 * which is exactly how this file's timeout-class flake was reported.
 *
 * Safe here despite `research-workbench.tsx` owning a real polling timer: the
 * option removes userEvent's *own* waiting, not the component's timers, and no
 * assertion in this file depends on time passing between two events. It is
 * also strictly safer for the three tests that install fake timers, since
 * userEvent's default delay needs an `advanceTimers` bridge to work under them
 * at all.
 */
function setupUser(
  options: Parameters<typeof userEvent.setup>[0] = {},
): ReturnType<typeof userEvent.setup> {
  return userEvent.setup({ delay: null, ...options });
}

jest.mock("@/lib/api", () => ({
  // `classifyAsyncError` narrows on `instanceof ApiError`, so the real class
  // has to survive the mock or every error path throws instead of rendering.
  ApiError: jest.requireActual<typeof import("@/lib/api")>("@/lib/api").ApiError,
  activateProject: jest.fn(),
  createProject: jest.fn(),
  getCapabilityDiscovery: jest.fn(),
  getFoundryAgentInventory: jest.fn(),
  getFoundryProjectContext: jest.fn(),
  getFoundryProjectModels: jest.fn(),
  getWorkspaceData: jest.fn(),
  listProjects: jest.fn(),
  runStudio: jest.fn(),
  listChatAgents: jest.fn(),
  openChatThread: jest.fn(),
  getChatThread: jest.fn(),
  sendChatMessage: jest.fn(),
  uploadChatFile: jest.fn(),
  attachPromptCapability: jest.fn(),
  createPromptAgentDraft: jest.fn(),
  savePromptAgentDraft: jest.fn(),
  decideApproval: jest.fn(),
  testConnector: jest.fn(),
  updateConnector: jest.fn(),
  updateProject: jest.fn(),
  updateSettings: jest.fn(),
  uploadLibraryItem: jest.fn(),
}));

jest.mock("@/components/research-markdown", () => ({
  ResearchMarkdown: ({
    content,
    citations = [],
    unresolvedSourceIds = [],
    label = "Research artifact",
  }: {
    content: string;
    citations?: { id: string }[];
    unresolvedSourceIds?: string[];
    label?: string;
  }) => (
    <section aria-label={label}>
      <p>{content}</p>
      {citations.length ? <span>Resolved evidence</span> : null}
      {unresolvedSourceIds.length ? <span>Unsupported references</span> : null}
      {unresolvedSourceIds.length ? (
        <span>{unresolvedSourceIds.join(", ")}</span>
      ) : null}
    </section>
  ),
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
      credential_kind: "none",
      credential_required: false,
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

function cloneWorkspaceData(): WorkspaceData {
  return JSON.parse(JSON.stringify(workspaceData)) as WorkspaceData;
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function baseRun(overrides: Partial<StudioRun> = {}): StudioRun {
  return {
    capability: "literature",
    current_stage: "Complete",
    durable_instance_id: "research-run-test",
    id: "run-test",
    owner: "Dr. Maya Chen",
    progress: 100,
    started_at: "2026-07-16T12:00:00Z",
    status: "completed",
    title: "Test run",
    ...overrides,
  };
}

const literatureResult: LiteratureStudioResult = {
  run: baseRun({
    capability: "literature",
    id: "run-lit-1",
    durable_instance_id: "research-run-lit-1",
    title: "Auditable literature review",
    progress: 92,
    current_stage: "Synthesis",
  }),
  protocol: {
    research_question: "How can auditable synthesis stay deterministic?",
    date_from: 2020,
    date_to: 2026,
    sources: ["PubMed", "Crossref"],
    inclusion_criteria: ["Primary study"],
    exclusion_criteria: ["Duplicate"],
  },
  search_queries: ["How can auditable synthesis stay deterministic?"],
  candidate_count: 2,
  screening: [
    {
      source_id: "source-1",
      title: "Study A",
      decision: "include",
      reason: "Matches protocol",
      duplicate_group: null,
    },
    {
      source_id: "source-2",
      title: "Study B",
      decision: "maybe",
      reason: "Needs manual follow-up",
      duplicate_group: null,
    },
  ],
  extraction_matrix: [
    {
      source_id: "source-1",
      method: "Method A",
      population: "Population A",
      outcome: "Outcome A",
      limitation: "Limitation A",
      citation_ids: ["cite-1"],
    },
  ],
  synthesis: ["Verified insight from stored citations."],
  citations: [
    {
      id: "cite-1",
      title: "Study A",
      section: "Results",
      quote: "Quote A",
      source_id: "source-1",
      checksum: "sha256:a",
      license: "CC BY",
      chunk_id: "chunk-1",
      page_start: 3,
      canonical_url: "https://example.com/study-a",
    },
    {
      id: "cite-2",
      title: "Study B",
      section: "Discussion",
      quote: "Quote B",
      source_id: "source-2",
      checksum: "sha256:b",
      license: "CC BY",
      chunk_id: "chunk-2",
      canonical_url: "https://example.com/study-b",
    },
  ],
  insight: {
    agent_name: "Literature synthesis",
    content: "Verified insight from stored citations.",
    evidence_state: "verified",
    online_research_used: true,
    referenced_source_ids: ["source-1", "source-2"],
    unresolved_source_ids: ["source-3"],
  },
};

describe("ResearchWorkbench", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    mockedGetWorkspaceData.mockReset();
    mockedGetWorkspaceData.mockResolvedValue(cloneWorkspaceData());
    jest.mocked(listProjects).mockReset();
    jest.mocked(listProjects).mockResolvedValue([
      {
        id: "demo-project",
        name: "V2 test workspace",
        description: "A governed test workspace.",
        active_runs: 1,
        source_count: 1,
        is_active: true,
      },
    ]);
    jest.mocked(activateProject).mockReset();
    jest.mocked(createProject).mockReset();
    jest.mocked(runStudio).mockReset();
    jest.mocked(listChatAgents).mockReset();
    jest.mocked(listChatAgents).mockResolvedValue([
      {
        name: "literature-agent",
        label: "Literature agent",
        description: "Grounded synthesis over stored evidence.",
        online: false,
      },
    ]);
    jest.mocked(openChatThread).mockReset();
    jest.mocked(openChatThread).mockResolvedValue({
      id: "thread-1",
      capability: "literature",
      agent_name: "literature-agent",
      created_at: "2026-07-30T00:00:00Z",
      updated_at: "2026-07-30T00:00:00Z",
      messages: [],
      attachments: [],
    });
    jest.mocked(sendChatMessage).mockReset();
    jest.mocked(getChatThread).mockReset();
    jest.mocked(uploadChatFile).mockReset();
    jest.mocked(decideApproval).mockReset();
    jest.mocked(testConnector).mockReset();
    jest.mocked(updateConnector).mockReset();
    jest.mocked(updateProject).mockReset();
    jest.mocked(updateSettings).mockReset();
    jest.mocked(uploadLibraryItem).mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
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

  it("creates a personal project and reloads the workspace in that project scope", async () => {
    const user = setupUser();
    const createdProject = {
      id: "project-0123456789abcdef0123456789abcdef",
      name: "Cancer outcomes review",
      description: "A private workspace for a bounded evidence review.",
      active_runs: 0,
      source_count: 0,
      is_active: true,
    };
    jest.mocked(listProjects)
      .mockReset()
      .mockResolvedValueOnce([
        {
          id: "demo-project",
          name: "V2 test workspace",
          description: "A governed test workspace.",
          active_runs: 1,
          source_count: 1,
          is_active: true,
        },
      ])
      .mockResolvedValueOnce([createdProject]);
    jest.mocked(createProject).mockResolvedValue(createdProject);

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    await user.type(screen.getByLabelText("Project name"), createdProject.name);
    await user.type(screen.getByLabelText("Description"), createdProject.description);
    await user.click(screen.getByRole("button", { name: "Save project" }));

    await waitFor(() =>
      expect(createProject).toHaveBeenCalledWith({
        name: createdProject.name,
        description: createdProject.description,
      }),
    );
    await waitFor(() =>
      expect(mockedGetWorkspaceData).toHaveBeenLastCalledWith(createdProject.id),
    );
    expect(window.location.search).toContain(`project=${createdProject.id}`);
  });

  it("has no automated accessibility violations on the overview", async () => {
    const { container } = render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    expect(await axe(container)).toHaveNoViolations();
  });

  it("opens the Literature Studio as an agent chat", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(
      screen.getByRole("button", { name: /literature review synthesis/i }),
    );

    expect(
      screen.getByRole("heading", { name: "Literature Studio" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("combobox", { name: "Agent" }),
    ).toHaveValue("literature-agent");
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();
    // No multi-step protocol form survives: the agent owns the workflow.
    expect(screen.queryByLabelText("Research question")).not.toBeInTheDocument();
  });

  it("renders real Library and Runs data", async () => {
    const user = setupUser();
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
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(
      screen.getByRole("button", { name: "Open project settings" }),
    );
    expect(
      screen.getByRole("heading", { name: "Project Settings" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));
    expect(
      screen.getByRole("heading", { name: "Research data connectors" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("PubMed").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Test connection" }),
    ).toBeInTheDocument();
  });

  it("shows a complete mobile navigation close control", async () => {
    const user = setupUser();
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

  it("[pw.mobile-nav:open] moves focus into the drawer's close control when opened", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const trigger = screen.getByRole("button", { name: "Open navigation" });
    trigger.focus();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    const railCloseButton = screen.getAllByRole("button", {
      name: "Close navigation",
    })[1];
    await waitFor(() => expect(railCloseButton).toHaveFocus());
  });

  it("[pw.mobile-nav:close-button] restores focus to the trigger when closed via the rail close button", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const trigger = screen.getByRole("button", { name: "Open navigation" });
    await user.click(trigger);
    const railCloseButton = screen.getAllByRole("button", {
      name: "Close navigation",
    })[1];
    await waitFor(() => expect(railCloseButton).toHaveFocus());

    await user.click(railCloseButton);
    await waitFor(() =>
      expect(
        screen.getByLabelText("Project navigation"),
      ).toHaveAttribute("data-open", "false"),
    );
    expect(trigger).toHaveFocus();
  });

  it("[pw.mobile-nav:close-scrim] restores focus to the trigger when closed via the scrim", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const trigger = screen.getByRole("button", { name: "Open navigation" });
    await user.click(trigger);
    const scrim = screen.getAllByRole("button", {
      name: "Close navigation",
    })[0];
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: "Close navigation" })[1],
      ).toHaveFocus(),
    );

    await user.click(scrim);
    await waitFor(() =>
      expect(
        screen.getByLabelText("Project navigation"),
      ).toHaveAttribute("data-open", "false"),
    );
    expect(trigger).toHaveFocus();
  });

  it("[pw.mobile-nav:close-escape] restores focus to the trigger when closed via Escape", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const trigger = screen.getByRole("button", { name: "Open navigation" });
    await user.click(trigger);
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: "Close navigation" })[1],
      ).toHaveFocus(),
    );

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(
        screen.getByLabelText("Project navigation"),
      ).toHaveAttribute("data-open", "false"),
    );
    expect(trigger).toHaveFocus();
  });

  it("[pw.mobile-nav:tab-order] tabs forward from the close control to project controls", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const railCloseButton = screen.getAllByRole("button", {
      name: "Close navigation",
    })[1];
    await waitFor(() => expect(railCloseButton).toHaveFocus());

    await user.tab();
    expect(
      screen.getByRole("combobox", { name: "Active project" }),
    ).toHaveFocus();
  });

  it("keeps Workflow Automation and Agents above Project Settings in the utility navigation", async () => {
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const studioNav = screen.getByRole("navigation", { name: "Research studios" });
    expect(within(studioNav).queryByRole("button", { name: "Workflow Automation" })).not.toBeInTheDocument();

    const utilityNav = screen.getByRole("navigation", { name: "Project utilities" });
    expect(within(utilityNav).getByRole("button", { name: "Workflow Automation" })).toBeInTheDocument();
    expect(within(utilityNav).getByRole("button", { name: "Agents" })).toBeInTheDocument();
    expect(within(utilityNav).getByRole("button", { name: "Project Settings" })).toBeInTheDocument();
  });

  it("[pw.mobile-nav:axe] has no automated accessibility violations while the drawer is open", async () => {
    const user = setupUser();
    const { container } = render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: "Close navigation" })[1],
      ).toHaveFocus(),
    );
    expect(await axe(container)).toHaveNoViolations();
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

  it("renders loading defaults, then shows empty states and auth-constrained matching controls", async () => {
    const user = setupUser();
    const deferred = createDeferred<WorkspaceData>();
    const emptyWorkspace = cloneWorkspaceData();
    emptyWorkspace.summary.library_items = 0;
    emptyWorkspace.summary.active_runs = 0;
    emptyWorkspace.summary.pending_approvals = 0;
    emptyWorkspace.summary.connector_ready = 0;
    emptyWorkspace.summary.connector_total = 0;
    emptyWorkspace.library = [];
    emptyWorkspace.runs = [];
    emptyWorkspace.approvals = [];
    emptyWorkspace.connectors = [];

    window.history.replaceState(null, "", "/?view=unsupported");
    mockedGetWorkspaceData.mockResolvedValue(emptyWorkspace);
    mockedGetWorkspaceData.mockReturnValueOnce(deferred.promise);

    render(<ResearchWorkbench />);

    expect(document.querySelector(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "false",
    );
    expect(
      screen.getAllByText("Project workspace").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "0 pending approvals" }),
    ).toBeInTheDocument();

    deferred.resolve(emptyWorkspace);
    await screen.findByText("V2 test workspace");
    expect(screen.getAllByText("Research command center").length).toBeGreaterThan(
      0,
    );

    await user.click(screen.getByRole("button", { name: "Overview" }));
    expect(window.location.search).toBe("");
    await user.click(screen.getByRole("button", { name: "Overview" }));
    expect(window.location.search).toBe("");

    await user.click(screen.getByRole("button", { name: /^Library 0$/i }));
    expect(screen.getByText("No sources match this view")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /runs & approvals/i }));
    expect(screen.getByText("No durable runs available")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Matching Explorer" }));
    expect(
      await screen.findByRole("combobox", { name: "Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Project Settings" }));
    expect(
      screen.getByRole("heading", { name: "Project Settings" }),
    ).toBeInTheDocument();
  });

  it("surfaces initial load failures while keeping the shell usable", async () => {
    const user = setupUser();
    mockedGetWorkspaceData
      .mockRejectedValueOnce("boot failure")
      .mockResolvedValueOnce(cloneWorkspaceData());

    render(<ResearchWorkbench />);

    expect(
      await screen.findByText(
        /Live workspace data is unavailable: Workspace data could not be loaded/i,
      ),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Open project settings" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Project Settings" }),
    ).toBeInTheDocument();
  });

  it("polls transitional state, handles refresh failures, refreshes on focus, and clears polling on unmount", async () => {
    jest.useFakeTimers();
    const transitionalWorkspace = cloneWorkspaceData();
    transitionalWorkspace.summary.pending_approvals = 0;
    transitionalWorkspace.library[0].status = "processing";
    transitionalWorkspace.runs[0].status = "running";
    transitionalWorkspace.runs[0].current_stage = "Collecting evidence";
    transitionalWorkspace.approvals = [];

    mockedGetWorkspaceData
      .mockResolvedValueOnce(transitionalWorkspace)
      .mockRejectedValueOnce(new Error("Refresh unavailable"))
      .mockResolvedValueOnce(cloneWorkspaceData());

    const { unmount } = render(<ResearchWorkbench />);

    await screen.findByText("V2 test workspace");

    act(() => {
      jest.advanceTimersByTime(3_000);
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByText(
        /Live workspace data is unavailable: Refresh unavailable/i,
      ),
    ).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(screen.queryByText(/Refresh unavailable/i)).not.toBeInTheDocument(),
    );

    mockedGetWorkspaceData.mockRejectedValueOnce("focus timeout");
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(4));
    expect(
      await screen.findByText(
        /Live workspace data is unavailable: Workspace data could not be loaded/i,
      ),
    ).toBeInTheDocument();

    unmount();
    act(() => {
      jest.advanceTimersByTime(3_000);
    });
    expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(4);
  });

  it("serializes transitional-state polling instead of starving forever when a response consistently takes longer than the poll interval", async () => {
    jest.useFakeTimers();
    const transitionalWorkspace = cloneWorkspaceData();
    transitionalWorkspace.library[0].status = "processing";
    transitionalWorkspace.runs[0].status = "running";

    const slowPollResponse = createDeferred<WorkspaceData>();
    mockedGetWorkspaceData
      .mockResolvedValueOnce(transitionalWorkspace) // initial mount load
      .mockReturnValueOnce(slowPollResponse.promise); // first poll, held open

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    act(() => {
      jest.advanceTimersByTime(3_000);
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2));

    // A fixed `setInterval` would have queued more polls here even though
    // the first poll's response has not landed yet -- with a consistently
    // slow backend, every one of those extra ticks would bump the
    // sequence guard again and doom every response (including this one)
    // to be discarded on arrival. Advancing well past several more
    // 3s ticks while the response is still held open must NOT issue any
    // further requests: at most one poll may ever be in flight at a time.
    act(() => {
      jest.advanceTimersByTime(30_000);
    });
    expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2);

    // Now let the slow response finally resolve, well after several poll
    // intervals have elapsed. Because only one request was ever in
    // flight, this is still the most-recently-issued request, so its
    // result must be applied -- proving the response is never starved.
    const settledWorkspace = cloneWorkspaceData();
    settledWorkspace.library[0].status = "processing";
    settledWorkspace.runs[0].status = "running";
    settledWorkspace.runs[0].current_stage = "Slow poll finally landed";
    mockedGetWorkspaceData.mockResolvedValueOnce(cloneWorkspaceData());
    await act(async () => {
      slowPollResponse.resolve(settledWorkspace);
      await slowPollResponse.promise;
    });

    fireEvent.click(screen.getByRole("button", { name: /runs & approvals/i }));
    expect(screen.getByText("Slow poll finally landed")).toBeInTheDocument();
  });

  it("does not reschedule another poll when the component unmounts while a poll's refresh() call is still in flight", async () => {
    jest.useFakeTimers();
    const transitionalWorkspace = cloneWorkspaceData();
    transitionalWorkspace.library[0].status = "processing";
    transitionalWorkspace.runs[0].status = "running";

    const inFlightPoll = createDeferred<WorkspaceData>();
    mockedGetWorkspaceData
      .mockResolvedValueOnce(transitionalWorkspace) // initial mount load
      .mockReturnValueOnce(inFlightPoll.promise); // first poll, held open

    const { unmount } = render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    act(() => {
      jest.advanceTimersByTime(3_000);
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2));

    // Unmount while the poll's refresh() promise is still pending: the
    // effect's cleanup sets `cancelled = true` before the promise ever
    // settles.
    unmount();

    // Resolve the in-flight poll only now, after unmount. If the
    // `cancelled` guard were missing, this `.finally()` would call
    // `scheduleNext()` again and schedule a further poll even though the
    // component is gone.
    await act(async () => {
      inFlightPoll.resolve(cloneWorkspaceData());
      await inFlightPoll.promise;
    });

    act(() => {
      jest.advanceTimersByTime(30_000);
    });
    // No further poll was ever scheduled, so the call count stays at 2
    // (initial load + the one in-flight poll) forever.
    expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2);
  });

  it("discards a stale refresh response that resolves after a newer refresh was issued (workspace-ready / approval-notification race)", async () => {
    const user = setupUser();
    const deferredStaleNavigateRefresh = createDeferred<WorkspaceData>();
    const freshFocusWorkspace = cloneWorkspaceData();
    freshFocusWorkspace.summary.library_items = 42;
    const staleNavigateWorkspace = cloneWorkspaceData();
    staleNavigateWorkspace.summary.library_items = 7;

    mockedGetWorkspaceData
      .mockResolvedValueOnce(cloneWorkspaceData()) // initial mount load
      .mockReturnValueOnce(deferredStaleNavigateRefresh.promise) // navigate("library") refresh, held open
      .mockResolvedValueOnce(freshFocusWorkspace); // focus refresh, resolves before the older one

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    // Navigating to Library issues a refresh (request #2) that we hold open
    // so it resolves after a later, newer refresh -- reproducing the
    // out-of-order network response that previously let a stale result
    // silently overwrite fresher data (including the pending-approvals count
    // the notification bell reads).
    await user.click(screen.getByRole("button", { name: /^Library \d+$/i }));
    await screen.findByRole("heading", { name: "Library" });

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(3));
    expect(
      await screen.findByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();

    // Now let the older, navigation-triggered refresh resolve. Its result
    // must be discarded because a newer refresh has since completed --
    // proving requests are applied in issue order, not resolution order.
    await act(async () => {
      deferredStaleNavigateRefresh.resolve(staleNavigateWorkspace);
      await deferredStaleNavigateRefresh.promise;
    });
    expect(
      screen.getByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Library 7$/i }),
    ).not.toBeInTheDocument();
  });

  it("discards a stale refresh rejection that arrives after a newer refresh already succeeded", async () => {
    const deferredStaleNavigateRefresh = createDeferred<WorkspaceData>();
    const freshFocusWorkspace = cloneWorkspaceData();
    freshFocusWorkspace.summary.library_items = 42;

    mockedGetWorkspaceData
      .mockResolvedValueOnce(cloneWorkspaceData()) // initial mount load
      .mockReturnValueOnce(deferredStaleNavigateRefresh.promise) // navigate("runs") refresh, held open, will reject
      .mockResolvedValueOnce(freshFocusWorkspace); // focus refresh, resolves before the older one

    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: /^Runs & approvals/i }));
    await screen.findByRole("heading", { name: "Runs & Approvals" });

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(3));
    expect(
      await screen.findByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();

    // The older, navigation-triggered refresh now rejects. Because a newer
    // refresh has already completed successfully, this stale rejection must
    // be discarded rather than overwriting the fresh data with a phantom
    // "unavailable" error banner (covers `refresh`'s own catch-branch
    // sequence guard, not just its success-branch guard).
    await act(async () => {
      deferredStaleNavigateRefresh.reject(new Error("stale runs refresh failed"));
      await deferredStaleNavigateRefresh.promise.catch(() => undefined);
    });
    expect(
      screen.queryByText(/Live workspace data is unavailable/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();
  });

  it("discards a stale initial-mount response that resolves after a later refresh already succeeded", async () => {
    const deferredMountLoad = createDeferred<WorkspaceData>();
    const freshFocusWorkspace = cloneWorkspaceData();
    freshFocusWorkspace.summary.library_items = 42;
    const staleMountWorkspace = cloneWorkspaceData();
    staleMountWorkspace.summary.library_items = 7;

    mockedGetWorkspaceData
      .mockReturnValueOnce(deferredMountLoad.promise) // initial mount load, held open
      .mockResolvedValueOnce(freshFocusWorkspace); // focus refresh, resolves before the mount load

    render(<ResearchWorkbench />);
    expect(
      screen.getByRole("main").closest(".workbench-shell"),
    ).toHaveAttribute("data-workspace-ready", "false");

    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();

    // Now let the mount-triggered load resolve. It must be discarded because
    // the focus-triggered refresh already applied newer data -- covers the
    // request-sequence guard inside the mount effect's own fetch, not only
    // the shared `refresh` callback's guard.
    await act(async () => {
      deferredMountLoad.resolve(staleMountWorkspace);
      await deferredMountLoad.promise;
    });
    expect(
      screen.getByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Library 7$/i }),
    ).not.toBeInTheDocument();
  });

  it("discards a stale initial-mount rejection that arrives after a later refresh already succeeded", async () => {
    const deferredMountLoad = createDeferred<WorkspaceData>();
    const freshFocusWorkspace = cloneWorkspaceData();
    freshFocusWorkspace.summary.library_items = 42;

    mockedGetWorkspaceData
      .mockReturnValueOnce(deferredMountLoad.promise) // initial mount load, held open, will reject
      .mockResolvedValueOnce(freshFocusWorkspace); // focus refresh, resolves before the mount load

    render(<ResearchWorkbench />);

    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockedGetWorkspaceData).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();

    // The mount-triggered load now rejects. It must be discarded because a
    // later refresh already succeeded, so no phantom "unavailable" error
    // banner should appear over the already-fresh data.
    await act(async () => {
      deferredMountLoad.reject(new Error("stale mount load failed"));
      await deferredMountLoad.promise.catch(() => undefined);
    });
    expect(
      screen.queryByText(/Live workspace data is unavailable/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Library 42$/i }),
    ).toBeInTheDocument();
  });

  it("supports search and navigation controls through buttons, scrims, and keyboard shortcuts", async () => {
    const user = setupUser();
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    expect(screen.getByLabelText("Project navigation")).toHaveAttribute(
      "data-open",
      "true",
    );
    await user.click(
      screen.getAllByRole("button", { name: "Close navigation" })[0],
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Project navigation")).toHaveAttribute(
        "data-open",
        "false",
      ),
    );

    await user.click(screen.getByRole("button", { name: /Search workspace/i }));
    const searchDialog = screen.getByRole("dialog", { name: "Search workspace" });
    expect(
      within(searchDialog).getByRole("button", { name: /Evidence Library/i }),
    ).toBeInTheDocument();
    expect(
      within(searchDialog).getByRole("button", {
        name: /Grant application studio/i,
      }),
    ).toBeInTheDocument();
    expect(
      within(searchDialog).getByRole("button", {
        name: /PI and resource matching/i,
      }),
    ).toBeInTheDocument();
    expect(
      within(searchDialog).getByRole("button", {
        name: /Institution-grounded Q&A/i,
      }),
    ).toBeInTheDocument();
    expect(
      within(searchDialog).getByRole("button", {
        name: /Research workflow orchestration/i,
      }),
    ).toBeInTheDocument();

    const searchInput = within(searchDialog).getByPlaceholderText(
      "Search studios, Library, runs, or settings",
    );
    await user.type(searchInput, "grant");
    expect(
      within(searchDialog).getByRole("button", {
        name: /Grant application studio/i,
      }),
    ).toBeInTheDocument();
    await user.click(
      within(searchDialog).getByRole("button", { name: "Close search" }),
    );
    expect(
      screen.queryByRole("dialog", { name: "Search workspace" }),
    ).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(
      screen.getByRole("dialog", { name: "Search workspace" }),
    ).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Search workspace" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("suppresses global shortcuts and inerts the shell while a blocking modal is open", async () => {
    const user = setupUser();
    const { container } = render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const shell = container.querySelector(".workbench-shell");
    expect(shell).not.toBeNull();
    expect(shell).not.toHaveAttribute("inert");

    // Open the navigation drawer first so there is visible shell state that
    // a stray Escape would wrongly collapse behind the modal. This used to be
    // the evidence inspector; the drawer is the same kind of proof -- a shell
    // surface with observable open state that Escape owns when no modal is up.
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const projectNavigation = screen.getByLabelText("Project navigation");
    expect(projectNavigation).toHaveAttribute("data-open", "true");

    let release!: () => void;
    act(() => {
      release = openBlockingModal();
    });

    // The whole shell -- rail, main, palette -- is inert, so nothing behind
    // the modal is reachable by keyboard or assistive tech.
    expect(shell).toHaveAttribute("inert");

    // Ctrl+K must not open the command palette on top of the modal: that
    // second dialog would live outside the first one's focus trap and
    // outside this inert region.
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(
      screen.queryByRole("dialog", { name: "Search workspace" }),
    ).not.toBeInTheDocument();

    // The macOS chord is the same shortcut and must be suppressed too.
    fireEvent.keyDown(window, { key: "K", metaKey: true });
    expect(
      screen.queryByRole("dialog", { name: "Search workspace" }),
    ).not.toBeInTheDocument();

    // Escape belongs to the modal while it is open; it must not reach through
    // and collapse shell surfaces the user cannot currently see.
    fireEvent.keyDown(window, { key: "Escape" });
    expect(projectNavigation).toHaveAttribute("data-open", "true");

    act(() => {
      release();
    });

    // Suppression is scoped to the modal's lifetime, not latched permanently.
    expect(shell).not.toHaveAttribute("inert");
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(
      screen.getByRole("dialog", { name: "Search workspace" }),
    ).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Search workspace" }),
      ).not.toBeInTheDocument(),
    );
    expect(projectNavigation).toHaveAttribute("data-open", "false");
  });

  it("uses the original initial-load error message when the loader rejects with an Error", async () => {
    mockedGetWorkspaceData.mockRejectedValueOnce(new Error("Backend offline"));

    render(<ResearchWorkbench />);

    expect(
      await screen.findByText(
        /Live workspace data is unavailable: Backend offline/i,
      ),
    ).toBeInTheDocument();
  });

  it("ignores late initial load settlements after the component unmounts", async () => {
    const resolveDeferred = createDeferred<WorkspaceData>();
    mockedGetWorkspaceData.mockReturnValueOnce(resolveDeferred.promise);
    const resolvingRender = render(<ResearchWorkbench />);
    resolvingRender.unmount();
    resolveDeferred.resolve(cloneWorkspaceData());
    await act(async () => {
      await resolveDeferred.promise;
    });

    const rejectDeferred = createDeferred<WorkspaceData>();
    mockedGetWorkspaceData.mockReset();
    mockedGetWorkspaceData.mockReturnValueOnce(rejectDeferred.promise);
    const rejectingRender = render(<ResearchWorkbench />);
    rejectingRender.unmount();
    rejectDeferred.reject(new Error("Late failure"));
    await act(async () => {
      await rejectDeferred.promise.catch(() => undefined);
    });

    expect(screen.queryByText("V2 test workspace")).not.toBeInTheDocument();
    expect(screen.queryByText(/Late failure/i)).not.toBeInTheDocument();
  });

  it("falls back to the generic title when a capability card loses its short title", async () => {
    const literatureCard = CAPABILITY_CARDS.find(
      (capability) => capability.id === "literature",
    );
    expect(literatureCard).toBeDefined();
    const originalShortTitle = literatureCard?.shortTitle;

    try {
      if (literatureCard) {
        literatureCard.shortTitle = undefined as unknown as string;
      }

      window.history.replaceState(null, "", "/?view=literature");
      render(<ResearchWorkbench />);

      await screen.findByText("V2 test workspace");
      expect(screen.getAllByText("Research Assistant").length).toBeGreaterThan(0);
    } finally {
      if (literatureCard && originalShortTitle) {
        literatureCard.shortTitle = originalShortTitle;
      }
    }
  });

  it("defaults studio run options when a studio submits without explicit options", async () => {
    const user = setupUser();
    const sharedReact = jest.requireActual<typeof import("react")>("react");
    jest.doMock("react", () => sharedReact);
    jest.doMock("@/components/studio-components", () => ({
      StudioForCapability: ({
        onRun,
      }: {
        onRun: (capability: "literature", objective: string) => Promise<void>;
      }) =>
        sharedReact.createElement(
          "button",
          {
            type: "button",
            onClick: () => void onRun("literature", "Implicit options"),
          },
          "Run without options",
        ),
    }));

    let IsolatedWorkbench: ComponentType | null = null;
    let isolatedApi: typeof import("@/lib/api");
    jest.isolateModules(() => {
      isolatedApi = jest.requireMock<typeof import("@/lib/api")>("@/lib/api");
      IsolatedWorkbench = jest.requireActual<
        typeof import("@/components/research-workbench")
      >("@/components/research-workbench").ResearchWorkbench;
    });
    jest.dontMock("@/components/studio-components");
    jest.dontMock("react");

    jest.mocked(isolatedApi!.getWorkspaceData).mockResolvedValue(cloneWorkspaceData());
    jest.mocked(isolatedApi!.runStudio).mockResolvedValue(literatureResult);

    window.history.replaceState(null, "", "/?view=literature");
    expect(IsolatedWorkbench).not.toBeNull();
    if (!IsolatedWorkbench) {
      throw new Error("Isolated workbench failed to load");
    }
    const WorkbenchUnderTest = IsolatedWorkbench;
    render(sharedReact.createElement(WorkbenchUnderTest));
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Run without options" }));

    await waitFor(() =>
      expect(isolatedApi!.runStudio).toHaveBeenCalledWith(
        "literature",
        "Implicit options",
        {},
        "demo-project",
      ),
    );
  });

  it("keeps the composer usable and the transcript empty until a turn is sent", async () => {
    const user = setupUser();

    window.history.replaceState(null, "", "/?view=literature");
    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());

    expect(screen.getByRole("log")).toHaveTextContent(
      /Describe what you need in your own words/i,
    );
    // Nothing has been sent, so no run artifact or boundary card exists yet.
    expect(screen.queryByText("Resolved IDs")).not.toBeInTheDocument();

    // Suggestions prefill the composer rather than launching a workflow.
    await user.click(screen.getAllByRole("button", { name: /^Compare / })[0]);
    expect((composer as HTMLTextAreaElement).value.length).toBeGreaterThan(0);
    expect(sendChatMessage).not.toHaveBeenCalled();
  });

  it("navigates through search and rails, opens library dialogs, and holds a literature conversation", async () => {
    const user = setupUser();

    window.history.replaceState(null, "", "/?view=grant");
    mockedGetWorkspaceData.mockResolvedValue(cloneWorkspaceData());

    render(<ResearchWorkbench />);
    await screen.findByRole("heading", { name: "Grant Studio" });

    await user.click(screen.getByRole("button", { name: /Search workspace/i }));
    await user.click(
      screen.getByRole("button", { name: /Evidence Library/i }),
    );
    await screen.findByRole("heading", { name: "Library" });

    await user.click(
      screen.getByRole("button", { name: /Evidence workflow study/i }),
    );
    const detailDialog = screen.getByRole("dialog", {
      name: "Evidence workflow study",
    });
    expect(within(detailDialog).getByText("Verified test paper.")).toBeInTheDocument();
    await user.click(
      within(detailDialog).getByRole("button", { name: "Close source detail" }),
    );
    expect(
      screen.queryByRole("dialog", { name: "Evidence workflow study" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Overview" }));
    expect(window.location.search).toBe("");

    await user.click(screen.getByRole("button", { name: "Literature Studio" }));
    await screen.findByRole("heading", { name: "Literature Studio" });

    const answered: ChatThread = {
      id: "thread-1",
      capability: "literature",
      agent_name: "literature-agent",
      created_at: "2026-07-30T00:00:00Z",
      updated_at: "2026-07-30T00:01:00Z",
      attachments: [],
      messages: [
        {
          id: "m1",
          role: "user",
          content: "How can auditable synthesis stay deterministic?",
          created_at: "2026-07-30T00:00:30Z",
          agent_name: null,
          attachments: [],
        },
        {
          id: "m2",
          role: "assistant",
          content: "Verified insight from stored citations.",
          created_at: "2026-07-30T00:01:00Z",
          agent_name: "literature-agent",
          attachments: [],
        },
      ],
    };
    jest.mocked(sendChatMessage).mockResolvedValue(answered.messages[1]);
    jest.mocked(getChatThread).mockResolvedValue(answered);

    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "How can auditable synthesis stay deterministic?");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(sendChatMessage).toHaveBeenCalledWith(
        "thread-1",
        "How can auditable synthesis stay deterministic?",
        "demo-project",
      ),
    );

    expect(
      await screen.findByText("Verified insight from stored citations."),
    ).toBeInTheDocument();
    const transcript = within(screen.getByRole("log"));
    expect(
      transcript.getByText("How can auditable synthesis stay deterministic?"),
    ).toBeInTheDocument();
    expect(transcript.getAllByText("literature-agent").length).toBeGreaterThan(0);
  });

  it("handles studio failures, renders no-citation evidence results, and routes orchestration inspections to Runs", async () => {
    const user = setupUser();
    const orchestrationWorkspace = cloneWorkspaceData();
    orchestrationWorkspace.runs.unshift({
      id: "run-orc-1",
      durable_instance_id: "research-run-orc-1",
      project_id: "demo-project",
      capability: "orchestration",
      title: "Evidence review graph",
      status: "waiting_for_approval",
      progress: 60,
      current_stage: "Human review",
      owner: "Dr. Maya Chen",
      started_at: "2026-07-16T12:00:00Z",
      completed_at: null,
      artifact_count: 1,
      estimated_cost_usd: 0,
      scheduler_managed: false,
      scheduling_state: "not_managed",
      orchestration_input: null,
      stages: [],
    });
    orchestrationWorkspace.approvals.unshift({
      id: "approval-orc-1",
      run_id: "run-orc-1",
      title: "Release evidence review graph",
      state: "pending",
      risk: "High",
      gated_action: "Activate graph v1.0.",
      destination: "Application approval boundary",
      requested_by: "orchestration-agent",
      requested_at: "2026-07-16T12:00:00Z",
      evidence_summary: "Dry run passed.",
      idempotency_key: "run-orc-1-v1",
      approver_id: null,
      approver_name: null,
      decided_at: null,
      rationale: null,
      event_delivery: "not_requested",
      decision_event_id: null,
    });
    orchestrationWorkspace.summary.pending_approvals = 2;

    mockedGetWorkspaceData.mockResolvedValue(orchestrationWorkspace);
    jest
      .mocked(sendChatMessage)
      .mockRejectedValueOnce(new Error("Research orchestration unavailable"))
      .mockRejectedValueOnce("service timeout");

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");

    await user.click(screen.getByRole("button", { name: "Literature Studio" }));
    const composer = await screen.findByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());

    await user.type(composer, "Summarise the evidence");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Research orchestration unavailable",
    );
    // The failed turn is rolled back and the draft restored, so the user can retry.
    await waitFor(() => expect(composer).toHaveValue("Summarise the evidence"));

    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Request failed.",
    );

    await user.click(screen.getByRole("button", { name: "2 pending approvals" }));
    await screen.findByRole("heading", { name: "Runs & Approvals" });

    await user.click(screen.getByRole("button", { name: "Workflow Automation" }));
    await screen.findByRole("heading", { name: "Workflow Automation" });
    await user.click(screen.getByRole("button", { name: "Inspect" }));

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Runs & Approvals" }),
      ).toBeInTheDocument(),
    );
    expect(screen.getAllByText("Evidence review graph").length).toBeGreaterThan(0);
    expect(screen.getByText("Exact gated action")).toBeInTheDocument();
  });

  it("discards a stale Workflow Automation dry-run response that resolves after a newer one already applied its result", async () => {
    const user = setupUser();
    const staleResult: AutomationStudioResult = {
      run: baseRun({
        capability: "orchestration",
        id: "run-orc-stale",
        durable_instance_id: "research-run-orc-stale",
        title: "Stale dry run",
      }),
      template_id: "evidence-review-v2",
      trigger: "Manual",
      steps: [],
      validation_errors: ["Stale dry run failure"],
      dry_run_status: "failed",
      graph_version: "1.0",
      graph_hash: "stalehash0000001",
      citations: [],
    };
    const freshResult: AutomationStudioResult = {
      run: baseRun({
        capability: "orchestration",
        id: "run-orc-fresh",
        durable_instance_id: "research-run-orc-fresh",
        title: "Fresh dry run",
      }),
      template_id: "evidence-review-v2",
      trigger: "Manual",
      steps: [],
      validation_errors: [],
      dry_run_status: "passed",
      graph_version: "1.0",
      graph_hash: "freshhash0000001",
      citations: [],
    };
    const deferredStale = createDeferred<AutomationStudioResult>();
    const deferredFresh = createDeferred<AutomationStudioResult>();
    jest
      .mocked(runStudio)
      .mockReturnValueOnce(deferredStale.promise)
      .mockReturnValueOnce(deferredFresh.promise);

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");
    await user.click(screen.getByRole("button", { name: "Workflow Automation" }));
    await screen.findByRole("heading", { name: "Workflow Automation" });

    // Two overlapping dry runs are issued before either resolves -- e.g. a
    // fast double-submit racing ahead of the button's disabled re-render,
    // or (as reproduced directly here) two dispatched submissions of the
    // same form. Network responses are not guaranteed to resolve in issue
    // order, so the *second* (fresher) request resolving before the first
    // (now-stale) one must not let that stale response overwrite it later.
    const form = document.querySelector(".automation-studio form");
    if (!form) throw new Error("Workflow Automation form not found");
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(runStudio).toHaveBeenCalledTimes(2);

    await act(async () => {
      deferredFresh.resolve(freshResult);
      await deferredFresh.promise;
    });
    expect(await screen.findByText("Dry run passed")).toBeInTheDocument();

    // The older, first-issued request now resolves. Its result must be
    // discarded because a newer request has since completed.
    await act(async () => {
      deferredStale.resolve(staleResult);
      await deferredStale.promise;
    });
    expect(screen.getByText("Dry run passed")).toBeInTheDocument();
    expect(screen.queryByText("Dry run failed")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Stale dry run failure"),
    ).not.toBeInTheDocument();
  });

  it("discards a stale Workflow Automation dry-run rejection that arrives after a newer request already applied its result", async () => {
    const user = setupUser();
    const freshResult: AutomationStudioResult = {
      run: baseRun({
        capability: "orchestration",
        id: "run-orc-fresh-2",
        durable_instance_id: "research-run-orc-fresh-2",
        title: "Fresh dry run",
      }),
      template_id: "evidence-review-v2",
      trigger: "Manual",
      steps: [],
      validation_errors: [],
      dry_run_status: "passed",
      graph_version: "1.0",
      graph_hash: "freshhash0000002",
      citations: [],
    };
    const deferredStale = createDeferred<AutomationStudioResult>();
    const deferredFresh = createDeferred<AutomationStudioResult>();
    jest
      .mocked(runStudio)
      .mockReturnValueOnce(deferredStale.promise)
      .mockReturnValueOnce(deferredFresh.promise);

    render(<ResearchWorkbench />);
    await screen.findByText("V2 test workspace");
    await user.click(screen.getByRole("button", { name: "Workflow Automation" }));
    await screen.findByRole("heading", { name: "Workflow Automation" });

    const form = document.querySelector(".automation-studio form");
    if (!form) throw new Error("Workflow Automation form not found");
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(runStudio).toHaveBeenCalledTimes(2);

    await act(async () => {
      deferredFresh.resolve(freshResult);
      await deferredFresh.promise;
    });
    expect(await screen.findByText("Dry run passed")).toBeInTheDocument();

    // The older, first-issued request now rejects. Its failure must be
    // discarded too -- a newer request already succeeded and that result
    // must not be clobbered by a stale error message.
    await act(async () => {
      deferredStale.reject(new Error("Stale transport failure"));
      await deferredStale.promise.catch(() => undefined);
    });
    expect(screen.getByText("Dry run passed")).toBeInTheDocument();
    expect(
      screen.queryByText("Stale transport failure"),
    ).not.toBeInTheDocument();
  });
});
