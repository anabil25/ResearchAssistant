import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import {
  CAPABILITY_CARDS,
  LibraryView,
  Overview,
  RunsView,
  SettingsView,
} from "@/components/workspace-views";
import {
  decideApproval,
  testConnector,
  updateConnector,
  updateSettings,
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";

/**
 * `userEvent.setup` with the artificial inter-event delay removed. See the
 * identical helper in `studio-components.test.tsx` for the full rationale:
 * userEvent v14's default `delay: 0` awaits a real `setTimeout(..., 0)`
 * between every dispatched event, and the interaction-heavy tests in this
 * file accumulate enough of those hops to sit at ~60-65% of Jest's 5s default
 * budget under the full `--runInBand` suite. Removing the waiting -- not the
 * events, which are all still dispatched in the same order -- restores the
 * headroom. Safe here because `workspace-views.tsx` owns no timers and no
 * assertion in this file depends on time passing between two events.
 */
function setupUser(
  options: Parameters<typeof userEvent.setup>[0] = {},
): ReturnType<typeof userEvent.setup> {
  return userEvent.setup({ delay: null, ...options });
}

jest.mock("@/lib/api", () => ({
  decideApproval: jest.fn(),
  testConnector: jest.fn(),
  updateConnector: jest.fn(),
  updateSettings: jest.fn(),
  uploadLibraryItem: jest.fn(),
}));

type LibraryItem = WorkspaceData["library"][number];
type RunSummary = WorkspaceData["runs"][number];
type ApprovalRecord = WorkspaceData["approvals"][number];
type ConnectorSetting = WorkspaceData["connectors"][number];
type AgentSetting = WorkspaceData["agents"][number];

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function buildLibraryItem(
  overrides: Partial<LibraryItem> = {},
): LibraryItem {
  return {
    id: "library-item-1",
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
    tags: ["evidence", "review"],
    size_bytes: 2_400_000,
    content_type: "application/pdf",
    ...overrides,
  };
}

function buildRunSummary(
  overrides: Partial<RunSummary> = {},
): RunSummary {
  return {
    id: "run-1",
    durable_instance_id: "research-run-1",
    project_id: "demo-project",
    capability: "literature",
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
    ...overrides,
  };
}

function buildApprovalRecord(
  overrides: Partial<ApprovalRecord> = {},
): ApprovalRecord {
  return {
    id: "approval-1",
    run_id: "run-1",
    title: "Release evidence review graph",
    state: "pending",
    risk: "High",
    gated_action: "Activate graph v1.0.",
    destination: "Application approval boundary",
    requested_by: "orchestration-agent",
    requested_at: "2026-07-16T12:00:00Z",
    evidence_summary: "Dry run passed.",
    idempotency_key: "run-1-v1",
    approver_id: null,
    approver_name: null,
    decided_at: null,
    rationale: null,
    event_delivery: "not_requested",
    decision_event_id: null,
    ...overrides,
  };
}

function buildConnector(
  overrides: Partial<ConnectorSetting> = {},
): ConnectorSetting {
  return {
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
    ...overrides,
  };
}

function buildAgent(
  overrides: Partial<AgentSetting> = {},
): AgentSetting {
  return {
    id: "literature",
    name: "Literature synthesis",
    model_tier: "Primary",
    status: "Active",
    web_access: "Opt-in public only",
    workflow_steps: ["Protocol", "Search", "Screen", "Audit"],
    deployment: "Foundry Hosted Agent",
    ...overrides,
  };
}

function buildWorkspaceData(
  overrides: Partial<WorkspaceData> = {},
): WorkspaceData {
  return {
    summary: {
      project: {
        project_id: "demo-project",
        name: "Test workspace",
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
      active_runs: 2,
      pending_approvals: 1,
      connector_ready: 1,
      connector_total: 1,
      last_activity_at: "2026-07-16T12:00:00Z",
      persistence: "in-memory demo",
    },
    library: [buildLibraryItem()],
    runs: [buildRunSummary()],
    approvals: [buildApprovalRecord()],
    connectors: [buildConnector()],
    settings: {
      project_id: "demo-project",
      name: "Test workspace",
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
    agents: [buildAgent()],
    workflows: [],
    ...overrides,
  };
}

describe("Overview", () => {
  it("renders live metrics, capability navigation, and recent runs accessibly", async () => {
    const user = setupUser();
    const onNavigate = jest.fn();
    const data = buildWorkspaceData({
      runs: [
        buildRunSummary({
          id: "run-lit",
          capability: "literature",
          title: "Plan literature review",
          current_stage: "screening",
          progress: 15,
        }),
        buildRunSummary({
          id: "run-grant",
          capability: "grant",
          title: "Grant drafting",
          status: "running",
          current_stage: "drafting",
          progress: 45,
        }),
        buildRunSummary({
          id: "run-dataset",
          capability: "dataset",
          title: "Dataset profiling",
          status: "completed",
          current_stage: "profiled",
          progress: 100,
          completed_at: "2026-07-16T13:00:00Z",
        }),
        buildRunSummary({
          id: "run-qa",
          capability: "institutional_qa",
          title: "Policy answer review",
          status: "completed",
          current_stage: "approved",
          progress: 100,
          completed_at: "2026-07-16T14:00:00Z",
        }),
        buildRunSummary({
          id: "run-hidden",
          capability: "matching",
          title: "Hidden fifth run",
          progress: 10,
        }),
      ],
    });

    const { container } = render(
      <Overview
        data={data}
        capabilities={CAPABILITY_CARDS}
        onNavigate={onNavigate}
      />,
    );

    const metrics = within(screen.getByLabelText("Workspace metrics"));
    expect(metrics.getByText("Governed library items")).toBeInTheDocument();
    expect(metrics.getByText("Governed library items").previousElementSibling).toHaveTextContent(
      "1",
    );
    expect(metrics.getByText("Active durable runs").previousElementSibling).toHaveTextContent(
      "2",
    );
    expect(screen.getByText("Plan literature review")).toBeInTheDocument();
    expect(screen.getByText("Grant drafting")).toBeInTheDocument();
    expect(screen.getByText("Dataset profiling")).toBeInTheDocument();
    expect(screen.getByText("Policy answer review")).toBeInTheDocument();
    expect(screen.queryByText("Hidden fifth run")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /literature review synthesis/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /research workflow orchestration/i }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Start a literature review" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Explore evidence library" }),
    );
    await user.click(screen.getByRole("button", { name: /view all runs/i }));
    await user.click(
      screen.getByRole("button", { name: /research workflow orchestration/i }),
    );
    await user.click(screen.getByRole("button", { name: /grant drafting/i }));

    expect(onNavigate.mock.calls).toEqual([
      ["literature"],
      ["library"],
      ["runs"],
      ["orchestration"],
      ["runs"],
    ]);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("shows fallback metrics and a loading state when workspace data is unavailable", () => {
    render(
      <Overview
        data={null}
        capabilities={CAPABILITY_CARDS}
        onNavigate={jest.fn()}
      />,
    );

    expect(screen.getByText("9")).toBeInTheDocument();
    expect(screen.getByText("12/12")).toBeInTheDocument();
    expect(screen.getByText("Loading durable runs…")).toBeInTheDocument();
  });
});

describe("LibraryView", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("filters sources, opens detail dialogs, and handles empty library results accessibly", async () => {
    const user = setupUser();
    const data = buildWorkspaceData({
      library: [
        buildLibraryItem(),
        buildLibraryItem({
          id: "dataset-1",
          title: "Sequencing dataset",
          kind: "Dataset",
          source: "Zenodo",
          status: "processing",
          access: "internal",
          connector: "DataCite",
          provider: "Zenodo",
          description: "Structured dataset evidence.",
          tags: ["omics"],
        }),
        buildLibraryItem({
          id: "policy-1",
          title: "Data governance policy",
          kind: "Policy",
          source: "Internal policy",
          status: "needs_review",
          access: "restricted",
          description: "Policy evidence baseline.",
          tags: ["policy"],
        }),
        buildLibraryItem({
          id: "template-1",
          title: "Grant template",
          kind: "Template",
          source: "Workspace upload",
          status: "blocked",
          publication_year: undefined,
          size_bytes: undefined,
          content_type: undefined,
          tags: undefined,
          description: "Reusable authoring scaffold.",
        }),
      ],
    });

    const { container } = render(
      <LibraryView data={data} onRefresh={jest.fn()} />,
    );

    expect(screen.getByText("Evidence workflow study")).toBeInTheDocument();
    expect(screen.getByText("Sequencing dataset")).toBeInTheDocument();
    expect(screen.getByText("Data governance policy")).toBeInTheDocument();
    expect(screen.getByText("Grant template")).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole("button", { name: "Policy" }));
    expect(screen.getByText("Data governance policy")).toBeInTheDocument();
    expect(
      screen.queryByText("Evidence workflow study"),
    ).not.toBeInTheDocument();

    await user.clear(screen.getByPlaceholderText("Search title, source, or tag"));
    await user.type(
      screen.getByPlaceholderText("Search title, source, or tag"),
      "workflow",
    );
    await user.click(screen.getByRole("button", { name: "All" }));
    await user.click(screen.getByRole("button", { name: /evidence workflow study/i }));

    const detailDialog = screen.getByRole("dialog", {
      name: "Evidence workflow study",
    });
    expect(within(detailDialog).getByText("Publication year")).toBeInTheDocument();
    expect(within(detailDialog).getByText("2.40 MB")).toBeInTheDocument();
    expect(within(detailDialog).getByText("application/pdf")).toBeInTheDocument();
    expect(within(detailDialog).getByText("review")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close source detail" }));
    expect(
      screen.queryByRole("dialog", { name: "Evidence workflow study" }),
    ).not.toBeInTheDocument();

    await user.clear(screen.getByPlaceholderText("Search title, source, or tag"));
    await user.type(
      screen.getByPlaceholderText("Search title, source, or tag"),
      "template",
    );
    await user.click(screen.getByRole("button", { name: /grant template/i }));
    const templateDialog = screen.getByRole("dialog", { name: "Grant template" });
    expect(
      within(templateDialog).queryByText("Publication year"),
    ).not.toBeInTheDocument();
    expect(within(templateDialog).queryByText("Content type")).not.toBeInTheDocument();
    expect(templateDialog.querySelector(".tag-list")).toBeNull();
    await user.click(within(templateDialog).getByRole("button", { name: "Close" }));
    expect(
      screen.queryByRole("dialog", { name: "Grant template" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Ingest source" }));
    expect(
      screen.getByRole("dialog", { name: "Add source to Library" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close ingest dialog" }));

    await user.click(screen.getByRole("button", { name: "Ingest source" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("dialog", { name: "Add source to Library" }),
    ).not.toBeInTheDocument();

    await user.clear(screen.getByPlaceholderText("Search title, source, or tag"));
    await user.type(
      screen.getByPlaceholderText("Search title, source, or tag"),
      "missing-source",
    );
    expect(screen.getByText("No sources match this view")).toBeInTheDocument();
  });

  it("submits ingestion, shows a queued state, and refreshes on success", async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const deferred = createDeferred<{
      item: LibraryItem;
      run: RunSummary;
    }>();
    jest.mocked(uploadLibraryItem).mockReturnValue(deferred.promise);

    render(<LibraryView data={buildWorkspaceData()} onRefresh={onRefresh} />);

    await user.click(screen.getByRole("button", { name: "Ingest source" }));
    await user.type(screen.getByLabelText("Title"), "Protocol package");
    await user.upload(
      screen.getByLabelText("Source file"),
      new File(["protocol"], "protocol.pdf", { type: "application/pdf" }),
    );
    await user.type(
      screen.getByLabelText("Description"),
      "Upload a governed protocol package.",
    );
    fireEvent.submit(
      screen.getByRole("dialog", { name: "Add source to Library" }).querySelector(
        "form",
      ) as HTMLFormElement,
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Queuing…" }),
      ).toBeDisabled(),
    );
    expect(uploadLibraryItem).toHaveBeenCalledTimes(1);
    const formData = jest.mocked(uploadLibraryItem).mock.calls[0]?.[0];
    expect(formData?.get("title")).toBe("Protocol package");
    expect(formData?.get("description")).toBe(
      "Upload a governed protocol package.",
    );
    expect(formData?.get("source")).toBe("Workspace upload");
    expect(formData?.get("kind")).toBe("Paper");
    expect(formData?.get("access")).toBe("internal");
    expect(formData?.get("license")).toBe("Project supplied");
    expect(formData?.get("publication_year")).toBe("2026");
    expect(formData?.get("file")).toBeInstanceOf(File);

    deferred.resolve({
      item: buildLibraryItem({ id: "uploaded-item" }),
      run: buildRunSummary({ id: "upload-run" }),
    });

    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Add source to Library" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("shows specific and fallback ingestion errors while keeping the dialog open", async () => {
    const user = setupUser();
    jest
      .mocked(uploadLibraryItem)
      .mockRejectedValueOnce(new Error("License validation blocked the upload."))
      .mockRejectedValueOnce("denied");

    render(<LibraryView data={buildWorkspaceData()} onRefresh={jest.fn()} />);

    await user.click(screen.getByRole("button", { name: "Ingest source" }));
    await user.type(screen.getByLabelText("Title"), "Blocked upload");
    await user.upload(
      screen.getByLabelText("Source file"),
      new File(["notes"], "notes.txt", { type: "text/plain" }),
    );
    await user.type(screen.getByLabelText("Description"), "Blocked test upload.");
    fireEvent.submit(
      screen.getByRole("dialog", { name: "Add source to Library" }).querySelector(
        "form",
      ) as HTMLFormElement,
    );

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "License validation blocked the upload.",
      ),
    );
    expect(
      screen.getByRole("dialog", { name: "Add source to Library" }),
    ).toBeInTheDocument();

    fireEvent.submit(
      screen.getByRole("dialog", { name: "Add source to Library" }).querySelector(
        "form",
      ) as HTMLFormElement,
    );
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Ingestion could not be queued.",
      ),
    );
  });

  it("shows an empty fallback when library data has not loaded", () => {
    render(<LibraryView data={null} onRefresh={jest.fn()} />);

    expect(screen.getByText("No sources match this view")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();
  });
});

describe("RunsView", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  function buildRunsWorkspaceData(): WorkspaceData {
    return buildWorkspaceData({
      runs: [
        buildRunSummary({
          id: "run-approval",
          capability: "literature",
          title: "Plan evidence map",
          status: "waiting_for_approval",
          progress: 61,
          current_stage: "Human review",
        }),
        buildRunSummary({
          id: "run-grant",
          capability: "grant",
          title: "Grant report",
          status: "running",
          progress: 54,
          current_stage: "Drafting",
          stages: [
            {
              id: "stage-plan",
              label: "Plan",
              owner: "planner",
              status: "completed",
            },
            {
              id: "stage-report",
              label: "Report",
              owner: "writer",
              status: "running",
            },
          ],
          scheduler_managed: true,
        }),
        buildRunSummary({
          id: "run-matching",
          capability: "matching",
          title: "Matching shortlist",
          status: "running",
          progress: 40,
          current_stage: "Scoring",
        }),
        buildRunSummary({
          id: "run-dataset",
          capability: "dataset",
          title: "Dataset synthesis",
          status: "completed",
          progress: 100,
          current_stage: "Complete",
          completed_at: "2026-07-16T15:00:00Z",
          scheduler_managed: true,
        }),
        buildRunSummary({
          id: "run-qa",
          capability: "institutional_qa",
          title: "Policy answer",
          status: "completed",
          progress: 100,
          current_stage: "Answered",
          completed_at: "2026-07-16T16:00:00Z",
        }),
        buildRunSummary({
          id: "run-orchestration",
          capability: "orchestration",
          title: "Workflow history",
          status: "completed",
          progress: 100,
          current_stage: "Activated",
          completed_at: "2026-07-16T17:00:00Z",
          stages: undefined,
        }),
      ],
      approvals: [buildApprovalRecord({ run_id: "run-approval" })],
    });
  }

  it("filters runs, switches selections, renders timelines, and remains accessible", async () => {
    const user = setupUser();
    const { container } = render(
      <RunsView data={buildRunsWorkspaceData()} onRefresh={jest.fn()} />,
    );

    expect(screen.getAllByText("Plan evidence map").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Grant report").length).toBeGreaterThan(0);
    expect(screen.getByText("Matching shortlist")).toBeInTheDocument();
    expect(screen.getByText("Dataset synthesis")).toBeInTheDocument();
    expect(screen.getByText("Policy answer")).toBeInTheDocument();
    expect(screen.getByText("Workflow history")).toBeInTheDocument();
    expect(screen.getByText("Exact gated action")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Running" }));
    expect(screen.getAllByText("Grant report").length).toBeGreaterThan(0);
    expect(screen.getByText("Matching shortlist")).toBeInTheDocument();
    expect(screen.queryByText("Plan evidence map")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /grant report/i }));
    expect(screen.getByText("Direct API execution")).toBeInTheDocument();
    expect(screen.getByText("Plan")).toBeInTheDocument();
    expect(screen.getByText("writer")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Completed" }));
    await user.click(screen.getByRole("button", { name: /workflow history/i }));
    expect(screen.getByText("No pending decision")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Needs approval" }));
    expect(screen.getAllByText("Plan evidence map").length).toBeGreaterThan(0);
    expect(screen.getByText("Activate graph v1.0.")).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it(
    "requires rationale before rejection and clears it after a successful decision",
    async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const deferred = createDeferred<ApprovalRecord>();
    jest.mocked(decideApproval).mockReturnValue(deferred.promise);

    render(
      <RunsView
        data={buildRunsWorkspaceData()}
        onRefresh={onRefresh}
        focusRunId="run-approval"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reject action" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Add a rationale before recording a decision.",
    );

    await user.type(screen.getByLabelText("Reviewer rationale"), "OK!");
    await user.click(screen.getByRole("button", { name: "Reject action" }));

    expect(decideApproval).toHaveBeenCalledWith("approval-1", "rejected", "OK!");
    expect(
      screen.getByRole("button", { name: "Reject action" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Approve exact action" }),
    ).toBeDisabled();
    // Rationale must remain disabled (not just the decision buttons) while a
    // decision is in flight -- the authoritative product decision is that
    // rationale stays disabled during the request, never editable mid-flight.
    expect(screen.getByLabelText("Reviewer rationale")).toBeDisabled();

    deferred.resolve(buildApprovalRecord({ state: "rejected" }));

    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Reject action" }),
      ).not.toBeDisabled(),
    );
    // After a successful decision settles, the rationale field is re-enabled
    // and cleared (the record's stored rationale is deleted on success), not
    // left disabled or holding stale text.
    await waitFor(() =>
      expect(screen.getByLabelText("Reviewer rationale")).not.toBeDisabled(),
    );
    expect(screen.getByLabelText("Reviewer rationale")).toHaveValue("");
    },
    10000,
  );

  it(
    "keeps rationale disabled mid-flight and re-enables it with the typed text preserved after a failed decision",
    async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const firstAttempt = createDeferred<ApprovalRecord>();
    jest
      .mocked(decideApproval)
      .mockReturnValueOnce(firstAttempt.promise)
      .mockRejectedValueOnce("denied");

    render(
      <RunsView
        data={buildRunsWorkspaceData()}
        onRefresh={onRefresh}
        focusRunId="run-approval"
      />,
    );

    const rationaleField = screen.getByLabelText("Reviewer rationale");
    await user.type(rationaleField, "Valid rationale");
    await user.click(screen.getByRole("button", { name: "Approve exact action" }));

    // Rationale is disabled while the decision request is in flight, before
    // it has even resolved or rejected.
    expect(rationaleField).toBeDisabled();

    firstAttempt.reject(new Error("Decision denied by policy."));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Decision denied by policy.",
    );
    // A failed decision re-enables rationale and preserves exactly what the
    // reviewer typed -- it must never be silently cleared on error, only on
    // a genuinely successful decision.
    await waitFor(() => expect(rationaleField).not.toBeDisabled());
    expect(rationaleField).toHaveValue("Valid rationale");

    await user.click(screen.getByRole("button", { name: "Approve exact action" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Decision could not be saved.",
    );
    await waitFor(() => expect(rationaleField).not.toBeDisabled());
    expect(rationaleField).toHaveValue("Valid rationale");
    },
    10000,
  );

  it("shows an empty workspace state without runs", () => {
    render(<RunsView data={null} onRefresh={jest.fn()} />);

    expect(screen.getByText("No durable runs available")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
  });
});

describe("SettingsView", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders fallback settings states and every policy tab when data is unavailable", async () => {
    const user = setupUser();
    const { container } = render(
      <SettingsView data={null} onRefresh={jest.fn()} />,
    );

    expect(screen.getByText("Loading project settings…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connectors 12/i })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Agents & Models" }));
    expect(screen.getByText("9 active")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Connectors 12/i }));
    expect(screen.getByText("Select a connector to configure it.")).toBeInTheDocument();
    expect(screen.getByText("0/12 ready")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retrieval & Evidence" }));
    expect(screen.getByText("Prompt-injection treatment")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Governance" }));
    expect(screen.getByText("Authenticated platform identity")).toBeInTheDocument();
    expect(screen.getByText("internal")).toBeInTheDocument();
    expect(screen.getByText("2555 days")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Evaluation" }));
    expect(screen.getByText("Retrieval completeness")).toBeInTheDocument();
    expect(
      container.querySelector('[data-evaluation-state="ready"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-evaluation-state="blocked"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('[data-evaluation-state="degraded"]'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Readiness" }));
    expect(screen.getByText("GitHub Copilot connector authoring")).toBeInTheDocument();
    for (const state of [
      "deployment-managed",
      "needs-consent",
      "blocked",
      "ready",
    ]) {
      expect(
        container.querySelector(`[data-readiness-state="${state}"]`),
      ).toBeInTheDocument();
    }
    expect(await axe(container)).toHaveNoViolations();
  });

  it("saves project settings, persists edits, and reports validation errors", async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const saveDeferred = createDeferred<WorkspaceData["settings"]>();
    jest
      .mocked(updateSettings)
      .mockReturnValueOnce(saveDeferred.promise)
      .mockRejectedValueOnce(new Error("Retention policy blocked the update."))
      .mockRejectedValueOnce("denied");

    render(
      <SettingsView data={buildWorkspaceData()} onRefresh={onRefresh} />,
    );

    await user.clear(screen.getByDisplayValue("Test workspace"));
    await user.type(screen.getByRole("textbox", { name: "Project name" }), "Governed workspace");
    await user.clear(screen.getByRole("textbox", { name: "Description" }));
    await user.type(
      screen.getByRole("textbox", { name: "Description" }),
      "Updated governance-safe description.",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Default classification" }),
      "restricted",
    );
    await user.clear(screen.getByRole("spinbutton", { name: "Retention (days)" }));
    await user.type(
      screen.getByRole("spinbutton", { name: "Retention (days)" }),
      "365",
    );
    await user.click(
      screen.getByRole("button", { name: "Save project settings" }),
    );

    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "Governed workspace",
        description: "Updated governance-safe description.",
        default_classification: "restricted",
        retention_days: 365,
      }),
    );
    expect(
      screen.getByRole("button", { name: "Saving…" }),
    ).toBeDisabled();

    saveDeferred.resolve({
      ...buildWorkspaceData().settings,
      name: "Governed workspace",
      description: "Updated governance-safe description.",
      default_classification: "restricted",
      retention_days: 365,
    });

    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(screen.getByRole("status")).toHaveTextContent("Project settings saved.");

    await user.click(
      screen.getByRole("button", { name: "Save project settings" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Retention policy blocked the update.",
    );

    await user.click(
      screen.getByRole("button", { name: "Save project settings" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Settings could not be saved.",
    );
  });

  it("renders agent and connector states, filters the catalog, and saves connector assignments", async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const pubmed = buildConnector();
    const grants = buildConnector({
      id: "grants_gov",
      name: "Grants.gov",
      category: "Funding",
      test_status: "configuration_required",
      description: "Funding notice baseline.",
      assigned_agents: ["grant"],
    });
    const orcid = buildConnector({
      id: "orcid",
      name: "ORCID",
      category: "Identity",
      enabled: false,
      auth_kind: "OAuth",
      test_status: "ready",
      assigned_agents: ["matching"],
    });
    const dataset = buildConnector({
      id: "datacite",
      name: "DataCite",
      category: "Datasets",
      test_status: "unavailable",
      auth_kind: "Managed identity",
      assigned_agents: ["dataset", "matching"],
    });
    const apim = buildConnector({
      id: "internal-apim",
      name: "Internal APIM Gateway",
      category: "Gateway",
      test_status: "ready_with_key",
      assigned_agents: [],
    });
    const mcp = buildConnector({
      id: "internal-mcp",
      name: "Internal MCP Registry",
      category: "Gateway",
      test_status: "configuration_required",
      assigned_agents: [],
    });
    const toolbox = buildConnector({
      id: "toolbox-registry",
      name: "Toolbox Registry",
      category: "Gateway",
      test_status: "not_tested",
      assigned_agents: [],
    });
    const data = buildWorkspaceData({
      summary: {
        ...buildWorkspaceData().summary,
        connector_ready: 3,
        connector_total: 6,
      },
      connectors: [pubmed, grants, orcid, dataset, apim, mcp, toolbox],
      agents: [
        buildAgent(),
        buildAgent({
          id: "grant",
          name: "Grant compliance",
          deployment: "Foundry Hosted Agent",
          workflow_steps: ["Intake", "Draft", "Review"],
        }),
      ],
    });
    jest.mocked(updateConnector).mockResolvedValue({
      ...dataset,
      enabled: false,
      assigned_agents: ["dataset", "literature"],
    });

    render(<SettingsView data={data} onRefresh={onRefresh} />);

    await user.click(screen.getByRole("button", { name: "Agents & Models" }));
    expect(screen.getByText("2 active")).toBeInTheDocument();
    expect(screen.getByText("Grant compliance")).toBeInTheDocument();
    expect(screen.getByText("Draft")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Connectors 7/i }));
    expect(screen.getByText("Ready, key recommended")).toBeInTheDocument();
    expect(screen.getAllByText("Setup required").length).toBeGreaterThan(0);
    expect(screen.getByText("Connection failed")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("Not tested")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /provider terms/i }),
    ).toHaveAttribute("href", pubmed.terms_url);
    expect(
      screen.getByRole("link", { name: /provider terms/i }),
    ).toHaveAttribute("target", "_blank");
    expect(
      screen.getByRole("link", { name: /provider terms/i }),
    ).toHaveAttribute("rel", "noopener noreferrer");

    await user.click(screen.getByRole("button", { name: "Funding" }));
    const catalog = screen
      .getByText("Connector catalog")
      .closest(".connector-catalog") as HTMLElement;
    expect(within(catalog).getAllByText("Grants.gov").length).toBeGreaterThan(0);
    expect(within(catalog).queryByText("DataCite")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "All" }));
    await user.type(screen.getByPlaceholderText("Search connectors"), "missing");
    expect(screen.getByText("No connectors match this filter.")).toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText("Search connectors"));

    await user.click(screen.getByRole("button", { name: /orcid/i }));
    expect(
      screen.getByText("Disabled connectors are excluded from research runs."),
    ).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Enable ORCID" })).not.toBeDisabled();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Connector to manage" }),
      "pubmed",
    );
    expect(screen.getByRole("checkbox", { name: "Enable PubMed" })).toBeDisabled();
    expect(
      screen.getByText("Required baseline connectors cannot be disabled."),
    ).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Connector to manage" }),
      "grants_gov",
    );
    expect(
      screen.getByRole("checkbox", { name: "Enable Grants.gov" }),
    ).toBeDisabled();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Connector to manage" }),
      "datacite",
    );
    await user.click(screen.getByRole("checkbox", { name: "Enable DataCite" }));
    await user.click(
      screen.getByRole("checkbox", { name: "Assign literature to DataCite" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "Assign matching to DataCite" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() =>
      expect(updateConnector).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "datacite",
          enabled: false,
          assigned_agents: ["dataset", "literature"],
        }),
      ),
    );
    expect(onRefresh).toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "DataCite configuration saved.",
    );
    expect(
      screen.getByText("Internal APIM Gateway is registered, but version promotion still requires administrator approval."),
    ).toBeInTheDocument();
  });

  it("shows catalogued connector tools and a legacy fallback", async () => {
    const user = setupUser();
    const pubmed = buildConnector({ operations: ["search", "lookup"] });
    const legacyConnector = buildConnector({
      id: "legacy-connector",
      name: "Legacy Connector",
      operations: undefined,
    });

    render(
      <SettingsView
        data={buildWorkspaceData({ connectors: [pubmed, legacyConnector] })}
        onRefresh={jest.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Connectors 2/i }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Connector to manage" }),
      "pubmed",
    );
    expect(screen.getByText("Exposed tools")).toBeInTheDocument();
    expect(screen.getByText("search · lookup")).toBeInTheDocument();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Connector to manage" }),
      "legacy-connector",
    );
    expect(screen.getByText("No approved operations")).toBeInTheDocument();
  });

  it("[pw.connector-terms:ready] shows an allowlisted connector terms link as an accessible external link", async () => {
    const pubmed = buildConnector();
    const data = buildWorkspaceData({ connectors: [pubmed] });
    const onRefresh = jest.fn().mockResolvedValue(undefined);

    const { container } = render(
      <SettingsView data={data} onRefresh={onRefresh} />,
    );
    await setupUser().click(
      screen.getByRole("button", { name: /Connectors 1/i }),
    );

    const termsLink = screen.getByRole("link", { name: /provider terms/i });
    expect(termsLink).toHaveAttribute("href", pubmed.terms_url);
    expect(termsLink).toHaveAttribute("data-terms-state", "ready");
    expect(
      screen.queryByText(/is not on the approved list/i),
    ).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("[pw.connector-terms:blocked-url] blocks a connector terms link that fails URL policy and shows a visible unavailable state", async () => {
    const blockedConnector = buildConnector({
      terms_url: "https://evil.example.com/terms",
    });
    const data = buildWorkspaceData({ connectors: [blockedConnector] });
    const onRefresh = jest.fn().mockResolvedValue(undefined);

    const { container } = render(
      <SettingsView data={data} onRefresh={onRefresh} />,
    );
    await setupUser().click(
      screen.getByRole("button", { name: /Connectors 1/i }),
    );

    expect(
      screen.queryByRole("link", { name: /provider terms/i }),
    ).not.toBeInTheDocument();
    const blockedState = screen.getByRole("status", {
      name: /is not on the approved list/i,
    });
    expect(blockedState).toHaveAttribute("data-terms-state", "blocked-url");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("shows connector test tones, fallback test errors, and update failures", async () => {
    const user = setupUser();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    const datacite = buildConnector({
      id: "datacite",
      name: "DataCite",
      category: "Datasets",
      test_status: "ready",
      assigned_agents: ["dataset"],
    });
    const deferred = createDeferred<ConnectorSetting>();
    jest
      .mocked(testConnector)
      .mockReturnValueOnce(deferred.promise)
      .mockResolvedValueOnce({
        ...datacite,
        test_status: "configuration_required",
      })
      .mockResolvedValueOnce({
        ...datacite,
        test_status: "unavailable",
      })
      .mockRejectedValueOnce(new Error("Connector probe denied."))
      .mockRejectedValueOnce("denied");
    jest
      .mocked(updateConnector)
      .mockRejectedValueOnce(new Error("Connector update denied."))
      .mockRejectedValueOnce("denied");

    render(
      <SettingsView
        data={buildWorkspaceData({ connectors: [datacite] })}
        onRefresh={onRefresh}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(screen.getByRole("button", { name: "Testing…" })).toBeDisabled();

    deferred.resolve({
      ...datacite,
      test_status: "ready",
    });

    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
    expect(screen.getByRole("status")).toHaveTextContent(
      "DataCite: Ready. The latest bounded probe succeeded and this connector can serve its assigned specialists.",
    );

    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "DataCite: Setup required.",
    );

    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "DataCite: Connection failed.",
    );

    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connector probe denied.",
    );

    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connector test failed.",
    );

    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connector update denied.",
    );

    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connector update failed.",
    );
  });

  it("blocks enabled connector configurations with no assigned specialist", async () => {
    const user = setupUser();
    const datacite = buildConnector({
      id: "datacite",
      name: "DataCite",
      category: "Datasets",
      enabled: true,
      assigned_agents: ["dataset"],
    });

    render(
      <SettingsView
        data={buildWorkspaceData({ connectors: [datacite] })}
        onRefresh={jest.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));
    await user.click(
      screen.getByRole("checkbox", { name: "Assign dataset to DataCite" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );

    expect(updateConnector).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Enabled connectors must be assigned to at least one specialist.",
    );
  });
});
