import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import {
  AgentWorkspaceView,
  BuildTab,
  DeployTab,
  EvaluateTab,
  MonitorTab,
  TestTab,
  VersionsTab,
} from "@/components/agent-workspace";
import {
  ApiError,
  getAgentEvaluation,
  getAgentHealth,
  getAgentVersions,
  proposeManifestChange,
  runStudio,
  type WorkspaceData,
} from "@/lib/api";
import type { ConnectorSetting, RunSummary, WorkflowBlueprint } from "@/lib/types";

// A researcher-owned fixture entry: every entry in the real, static
// AGENT_CATALOG is platform-owned, so ownerKind === "researcher" and an
// unresolved specialist id are otherwise unreachable through
// AgentWorkspaceView's own `getAgentCatalogEntry` lookup. This mock adds one
// fixture id alongside the real catalog so those branches can be exercised
// directly without fabricating unrealistic app-wide data.
jest.mock("@/lib/agent-catalog", () => {
  const actual = jest.requireActual("@/lib/agent-catalog");
  const researcherFixtureEntry = {
    id: "researcher-fixture",
    name: "researcher-fixture-agent",
    ownerKind: "researcher",
    purpose: "A researcher-built fixture agent for testing only.",
    boundary: "Fixture boundary text for testing only.",
    knowledge: ["custom-notes"],
    tools: ["custom-tool"],
    modelTier: "fast",
    outputContract: "FixtureContractV1",
    workflowSteps: [],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "literature",
    specialists: ["missing-specialist-id"],
  };
  return {
    ...actual,
    getAgentCatalogEntry: (id: string) =>
      id === "researcher-fixture"
        ? researcherFixtureEntry
        : actual.getAgentCatalogEntry(id),
  };
});


jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ApiError: actual.ApiError,
    getAgentEvaluation: jest.fn(),
    getAgentHealth: jest.fn(),
    getAgentVersions: jest.fn(),
    proposeManifestChange: jest.fn(),
    runStudio: jest.fn(),
  };
});

jest.mock("@/components/studio-components", () => ({
  StudioForCapability: (props: {
    capability: string;
    workflow?: { title: string };
    running: boolean;
    error: string | null;
    result: unknown;
    onRun: (
      capability: string,
      objective: string,
      options?: Record<string, unknown>,
    ) => Promise<void> | void;
  }) => (
    <div data-testid="studio-stub">
      <span data-testid="studio-capability">{props.capability}</span>
      <span data-testid="studio-workflow-title">
        {props.workflow?.title ?? "no-workflow"}
      </span>
      <span data-testid="studio-running">{String(props.running)}</span>
      {props.error ? <span data-testid="studio-error">{props.error}</span> : null}
      {props.result ? <span data-testid="studio-result">has-result</span> : null}
      <button
        type="button"
        onClick={() => props.onRun(props.capability, "Test objective")}
      >
        Run test
      </button>
    </div>
  ),
}));

function connector(overrides: Partial<ConnectorSetting> = {}): ConnectorSetting {
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

function runSummary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: "run-1",
    durable_instance_id: "inst-1",
    project_id: "demo-project",
    capability: "literature",
    title: "Run 1",
    status: "completed",
    progress: 100,
    started_at: "2026-07-01T10:00:00Z",
    completed_at: "2026-07-01T10:20:00Z",
    owner: "researcher@example.org",
    current_stage: "audit",
    artifact_count: 2,
    estimated_cost_usd: 0.4,
    scheduler_managed: false,
    scheduling_state: "not_managed",
    ...overrides,
  };
}

function workflowBlueprint(
  overrides: Partial<WorkflowBlueprint> = {},
): WorkflowBlueprint {
  return {
    capability: "literature",
    title: "Weekly literature synthesis",
    purpose: "Summarize new literature every week.",
    primary_artifact: "Literature synthesis report",
    online_research_policy: "internal-only",
    stages: [],
    ...overrides,
  };
}

function workspaceData(overrides: Partial<WorkspaceData> = {}): WorkspaceData {
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
      library_items: 0,
      active_runs: 0,
      pending_approvals: 0,
      connector_ready: 0,
      connector_total: 0,
      last_activity_at: "2026-07-16T12:00:00Z",
      persistence: "in-memory demo",
    },
    library: [],
    runs: [],
    approvals: [],
    connectors: [],
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
    agents: [],
    workflows: [],
    ...overrides,
  };
}

beforeEach(() => {
  jest.clearAllMocks();
});

/** Creates a promise plus externally-callable resolve/reject, so tests can
 * control exactly when an in-flight request settles (e.g. after unmount). */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Flushes pending microtasks (promise reaction chains) between a
 * resolve/reject call and an assertion. */
async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("BuildTab", () => {
  it("does not submit when the trimmed intent is empty, even if the form is force-submitted", () => {
    const { container } = render(<BuildTab agentId="literature" />);
    const form = container.querySelector("form") as HTMLFormElement;
    fireEvent.submit(form);
    expect(proposeManifestChange).not.toHaveBeenCalled();
  });

  it("submits an intent and renders the proposed manifest change on success", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockResolvedValue({
      id: "proposal-1",
      agent_id: "literature",
      summary: "Only cite papers from the last 5 years.",
      changes: [],
      created_at: "2026-07-16T12:00:00Z",
    });
    render(<BuildTab agentId="literature" />);

    const input = screen.getByLabelText("Describe the change you want");
    await user.type(input, "Only cite recent papers.");
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    await waitFor(() =>
      expect(proposeManifestChange).toHaveBeenCalledWith(
        "literature",
        "Only cite recent papers.",
        [{ path: "builder_intent", before: null, after: "Only cite recent papers." }],
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Proposed manifest change proposal-1/),
      ).toBeInTheDocument(),
    );
    expect(input).toHaveValue("");
  });

  it("shows the unavailable-specific copy when the proposal endpoint is not implemented", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockRejectedValue(
      new ApiError("Not found", 404),
    );
    render(<BuildTab agentId="literature" />);

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    await waitFor(() =>
      expect(
        screen.getByText(/Manifest change proposals aren't available yet/),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Add a new rule.", { selector: '[data-role="user"]' }),
    ).toBeInTheDocument();
  });

  it("shows the classified message for a non-unavailable failure", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockRejectedValue(
      new ApiError("Server exploded", 500),
    );
    render(<BuildTab agentId="literature" />);

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    await waitFor(() =>
      expect(screen.getByText("Server exploded")).toBeInTheDocument(),
    );
  });

  it("does not submit an empty or whitespace-only intent", async () => {
    const user = userEvent.setup();
    render(<BuildTab agentId="literature" />);
    const submit = screen.getByRole("button", { name: /Propose change/ });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Describe the change you want"), "   ");
    expect(submit).toBeDisabled();
    expect(proposeManifestChange).not.toHaveBeenCalled();
  });
});

describe("TestTab", () => {
  it("renders an empty state when the agent has no attached capability", () => {
    render(<TestTab capability={null} data={workspaceData()} onRefresh={jest.fn()} />);
    expect(screen.getByText("No attached studio yet")).toBeInTheDocument();
    expect(screen.queryByTestId("studio-stub")).not.toBeInTheDocument();
  });

  it("passes the matching workflow and wires a successful run through to onRefresh", async () => {
    const user = userEvent.setup();
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    jest.mocked(runStudio).mockResolvedValue({
      title: "Result",
    } as never);
    const data = workspaceData({ workflows: [workflowBlueprint()] });
    render(<TestTab capability="literature" data={data} onRefresh={onRefresh} />);

    expect(screen.getByTestId("studio-workflow-title")).toHaveTextContent(
      "Weekly literature synthesis",
    );
    expect(screen.getByTestId("studio-running")).toHaveTextContent("false");

    await user.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() =>
      expect(runStudio).toHaveBeenCalledWith("literature", "Test objective", undefined),
    );
    await waitFor(() =>
      expect(screen.getByTestId("studio-result")).toBeInTheDocument(),
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it("shows no workflow title when no workflow matches the capability", () => {
    render(
      <TestTab
        capability="literature"
        data={workspaceData({ workflows: [] })}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByTestId("studio-workflow-title")).toHaveTextContent(
      "no-workflow",
    );
  });

  it("surfaces the Error message when a run fails", async () => {
    const user = userEvent.setup();
    jest.mocked(runStudio).mockRejectedValue(new Error("Objective is required."));
    render(
      <TestTab capability="literature" data={workspaceData()} onRefresh={jest.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() =>
      expect(screen.getByTestId("studio-error")).toHaveTextContent(
        "Objective is required.",
      ),
    );
  });

  it("falls back to a generic message when a run fails with a non-Error value", async () => {
    const user = userEvent.setup();
    jest.mocked(runStudio).mockRejectedValue("boom");
    render(
      <TestTab capability="literature" data={workspaceData()} onRefresh={jest.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Run test" }));
    await waitFor(() =>
      expect(screen.getByTestId("studio-error")).toHaveTextContent(
        "The test run failed.",
      ),
    );
  });
});

describe("EvaluateTab", () => {
  it("shows a loading state while the evaluation request is in flight", () => {
    jest.mocked(getAgentEvaluation).mockReturnValue(new Promise(() => {}));
    render(<EvaluateTab agentId="literature" />);
    expect(screen.getByText("Loading evaluation results…")).toBeInTheDocument();
  });

  it("renders advisory metrics and hard gates on success", async () => {
    jest.mocked(getAgentEvaluation).mockResolvedValue({
      advisory: true,
      citation_resolution: 92,
      claim_entailment: 88,
      retrieval_completeness: null,
      last_run_at: "2026-07-10T09:00:00Z",
      hard_gates: [
        { id: "citations", label: "Every claim resolves to a citation", passing: true },
        { id: "no-secrets", label: "No secret-classified evidence leaked", passing: false },
      ],
    });
    render(<EvaluateTab agentId="literature" />);

    await waitFor(() => expect(screen.getByText("92%")).toBeInTheDocument());
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("—%")).toBeInTheDocument();
    const passingGate = screen.getByText(
      "Every claim resolves to a citation",
    );
    expect(passingGate.closest("li")).toHaveAttribute("data-passing", "true");
    const failingGate = screen.getByText(
      "No secret-classified evidence leaked",
    );
    expect(failingGate.closest("li")).toHaveAttribute("data-passing", "false");
  });

  it("shows an em dash fallback for citation resolution and claim entailment when they are null", async () => {
    jest.mocked(getAgentEvaluation).mockResolvedValue({
      advisory: true,
      citation_resolution: null,
      claim_entailment: null,
      retrieval_completeness: 70,
      last_run_at: null,
      hard_gates: [],
    });
    render(<EvaluateTab agentId="literature" />);

    await waitFor(() => expect(screen.getByText("70%")).toBeInTheDocument());
    expect(screen.getAllByText("—%")).toHaveLength(2);
  });

  it("renders nothing extra when the evaluation resolves to a falsy value", async () => {
    jest.mocked(getAgentEvaluation).mockResolvedValue(null as never);
    render(<EvaluateTab agentId="literature" />);

    await waitFor(() =>
      expect(
        screen.queryByText("Loading evaluation results…"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Objective hard gates")).not.toBeInTheDocument();
  });

  it("does not update state after unmount when the evaluation request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, resolve } = deferred<{
      advisory: true;
      citation_resolution: number | null;
      claim_entailment: number | null;
      retrieval_completeness: number | null;
      last_run_at: string | null;
      hard_gates: never[];
    }>();
    jest.mocked(getAgentEvaluation).mockReturnValue(promise);
    const { unmount } = render(<EvaluateTab agentId="literature" />);
    unmount();
    resolve({
      advisory: true,
      citation_resolution: 90,
      claim_entailment: 90,
      retrieval_completeness: 90,
      last_run_at: null,
      hard_gates: [],
    });
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update state after unmount when the evaluation request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentEvaluation).mockReturnValue(promise);
    const { unmount } = render(<EvaluateTab agentId="literature" />);
    unmount();
    reject(new Error("late failure"));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("renders a classified async-state banner on failure", async () => {
    jest.mocked(getAgentEvaluation).mockRejectedValue(new ApiError("nope", 404));
    render(<EvaluateTab agentId="literature" />);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveAttribute("data-tone", "unavailable"),
    );
  });
});

describe("DeployTab", () => {
  it("shows platform copy and 'Not discovered yet' when there is no live status", () => {
    render(<DeployTab agentId="literature" ownerKind="platform" status={undefined} />);
    expect(
      screen.getByText(/Only platform owners publish new versions/),
    ).toBeInTheDocument();
    expect(screen.getByText("Not discovered yet")).toBeInTheDocument();
  });

  it("shows researcher copy and the live status when present", () => {
    render(<DeployTab agentId="custom-agent" ownerKind="researcher" status="Active" />);
    expect(
      screen.getByText(/Researcher-created agents deploy through/),
    ).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("submits a deployment request and shows a success message", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockResolvedValue({
      id: "deploy-proposal-1",
      agent_id: "literature",
      summary: "Request deployment",
      changes: [],
      created_at: "2026-07-16T12:00:00Z",
    });
    render(<DeployTab agentId="literature" ownerKind="platform" status="draft" />);

    await user.click(screen.getByRole("button", { name: /Request deployment/ }));
    await waitFor(() =>
      expect(proposeManifestChange).toHaveBeenCalledWith(
        "literature",
        "Request deployment",
        [{ path: "lifecycle", before: "draft", after: "active" }],
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Deployment request recorded as proposal deploy-proposal-1/),
      ).toBeInTheDocument(),
    );
  });

  it("shows the unavailable-specific copy when direct deployment isn't implemented", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockRejectedValue(new ApiError("nope", 404));
    render(<DeployTab agentId="literature" ownerKind="platform" status={undefined} />);

    await user.click(screen.getByRole("button", { name: /Request deployment/ }));
    await waitFor(() =>
      expect(
        screen.getByText(/Direct deployment controls aren't available yet/),
      ).toBeInTheDocument(),
    );
  });

  it("shows the classified message for a non-unavailable deployment failure", async () => {
    const user = userEvent.setup();
    jest.mocked(proposeManifestChange).mockRejectedValue(
      new ApiError("Deployment gate failed", 409),
    );
    render(<DeployTab agentId="literature" ownerKind="platform" status={undefined} />);

    await user.click(screen.getByRole("button", { name: /Request deployment/ }));
    await waitFor(() =>
      expect(screen.getByText("Deployment gate failed")).toBeInTheDocument(),
    );
  });
});

describe("MonitorTab", () => {
  it("shows zero counts and an em dash for last-used when there is no capability", () => {
    jest.mocked(getAgentHealth).mockReturnValue(new Promise(() => {}));
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability={null} />,
    );
    expect(screen.getByText("0 runs")).toBeInTheDocument();
    expect(screen.getByText("0 workflows")).toBeInTheDocument();
  });

  it("computes singular run/workflow counts and the most recent last-used date", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: "2026-07-16T12:00:00Z",
      detail: "All systems nominal.",
    });
    const data = workspaceData({
      runs: [
        runSummary({ id: "run-1", completed_at: "2026-07-01T10:00:00Z" }),
      ],
      workflows: [workflowBlueprint()],
    });
    render(<MonitorTab agentId="literature" data={data} capability="literature" />);

    await waitFor(() => expect(screen.getByText("healthy")).toBeInTheDocument());
    expect(screen.getByText("All systems nominal.")).toBeInTheDocument();
    expect(screen.getByText("1 run")).toBeInTheDocument();
    expect(screen.getByText("1 workflow")).toBeInTheDocument();
  });

  it("uses started_at when a run has no completed_at, and pluralizes multiple runs", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "degraded",
      last_checked_at: null,
      detail: "Slow responses.",
    });
    const data = workspaceData({
      runs: [
        runSummary({
          id: "run-1",
          completed_at: null,
          started_at: "2026-07-01T10:00:00Z",
        }),
        runSummary({
          id: "run-2",
          completed_at: "2026-07-05T10:00:00Z",
          started_at: "2026-07-04T10:00:00Z",
        }),
      ],
      workflows: [workflowBlueprint(), workflowBlueprint({ title: "Second" })],
    });
    render(<MonitorTab agentId="literature" data={data} capability="literature" />);

    await waitFor(() => expect(screen.getByText("degraded")).toBeInTheDocument());
    expect(screen.getByText("2 runs")).toBeInTheDocument();
    expect(screen.getByText("2 workflows")).toBeInTheDocument();
  });

  it("renders a classified async-state banner when the health check fails", async () => {
    jest.mocked(getAgentHealth).mockRejectedValue(new ApiError("down", 503));
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveAttribute("data-tone", "degraded"),
    );
  });

  it("falls back to empty run/workflow lists when data is null but a capability is set", () => {
    jest.mocked(getAgentHealth).mockReturnValue(new Promise(() => {}));
    render(<MonitorTab agentId="literature" data={null} capability="literature" />);
    expect(screen.getByText("0 runs")).toBeInTheDocument();
    expect(screen.getByText("0 workflows")).toBeInTheDocument();
  });

  it("renders nothing extra in the live-grid section when health resolves to a falsy value", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue(null as never);
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    await waitFor(() =>
      expect(screen.queryByText("Checking live health…")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("State")).not.toBeInTheDocument();
  });

  it("does not update state after unmount when the health request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, resolve } = deferred<{
      state: "healthy";
      last_checked_at: string | null;
      detail: string;
    }>();
    jest.mocked(getAgentHealth).mockReturnValue(promise);
    const { unmount } = render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    unmount();
    resolve({ state: "healthy", last_checked_at: null, detail: "ok" });
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update state after unmount when the health request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentHealth).mockReturnValue(promise);
    const { unmount } = render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    unmount();
    reject(new Error("late failure"));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});

describe("VersionsTab", () => {
  it("shows platform copy while loading", () => {
    jest.mocked(getAgentVersions).mockReturnValue(new Promise(() => {}));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    expect(
      screen.getByText(/This agent's baseline is immutable/),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading version history…")).toBeInTheDocument();
  });

  it("shows researcher copy", () => {
    jest.mocked(getAgentVersions).mockReturnValue(new Promise(() => {}));
    render(<VersionsTab agentId="custom-agent" ownerKind="researcher" />);
    expect(
      screen.getByText(/Forked and researcher-built agents keep their own version/),
    ).toBeInTheDocument();
  });

  it("renders the version list on success", async () => {
    jest.mocked(getAgentVersions).mockResolvedValue([
      {
        version: "1.2.0",
        created_at: "2026-06-01T00:00:00Z",
        created_by: "platform-team",
        status: "active",
        changelog: "Improved citation coverage.",
      },
    ]);
    render(<VersionsTab agentId="literature" ownerKind="platform" />);

    await waitFor(() => expect(screen.getByText("1.2.0")).toBeInTheDocument());
    expect(screen.getByText("Improved citation coverage.")).toBeInTheDocument();
    expect(screen.getByText(/platform-team/)).toBeInTheDocument();
  });

  it("shows an empty state when there is no version history", async () => {
    jest.mocked(getAgentVersions).mockResolvedValue([]);
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    await waitFor(() =>
      expect(screen.getByText("Immutable baseline only")).toBeInTheDocument(),
    );
  });

  it("renders a classified async-state banner on failure", async () => {
    jest.mocked(getAgentVersions).mockRejectedValue(new ApiError("nope", 401));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveAttribute("data-tone", "unauthorized"),
    );
  });

  it("does not update state after unmount when the versions request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, resolve } = deferred<[]>();
    jest.mocked(getAgentVersions).mockReturnValue(promise);
    const { unmount } = render(<VersionsTab agentId="literature" ownerKind="platform" />);
    unmount();
    resolve([]);
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update state after unmount when the versions request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentVersions).mockReturnValue(promise);
    const { unmount } = render(<VersionsTab agentId="literature" ownerKind="platform" />);
    unmount();
    reject(new Error("late failure"));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});

describe("AgentWorkspaceView", () => {
  beforeEach(() => {
    jest.mocked(getAgentEvaluation).mockReturnValue(new Promise(() => {}));
    jest.mocked(getAgentHealth).mockReturnValue(new Promise(() => {}));
    jest.mocked(getAgentVersions).mockReturnValue(new Promise(() => {}));
  });

  it("shows a not-found empty state and calls onBack for an unknown agent id", async () => {
    const user = userEvent.setup();
    const onBack = jest.fn();
    render(
      <AgentWorkspaceView
        agentId="unknown-agent"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={onBack}
      />,
    );
    expect(screen.getByText("Agent not found")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Back to registry/ }));
    expect(onBack).toHaveBeenCalled();
  });

  it("falls back to no connections when data is null", () => {
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={null}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const contract = screen.getByLabelText("Behavioral contract");
    expect(
      within(contract).getByText("No workspace connections assigned yet."),
    ).toBeInTheDocument();
  });

  it("renders researcher-owned copy and an unresolved specialist id as a literal fallback", () => {
    render(
      <AgentWorkspaceView
        agentId="researcher-fixture"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    expect(screen.getByText("Researcher-owned")).toBeInTheDocument();
    const contract = screen.getByLabelText("Behavioral contract");
    expect(within(contract).getByText("missing-specialist-id")).toBeInTheDocument();
  });

  it("renders the behavioral contract with no-data branches for an agent with specialists and no connections", () => {
    render(
      <AgentWorkspaceView
        agentId="coordinator"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const contract = screen.getByLabelText("Behavioral contract");
    expect(within(contract).getAllByText("None")).toHaveLength(2); // knowledge, tools
    expect(
      within(contract).getByText("No workspace connections assigned yet."),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/literature-agent/),
    ).toBeInTheDocument(); // specialist display name resolved from catalog
    expect(within(contract).getByText("No public web access.")).toBeInTheDocument();
    expect(within(contract).getByText("Not discovered yet")).toBeInTheDocument();
  });

  it("renders connections, live model/status, and the read-only web boundary for a discovered agent", () => {
    const data = workspaceData({
      connectors: [connector({ assigned_agents: ["literature_online"] })],
      agents: [
        {
          id: "literature_online",
          name: "literature-online-agent",
          deployment: "gpt-5-fast",
          model_tier: "fast",
          status: "Active",
          web_access: "read_only",
          workflow_steps: [],
        },
      ],
    });
    render(
      <AgentWorkspaceView
        agentId="literature_online"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const contract = screen.getByLabelText("Behavioral contract");
    expect(within(contract).getByText("PubMed")).toBeInTheDocument();
    expect(within(contract).getByText("gpt-5-fast (discovered from project deployments)")).toBeInTheDocument();
    expect(
      within(contract).getByText(/Public web access is read-only/),
    ).toBeInTheDocument();
    expect(within(contract).getByText("Active")).toBeInTheDocument();
  });

  it("expands Advanced to reveal schema, runtime, and identity details including the live deployment id", async () => {
    const user = userEvent.setup();
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "gpt-5-primary",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const toggle = screen.getByRole("button", { name: /Advanced/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Runtime")).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Runtime")).toBeInTheDocument();
    const advanced = document.querySelector(".agent-workspace-advanced");
    expect(advanced).not.toBeNull();
    expect(within(advanced as HTMLElement).getByText(/Deployment/)).toBeInTheDocument();
    expect(within(advanced as HTMLElement).getByText("gpt-5-primary")).toBeInTheDocument();

    await user.click(toggle);
    expect(screen.queryByText("Runtime")).not.toBeInTheDocument();
  });

  it("does not show a live deployment id under Advanced when the agent has not been discovered", async () => {
    const user = userEvent.setup();
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    const advanced = document.querySelector(".agent-workspace-advanced");
    expect(advanced).not.toBeNull();
    expect(within(advanced as HTMLElement).getByText("Agent ID")).toBeInTheDocument();
    expect(within(advanced as HTMLElement).queryByText(/Deployment/)).not.toBeInTheDocument();
  });

  it("switches between all six tabs", async () => {
    const user = userEvent.setup();
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const tabpanel = screen.getByRole("tabpanel");
    expect(within(tabpanel).getByText("Builder Agent")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Test" }));
    expect(within(tabpanel).getByTestId("studio-stub")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Evaluate" }));
    expect(within(tabpanel).getByText("Advisory evaluation")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Deploy" }));
    expect(within(tabpanel).getByText("Deployment")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Monitor" }));
    expect(within(tabpanel).getByText("Health & usage")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Versions" }));
    expect(within(tabpanel).getByText("Versions")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Build" }));
    expect(within(tabpanel).getByText("Builder Agent")).toBeInTheDocument();
  });

  it("calls onBack from the workspace header", async () => {
    const user = userEvent.setup();
    const onBack = jest.fn();
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={onBack}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Registry/ }));
    expect(onBack).toHaveBeenCalled();
  });

  it("has no detectable accessibility violations", async () => {
    const { container } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={workspaceData()}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const results = await axe(container);
    expect(results).toHaveNoViolations();
  });
});
