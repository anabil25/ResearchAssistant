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
  applyBuilderProposal,
  forgetAgentMemoryScope,
  getAgentDeployment,
  getAgentDraft,
  getAgentEvaluation,
  getAgentHealth,
  getAgentRelease,
  getAgentReleases,
  getAgentTraces,
  getCapabilityDiscovery,
  postBuilderMessage,
  runStudio,
  type WorkspaceData,
} from "@/lib/api";
import type {
  AgentBuilderProposal,
  AgentContractView,
  AgentDraftView,
  AgentReleaseSummary,
  AgentVersionSummary,
  DeploymentSummary,
  RunSummary,
  WorkflowBlueprint,
} from "@/lib/types";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ApiError: actual.ApiError,
    applyBuilderProposal: jest.fn(),
    forgetAgentMemoryScope: jest.fn(),
    getAgentDeployment: jest.fn(),
    getAgentDraft: jest.fn(),
    getAgentEvaluation: jest.fn(),
    getAgentHealth: jest.fn(),
    getAgentRelease: jest.fn(),
    getAgentReleases: jest.fn(),
    getAgentTraces: jest.fn(),
    getCapabilityDiscovery: jest.fn(),
    postBuilderMessage: jest.fn(),
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

function emptyContract(): AgentContractView {
  return {
    purpose: null,
    boundary: null,
    input_artifact: null,
    instructions: null,
    evidence_policy: null,
    model: { deployment: null, discovered: false },
    knowledge: null,
    tools: null,
    memory: { scopes: [] },
    connections: null,
    specialists: null,
    capabilities: null,
    safety: null,
    tests: null,
    deployment: null,
    public_boundary: null,
  };
}

function draftView(overrides: Partial<AgentDraftView> = {}): AgentDraftView {
  return {
    draft_id: "draft-7",
    agent_id: null,
    base_version: null,
    status: "editing",
    etag: "etag-1",
    contract: emptyContract(),
    created_by: "researcher",
    created_at: "2026-07-16T12:00:00Z",
    ...overrides,
  };
}

function builderProposal(
  overrides: Partial<AgentBuilderProposal> = {},
): AgentBuilderProposal {
  return {
    proposal_id: "proposal-1",
    draft_id: "draft-7",
    summary: "Only cite papers from the last 5 years.",
    patch: [],
    before_summary: "Cites papers of any age.",
    after_summary: "Only cites papers from the last 5 years.",
    capability_changes: [],
    permission_changes: [],
    data_boundary_changes: [],
    validation_warnings: [],
    base_etag: "etag-1",
    ...overrides,
  };
}

function releaseSummary(
  overrides: Partial<AgentVersionSummary> = {},
  deploymentOverrides: Partial<DeploymentSummary> = {},
): AgentReleaseSummary {
  return {
    version_summary: {
      version: "1.0.0",
      created_at: "2026-01-01T00:00:00Z",
      created_by: "platform",
      changelog: "Initial release.",
      derived_from: null,
      content_hash: "sha256:0000000000000000",
      model_version: "gpt-5-fast",
      capability_versions: {},
      ...overrides,
    },
    deployment: {
      deployment_status: "deployed",
      ...deploymentOverrides,
    },
  };
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(getCapabilityDiscovery).mockResolvedValue({
    descriptors: [],
    instances: [],
    warnings: [],
    refreshed_at: null,
  });
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
  beforeEach(() => {
    jest.mocked(getAgentDraft).mockResolvedValue(draftView());
  });

  it("does not submit when the trimmed intent is empty, even if the form is force-submitted", async () => {
    const { container } = render(<BuildTab agentId="literature" />);
    const form = container.querySelector("form") as HTMLFormElement;
    fireEvent.submit(form);
    expect(postBuilderMessage).not.toHaveBeenCalled();

    // Also force-submit after the real draft has loaded, so the empty-intent
    // guard is exercised with draftReady true, not only blocked upstream by
    // the not-yet-loaded guard.
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );
    fireEvent.submit(form);
    expect(postBuilderMessage).not.toHaveBeenCalled();
  });

  it("shows a loading draft-status chip, then the loaded draft's real status once it resolves", async () => {
    jest.mocked(getAgentDraft).mockResolvedValue(draftView({ status: "validating" }));
    render(<BuildTab agentId="literature" />);
    expect(screen.getByText("Draft status: loading…")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("Draft status: validating")).toBeInTheDocument(),
    );
  });

  it("shows an unavailable banner with retry and blocks submission when the draft fetch fails", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockRejectedValue(new ApiError("no draft endpoint", 404));
    render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: unavailable")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/couldn't be loaded, so builder changes are disabled/),
    ).toBeInTheDocument();

    // Never falls back to a fabricated draftId/etag — submit stays disabled.
    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    const submit = screen.getByRole("button", { name: "Waiting for draft…" });
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(postBuilderMessage).not.toHaveBeenCalled();

    // Retrying can fail again — it stays in the unavailable/blocked state
    // rather than silently unblocking submission.
    jest.mocked(getAgentDraft).mockRejectedValue(new ApiError("still down", 503));
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByText("Draft status: unavailable")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: "Waiting for draft…" }),
    ).toBeDisabled();

    jest.mocked(getAgentDraft).mockResolvedValue(draftView());
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /Propose change/ }),
    ).not.toBeDisabled();
  });

  it("submits an intent and renders the proposed change for review, then applies it", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({ draft_id: "draft-42", etag: "etag-42" }),
    );
    jest.mocked(postBuilderMessage).mockResolvedValue(
      builderProposal({
        draft_id: "draft-42",
        base_etag: "etag-42",
        capability_changes: ["Attach web-search v2"],
        permission_changes: ["Grant write access to Workspace Library"],
        data_boundary_changes: ["Outbound results now include full text"],
        validation_warnings: ["Citation coverage threshold not yet re-evaluated"],
      }),
    );
    jest.mocked(applyBuilderProposal).mockResolvedValue(
      draftView({ draft_id: "draft-42", status: "ready_for_review" }),
    );
    render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );

    const input = screen.getByLabelText("Describe the change you want");
    await user.type(input, "Only cite recent papers.");
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    await waitFor(() =>
      expect(postBuilderMessage).toHaveBeenCalledWith(
        "draft-42",
        "Only cite recent papers.",
        "etag-42",
      ),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Proposed change proposal-1: Only cite papers from the last 5 years\./),
      ).toBeInTheDocument(),
    );
    expect(input).toHaveValue("");
    expect(screen.getByText("Cites papers of any age.")).toBeInTheDocument();
    expect(screen.getByText("Attach web-search v2")).toBeInTheDocument();
    expect(
      screen.getByText("Grant write access to Workspace Library"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Outbound results now include full text"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Citation coverage threshold not yet re-evaluated"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve & apply" }));
    await waitFor(() =>
      expect(applyBuilderProposal).toHaveBeenCalledWith(
        "draft-42",
        "proposal-1",
        "etag-42",
      ),
    );
    await waitFor(() =>
      expect(screen.getByText("Change applied to the draft.")).toBeInTheDocument(),
    );
    expect(screen.getByText("Draft status: ready_for_review")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Approve & apply" }),
    ).not.toBeInTheDocument();
  });

  it("keeps submission blocked while the draft is still loading, even if the user types quickly", async () => {
    const user = userEvent.setup();
    const draftPromise = deferred<AgentDraftView>();
    jest.mocked(getAgentDraft).mockReturnValue(draftPromise.promise);
    jest.mocked(postBuilderMessage).mockResolvedValue(builderProposal());
    render(<BuildTab agentId="literature" />);
    expect(screen.getByText("Draft status: loading…")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    const submit = screen.getByRole("button", { name: "Waiting for draft…" });
    expect(submit).toBeDisabled();
    await user.click(submit);
    expect(postBuilderMessage).not.toHaveBeenCalled();

    draftPromise.resolve(draftView({ draft_id: "draft-42", etag: "etag-42" }));
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));
    await waitFor(() =>
      expect(postBuilderMessage).toHaveBeenCalledWith(
        "draft-42",
        "Add a new rule.",
        "etag-42",
      ),
    );
  });

  it("shows the unavailable-specific copy when the builder-message endpoint is not implemented", async () => {
    const user = userEvent.setup();
    jest.mocked(postBuilderMessage).mockRejectedValue(new ApiError("Not found", 404));
    render(<BuildTab agentId="literature" />);

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    await waitFor(() =>
      expect(
        screen.getByText(/Builder proposals aren't available yet/),
      ).toBeInTheDocument(),
    );
    const userMessage = screen.getByText("Add a new rule.");
    expect(userMessage).toHaveAttribute("data-role", "user");
  });

  it("shows the classified message for a non-unavailable builder-message failure", async () => {
    const user = userEvent.setup();
    jest.mocked(postBuilderMessage).mockRejectedValue(new ApiError("Server exploded", 500));
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

  it("shows the unavailable-specific copy when applying a proposal is not implemented", async () => {
    const user = userEvent.setup();
    jest.mocked(postBuilderMessage).mockResolvedValue(builderProposal());
    jest.mocked(applyBuilderProposal).mockRejectedValue(new ApiError("nope", 404));
    render(<BuildTab agentId="literature" />);

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve & apply" })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Approve & apply" }));
    await waitFor(() =>
      expect(
        screen.getByText(/Applying this proposal isn't available yet/),
      ).toBeInTheDocument(),
    );
  });

  it("classifies a 409 from apply as a concurrency conflict, not a generic or governance-approval error, and reloads the draft", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({ draft_id: "draft-42", etag: "etag-42" }),
    );
    jest.mocked(postBuilderMessage).mockResolvedValue(builderProposal());
    jest.mocked(applyBuilderProposal).mockRejectedValue(
      new ApiError("Draft was modified concurrently", 409),
    );
    render(<BuildTab agentId="literature" />);
    await waitFor(() => expect(getAgentDraft).toHaveBeenCalledTimes(1));

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve & apply" })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Approve & apply" }));

    const conflictMessage = await screen.findByText(
      /This draft changed since you last loaded it \(etag conflict\)/,
    );
    // Distinct styling/kind from generic error and from governance
    // "needs_approval" holds — an ETag conflict is neither.
    expect(conflictMessage).toHaveAttribute("data-tone", "conflict");
    expect(conflictMessage).toHaveAttribute("role", "alert");
    expect(screen.queryByText("Draft was modified concurrently")).not.toBeInTheDocument();
    // The tone must be legible without color: a text badge names it
    // "Conflict" regardless of background tint.
    expect(within(conflictMessage).getByText("Conflict")).toBeInTheDocument();

    // The conflict is treated as actionable: the draft is refetched so a
    // fresh etag is available for the user's next attempt.
    await waitFor(() => expect(getAgentDraft).toHaveBeenCalledTimes(2));
  });

  it("classifies a 409 from the builder-message submit as a concurrency conflict with accessible alert styling", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({ draft_id: "draft-42", etag: "etag-42" }),
    );
    jest.mocked(postBuilderMessage).mockRejectedValue(
      new ApiError("stale etag", 409),
    );
    render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    const conflictMessage = await screen.findByText(
      /This draft changed since you last loaded it \(etag conflict\)/,
    );
    expect(conflictMessage).toHaveAttribute("data-tone", "conflict");
    expect(conflictMessage).toHaveAttribute("role", "alert");
    expect(within(conflictMessage).getByText("Conflict")).toBeInTheDocument();
  });

  it("has no detectable accessibility violations with a non-color-only conflict-tone builder message rendered", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({ draft_id: "draft-42", etag: "etag-42" }),
    );
    jest.mocked(postBuilderMessage).mockRejectedValue(
      new ApiError("stale etag", 409),
    );
    const { container } = render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));
    await screen.findByText(
      /This draft changed since you last loaded it \(etag conflict\)/,
    );

    expect(await axe(container)).toHaveNoViolations();
  });

  it("gives non-error/conflict builder messages a status role, not an alert role", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({ draft_id: "draft-42", etag: "etag-42" }),
    );
    jest.mocked(postBuilderMessage).mockResolvedValue(builderProposal());
    render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );

    await user.type(
      screen.getByLabelText("Describe the change you want"),
      "Add a new rule.",
    );
    await user.click(screen.getByRole("button", { name: /Propose change/ }));

    const successMessage = await screen.findByText(
      /Proposed change proposal-1/,
    );
    expect(successMessage).toHaveAttribute("data-tone", "success");
    expect(successMessage).toHaveAttribute("role", "status");
    expect(within(successMessage).getByText("Success")).toBeInTheDocument();
  });

  it("does nothing when Approve & apply is invoked with no active proposal", () => {
    render(<BuildTab agentId="literature" />);
    expect(
      screen.queryByRole("button", { name: "Approve & apply" }),
    ).not.toBeInTheDocument();
    expect(applyBuilderProposal).not.toHaveBeenCalled();
  });

  it("does not submit an empty or whitespace-only intent", async () => {
    const user = userEvent.setup();
    render(<BuildTab agentId="literature" />);
    await waitFor(() =>
      expect(screen.getByText("Draft status: editing")).toBeInTheDocument(),
    );
    const submit = screen.getByRole("button", { name: /Propose change/ });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText("Describe the change you want"), "   ");
    expect(submit).toBeDisabled();
    expect(postBuilderMessage).not.toHaveBeenCalled();
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
  it("shows platform copy and a loading state, then 'Not deployed yet' when there is no version", async () => {
    jest.mocked(getAgentDeployment).mockResolvedValue({ status: "not_deployed", version: null });
    render(<DeployTab agentId="literature" ownerKind="platform" />);
    expect(
      screen.getByText(/Only platform owners publish new versions/),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading deployment status…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("not_deployed")).toBeInTheDocument());
    expect(screen.getByText("Not deployed yet")).toBeInTheDocument();
  });

  it("shows researcher copy and the deployed version when present", async () => {
    jest
      .mocked(getAgentDeployment)
      .mockResolvedValue({ status: "deployed", version: "1.2.0" });
    render(<DeployTab agentId="custom-agent" ownerKind="researcher" />);
    expect(
      screen.getByText(/Researcher-created agents deploy through/),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("1.2.0")).toBeInTheDocument());
    expect(screen.getByText("deployed")).toBeInTheDocument();
  });

  it("renders a classified async-state banner when the deployment fetch fails", async () => {
    jest.mocked(getAgentDeployment).mockRejectedValue(new ApiError("nope", 404));
    render(<DeployTab agentId="literature" ownerKind="platform" />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveAttribute("data-tone", "unavailable"),
    );
  });

  it("does not update state after unmount when the deployment request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, resolve } = deferred<{ status: string; version: string | null }>();
    jest.mocked(getAgentDeployment).mockReturnValue(promise);
    const { unmount } = render(<DeployTab agentId="literature" ownerKind="platform" />);
    unmount();
    resolve({ status: "deployed", version: "1.0.0" });
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update state after unmount when the deployment request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentDeployment).mockReturnValue(promise);
    const { unmount } = render(<DeployTab agentId="literature" ownerKind="platform" />);
    unmount();
    reject(new Error("late failure"));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});

describe("MonitorTab", () => {
  beforeEach(() => {
    jest.mocked(getAgentTraces).mockResolvedValue([]);
  });

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

  it("renders recent traces on success", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: null,
      detail: "ok",
    });
    jest.mocked(getAgentTraces).mockResolvedValue([
      { id: "trace-1", started_at: "2026-07-01T10:00:00Z", status: "success", summary: "Ran fine." },
      { id: "trace-2", started_at: "2026-07-02T10:00:00Z", status: "error", summary: "Timed out." },
    ]);
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    await waitFor(() => expect(screen.getByText("Ran fine.")).toBeInTheDocument());
    expect(screen.getByText("Timed out.")).toBeInTheDocument();
  });

  it("shows an empty state when there are no traces", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: null,
      detail: "ok",
    });
    jest.mocked(getAgentTraces).mockResolvedValue([]);
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    await waitFor(() =>
      expect(screen.getByText("No traces available yet")).toBeInTheDocument(),
    );
  });

  it("shows an empty traces state when the traces fetch fails", async () => {
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: null,
      detail: "ok",
    });
    jest.mocked(getAgentTraces).mockRejectedValue(new ApiError("nope", 404));
    render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    await waitFor(() =>
      expect(screen.getByText("No traces available yet")).toBeInTheDocument(),
    );
  });

  it("does not update trace state after unmount", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: null,
      detail: "ok",
    });
    const { promise, resolve } = deferred<never[]>();
    jest.mocked(getAgentTraces).mockReturnValue(promise);
    const { unmount } = render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    unmount();
    resolve([]);
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update trace state after unmount when the traces request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: null,
      detail: "ok",
    });
    const { promise, reject } = deferred<never[]>();
    jest.mocked(getAgentTraces).mockReturnValue(promise);
    const { unmount } = render(
      <MonitorTab agentId="literature" data={workspaceData()} capability="literature" />,
    );
    unmount();
    reject(new ApiError("nope", 404));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});

describe("VersionsTab", () => {
  beforeEach(() => {
    jest.mocked(getAgentDraft).mockReturnValue(new Promise(() => {}));
  });

  it("shows platform copy while loading, and 'not available yet' for the draft status", () => {
    jest.mocked(getAgentReleases).mockReturnValue(new Promise(() => {}));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    expect(
      screen.getByText(/Every release below is immutable/),
    ).toBeInTheDocument();
    expect(screen.getByText("Loading version history…")).toBeInTheDocument();
    expect(screen.getByText("not available yet")).toBeInTheDocument();
  });

  it("shows researcher copy", () => {
    jest.mocked(getAgentReleases).mockReturnValue(new Promise(() => {}));
    render(<VersionsTab agentId="custom-agent" ownerKind="researcher" />);
    expect(
      screen.getByText(/Forked and researcher-built agents keep their own immutable/),
    ).toBeInTheDocument();
  });

  it("renders the release list and separate draft status on success", async () => {
    jest.mocked(getAgentReleases).mockResolvedValue([
      {
        version_summary: {
          version: "1.2.0",
          created_at: "2026-06-01T00:00:00Z",
          created_by: "platform-team",
          changelog: "Improved citation coverage.",
          derived_from: "1.1.0",
          content_hash: "sha256:abcdef1234567890",
          model_version: "gpt-4o-2026-05-01",
          capability_versions: { "web-search": "3.2.0" },
        },
        deployment: { deployment_status: "deployed" },
      },
      {
        version_summary: {
          version: "1.0.0",
          created_at: "2026-01-01T00:00:00Z",
          created_by: "platform-team",
          changelog: "Initial release.",
          derived_from: null,
          content_hash: "sha256:0011223344556677",
          model_version: "gpt-4o-2026-01-01",
          capability_versions: {},
        },
        deployment: { deployment_status: "rolled_back" },
      },
    ]);
    jest.mocked(getAgentDraft).mockResolvedValue(draftView({ status: "evaluating" }));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);

    await waitFor(() => expect(screen.getByText("1.2.0")).toBeInTheDocument());
    expect(screen.getByText("Improved citation coverage.")).toBeInTheDocument();
    expect(screen.getAllByText(/platform-team/).length).toBe(2);
    expect(screen.getByText(/Forked from 1\.1\.0/)).toBeInTheDocument();
    expect(screen.getByText(/Original release/)).toBeInTheDocument();
    expect(screen.getByText("deployed")).toBeInTheDocument();
    expect(screen.getByText("rolled back")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("evaluating")).toBeInTheDocument());
  });

  it("shows an empty state when there is no release history", async () => {
    jest.mocked(getAgentReleases).mockResolvedValue([]);
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    await waitFor(() =>
      expect(screen.getByText("Immutable baseline only")).toBeInTheDocument(),
    );
  });

  it("falls back to 'not available yet' draft status when the draft fetch fails", async () => {
    jest.mocked(getAgentReleases).mockResolvedValue([]);
    jest.mocked(getAgentDraft).mockRejectedValue(new ApiError("nope", 404));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    await waitFor(() => expect(screen.getByText("not available yet")).toBeInTheDocument());
  });

  it("renders a classified async-state banner on failure", async () => {
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("nope", 401));
    render(<VersionsTab agentId="literature" ownerKind="platform" />);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveAttribute("data-tone", "unauthorized"),
    );
  });

  it("does not update state after unmount when the releases request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, resolve } = deferred<[]>();
    jest.mocked(getAgentReleases).mockReturnValue(promise);
    const { unmount } = render(<VersionsTab agentId="literature" ownerKind="platform" />);
    unmount();
    resolve([]);
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update state after unmount when the releases request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentReleases).mockReturnValue(promise);
    const { unmount } = render(<VersionsTab agentId="literature" ownerKind="platform" />);
    unmount();
    reject(new Error("late failure"));
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update the draft-status state after unmount when the draft request resolves late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    jest.mocked(getAgentReleases).mockResolvedValue([]);
    const { promise, resolve } = deferred<AgentDraftView>();
    jest.mocked(getAgentDraft).mockReturnValue(promise);
    const { unmount } = render(<VersionsTab agentId="literature" ownerKind="platform" />);
    unmount();
    resolve(draftView());
    await flush();
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("does not update the draft-status state after unmount when the draft request rejects late", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    jest.mocked(getAgentReleases).mockResolvedValue([]);
    const { promise, reject } = deferred<never>();
    jest.mocked(getAgentDraft).mockReturnValue(promise);
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
    jest.mocked(getAgentTraces).mockReturnValue(new Promise(() => {}));
    jest.mocked(getAgentDeployment).mockReturnValue(new Promise(() => {}));
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentRelease).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentDraft).mockResolvedValue(draftView());
  });

  it("shows a loading state while workspace data has not arrived", () => {
    render(
      <AgentWorkspaceView
        agentId="literature"
        data={null}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    expect(screen.getByText("Loading agent workspace…")).toBeInTheDocument();
  });

  it("resets the contract to loading and refetches when the agentId prop changes without unmounting", async () => {
    jest.mocked(getAgentReleases).mockImplementation((agentId: string) =>
      agentId === "literature"
        ? Promise.reject(new ApiError("no releases yet", 404))
        : Promise.resolve([releaseSummary({ version: "2.0.0" })]),
    );
    jest.mocked(getAgentRelease).mockImplementation((agentId: string) =>
      agentId === "coordinator"
        ? Promise.resolve({
            release: releaseSummary({ version: "2.0.0" }),
            contract: { ...emptyContract(), purpose: "Coordinates specialist agents." },
          })
        : Promise.reject(new ApiError("no releases yet", 404)),
    );
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
        {
          id: "coordinator",
          name: "coordinator-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { rerender } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Behavioral contract")).toBeInTheDocument(),
    );

    rerender(
      <AgentWorkspaceView
        agentId="coordinator"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getAllByText("Coordinates specialist agents.").length,
      ).toBeGreaterThan(0),
    );
  });

  it("shows a not-found empty state and calls onBack for an unknown agent id", async () => {
    const user = userEvent.setup();
    const onBack = jest.fn();
    render(
      <AgentWorkspaceView
        agentId="unknown-agent"
        data={workspaceData({ agents: [] })}
        onRefresh={jest.fn()}
        onBack={onBack}
      />,
    );
    expect(screen.getByText("Agent not found")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Back to registry/ }));
    expect(onBack).toHaveBeenCalled();
  });

  it("renders researcher-owned copy for an agent id outside the structural system index", async () => {
    const data = workspaceData({
      agents: [
        {
          id: "custom-agent",
          name: "custom-agent",
          deployment: "",
          model_tier: "fast",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    render(
      <AgentWorkspaceView
        agentId="custom-agent"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    expect(screen.getByText("Researcher-owned")).toBeInTheDocument();
  });

  it("renders 'Not available yet' fallbacks throughout the contract when the draft has no data", async () => {
    const data = workspaceData({
      agents: [
        {
          id: "coordinator",
          name: "coordinator-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    render(
      <AgentWorkspaceView
        agentId="coordinator"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const contract = screen.getByLabelText("Behavioral contract");
    await waitFor(() =>
      expect(within(contract).getAllByText("Not available yet.").length).toBeGreaterThan(0),
    );
    expect(within(contract).getByText("None attached yet.")).toBeInTheDocument();
    expect(within(contract).getByText(/Not discovered yet/)).toBeInTheDocument();
  });

  it("renders an unresolved specialist id as a literal fallback when the specialist has no display name", async () => {
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          specialists: [
            {
              id: "missing-specialist-id",
              name: null,
              owner_kind: null,
              purpose: null,
              attached: false,
            },
          ],
        },
      }),
    );
    const data = workspaceData({
      agents: [
        {
          id: "custom-agent",
          name: "custom-agent",
          deployment: "",
          model_tier: "fast",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    render(
      <AgentWorkspaceView
        agentId="custom-agent"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    const contract = screen.getByLabelText("Behavioral contract");
    await waitFor(() =>
      expect(within(contract).getByText(/missing-specialist-id/)).toBeInTheDocument(),
    );
    expect(within(contract).getByText(/not attached/)).toBeInTheDocument();
  });

  it("renders connections, capabilities, live model/status, and the read-only public web boundary for a discovered release", async () => {
    jest.mocked(getAgentReleases).mockResolvedValue([
      {
        version_summary: {
          version: "1.0.0",
          created_at: "2026-01-01T00:00:00Z",
          created_by: "platform",
          changelog: "Initial release.",
          derived_from: null,
          content_hash: "sha256:0000000000000000",
          model_version: "gpt-5-fast",
          capability_versions: {},
        },
        deployment: { deployment_status: "deployed" },
      },
    ]);
    jest.mocked(getAgentRelease).mockResolvedValue({
      release: {
        version_summary: {
          version: "1.0.0",
          created_at: "2026-01-01T00:00:00Z",
          created_by: "platform",
          changelog: "Initial release.",
          derived_from: null,
          content_hash: "sha256:0000000000000000",
          model_version: "gpt-5-fast",
          capability_versions: {},
        },
        deployment: { deployment_status: "deployed" },
      },
      contract: {
        ...emptyContract(),
        deployment: null,
        connections: [
          {
            id: "pubmed",
            name: "PubMed",
            readiness: "ready",
            permissions: ["read"],
            scope: "workspace",
            policy: null,
            version: "3.2.0",
          },
        ],
        capabilities: [
          {
            descriptor_id: "web-search",
            descriptor_version: "1.0.0",
            descriptor_digest: "sha256:descriptor1",
            operation: "search",
            instance_id: "web-search-instance-1",
            instance_fingerprint: "sha256:instance1",
            pinned_provider_version: "2024-06-01",
            input_schema_digest: "sha256:input1",
            output_schema_digest: "sha256:output1",
            config: {},
            config_hash: "sha256:config1",
            connection_ref: "conn-bing",
            policy_ref: null,
            attached_by: "researcher@example.com",
            attached_at: "2026-01-01T00:00:00Z",
          },
        ],
        public_boundary: {
          mode: "public_online",
          sources: null,
          outbound_data_boundary: "Public metadata only, no raw data.",
          write_destinations: null,
          approval_required: true,
        },
      },
    });
    jest.mocked(getCapabilityDiscovery).mockResolvedValue({
      descriptors: [
        {
          id: "web-search",
          version: "1.0.0",
          provider: "bing",
          title: "web",
          description: "Search the public web.",
          operations: [
            {
              name: "search",
              maturity: "ga",
              lifecycle: "active",
              operation_class: "read",
              side_effect_destinations: ["bing.com"],
              requires_approval: false,
              reason: null,
              source_url: null,
              source_version: null,
              last_verified_at: null,
              input_schema_digest: "sha256:input1",
              output_schema_digest: "sha256:output1",
            },
          ],
          auth_requirements: [],
          risk_tier: "low",
          data_boundary: "project",
          managed_foundry_native: false,
        },
      ],
      instances: [
        {
          id: "web-search-instance-1",
          tenant_id: "tenant-demo",
          project_id: "project-demo",
          descriptor_id: "web-search",
          descriptor_version: "1.0.0",
          discovered_provider_version: "3.2.0",
          readiness: "ready",
          health_status: "healthy",
          config_fingerprint: "fp-web-search-1",
          instance_fingerprint: "sha256:instance1",
          unavailable_reason: null,
          discovered_at: "2026-01-01T00:00:00Z",
          registered_by: "platform",
        },
      ],
      warnings: [],
      refreshed_at: "2026-01-01T00:00:00Z",
    });
    const data = workspaceData({
      agents: [
        {
          id: "literature_online",
          name: "literature-online-agent",
          deployment: "gpt-5-fast",
          model_tier: "fast",
          status: "Active",
          web_access: "some public description",
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
    await waitFor(() =>
      expect(within(contract).getByText("PubMed")).toBeInTheDocument(),
    );
    expect(
      within(contract).getByText(
        "gpt-5-fast (discovered from project deployments)",
      ),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/Every result is treated as untrusted data/),
    ).toBeInTheDocument();
    expect(within(contract).getByText(/Approval required for writes/)).toBeInTheDocument();
    expect(within(contract).getByText("web") ).toBeInTheDocument();
    expect(within(contract).getAllByText(/search/).length).toBeGreaterThan(0);
    expect(
      within(contract).getByText(/instance web-search-instance-1 \(ready\)/),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/provider contract v2024-06-01/),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/Destinations: bing\.com/),
    ).toBeInTheDocument();
    expect(within(contract).getByText("Active")).toBeInTheDocument();
    expect(
      screen.getByText(/Showing the immutable release/),
    ).toBeInTheDocument();
    expect(screen.getByText("1.0.0", { exact: false })).toBeInTheDocument();
  });

  it("shows a draft-based note and a genuine error banner when the contract fetch fails entirely", async () => {
    jest.mocked(getAgentDraft).mockRejectedValue(new ApiError("no draft either", 404));
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    const contract = screen.getByLabelText("Behavioral contract");
    await waitFor(() =>
      expect(
        within(contract).getByRole("status"),
      ).toHaveAttribute("data-tone", "unavailable"),
    );
    expect(within(contract).getByText("Not available yet")).toBeInTheDocument();
  });

  it("shows the mutable-draft note when no release exists but the draft loads", async () => {
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await waitFor(() =>
      expect(
        screen.getByText(/No published release yet — showing the current mutable draft\./),
      ).toBeInTheDocument(),
    );
  });

  it("falls back to the draft when the release list resolves empty rather than rejecting", async () => {
    jest.mocked(getAgentReleases).mockResolvedValue([]);
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await waitFor(() =>
      expect(
        screen.getByText(/No published release yet — showing the current mutable draft\./),
      ).toBeInTheDocument(),
    );
  });

  it("ignores a release-contract resolution that arrives after the workspace view unmounts", async () => {
    const releaseCall = deferred<{
      release: AgentReleaseSummary;
      contract: AgentContractView;
    }>();
    jest.mocked(getAgentReleases).mockResolvedValue([releaseSummary()]);
    jest.mocked(getAgentRelease).mockReturnValue(releaseCall.promise);
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "gpt-5-fast",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { unmount } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    unmount();
    releaseCall.resolve({ release: releaseSummary(), contract: emptyContract() });
    await flush();
    // No assertion beyond not throwing: the effect's cleanup must have set
    // `cancelled = true` so the post-unmount `setContract`/`setStatus` calls
    // are skipped rather than triggering a React state update on an
    // unmounted component.
  });

  it("ignores a draft-fallback resolution that arrives after the workspace view unmounts", async () => {
    const draftCall = deferred<AgentDraftView>();
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentDraft).mockReturnValue(draftCall.promise);
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { unmount } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    unmount();
    draftCall.resolve(draftView());
    await flush();
  });

  it("ignores a final release-and-draft failure that arrives after the workspace view unmounts", async () => {
    const draftCall = deferred<AgentDraftView>();
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentDraft).mockReturnValue(draftCall.promise);
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { unmount } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    unmount();
    draftCall.reject(new ApiError("no draft either", 404));
    await flush();
  });

  it("ignores a final capability discovery failure that arrives after the workspace view unmounts", async () => {
    const discoveryCall = deferred<Awaited<ReturnType<typeof getCapabilityDiscovery>>>();
    jest.mocked(getCapabilityDiscovery).mockReturnValue(discoveryCall.promise);
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { unmount } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    unmount();
    discoveryCall.reject(new ApiError("capability discovery unavailable", 503));
    await flush();
  });

  it("renders populated knowledge/tools, alternate connection/specialist/capability shapes, and the deepest model/deployment/tier fallbacks", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          knowledge: ["Indexed PubMed abstracts (2015–present)"],
          tools: ["citation_lookup"],
          connections: [
            {
              id: "workspace-drive",
              name: "Workspace Drive",
              readiness: "ready",
              permissions: [],
              scope: "workspace",
              policy: null,
              version: null,
            },
          ],
          specialists: [
            {
              id: "stats-reviewer",
              name: "Stats reviewer",
              owner_kind: "researcher",
              purpose: null,
              attached: true,
            },
          ],
          capabilities: [
            {
              descriptor_id: "analysis-summarize",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "summarize",
              instance_id: "analysis-summarize-instance",
              instance_fingerprint: null,
              pinned_provider_version: null,
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
            {
              descriptor_id: "unresolved-descriptor-op",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "unresolved-operation",
              instance_id: "unresolved-instance",
              instance_fingerprint: null,
              pinned_provider_version: null,
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
            {
              descriptor_id: "analysis-classify",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "classify",
              instance_id: "analysis-classify-instance",
              instance_fingerprint: "sha256:classify-instance-1",
              pinned_provider_version: "2.1",
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
            {
              descriptor_id: "analysis-legacy-extract",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "extract",
              instance_id: "analysis-legacy-extract-instance",
              instance_fingerprint: null,
              pinned_provider_version: null,
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
            {
              descriptor_id: "analysis-quick-scan",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "quick_scan",
              instance_id: null,
              instance_fingerprint: null,
              pinned_provider_version: null,
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
          ],
        },
      }),
    );
    // `analysis-summarize`'s descriptor resolves but its instance doesn't
    // (simulating a removed/unavailable discovered resource); the
    // `unresolved-descriptor-op` binding's descriptor never resolves at all
    // (neither its descriptor nor its instance appear below); the other two
    // bindings resolve fully against a matching descriptor+operation+instance
    // — both `ga` maturity but on the independent `lifecycle` axis: one
    // `retired` (with a reason) requiring approval, one `deprecated`
    // (no reason) — demonstrating operation `maturity` and `lifecycle` are
    // two separate fields (verified against the backend's real
    // `OperationMaturity`/`OperationLifecycle` enums, commit `5dab8b7`), so a
    // `ga` operation can still be permanently non-attachable via lifecycle
    // alone.
    jest.mocked(getCapabilityDiscovery).mockResolvedValue({
      descriptors: [
        {
          id: "analysis-summarize",
          version: "1.0.0",
          provider: "internal",
          title: "analysis",
          description: "Summarize retrieved evidence.",
          operations: [
            {
              name: "summarize",
              maturity: "ga",
              lifecycle: "active",
              operation_class: "read",
              side_effect_destinations: [],
              requires_approval: false,
              reason: null,
              source_url: null,
              source_version: null,
              last_verified_at: null,
              input_schema_digest: null,
              output_schema_digest: null,
            },
          ],
          auth_requirements: [],
          risk_tier: "low",
          data_boundary: "project",
          managed_foundry_native: false,
        },
        {
          id: "analysis-classify",
          version: "1.0.0",
          provider: "internal",
          title: "analysis",
          description: "Classify retrieved evidence.",
          operations: [
            {
              name: "classify",
              maturity: "ga",
              lifecycle: "retired",
              operation_class: "read",
              side_effect_destinations: [],
              requires_approval: true,
              reason: "Superseded by analysis-summarize v2; sunset 2026-12-01.",
              source_url: null,
              source_version: null,
              last_verified_at: null,
              input_schema_digest: null,
              output_schema_digest: null,
            },
          ],
          auth_requirements: [],
          risk_tier: "low",
          data_boundary: "project",
          managed_foundry_native: false,
        },
        {
          id: "analysis-legacy-extract",
          version: "1.0.0",
          provider: "internal",
          title: "analysis",
          description: "Legacy structured extraction.",
          operations: [
            {
              name: "extract",
              maturity: "ga",
              lifecycle: "deprecated",
              operation_class: "read",
              side_effect_destinations: ["internal-store"],
              requires_approval: false,
              reason: null,
              source_url: null,
              source_version: null,
              last_verified_at: null,
              input_schema_digest: null,
              output_schema_digest: null,
            },
          ],
          auth_requirements: [],
          risk_tier: "low",
          data_boundary: "project",
          managed_foundry_native: false,
        },
        {
          id: "analysis-quick-scan",
          version: "1.0.0",
          provider: "internal",
          title: "Quick scan",
          description: "Stateless scan needing no discovered instance.",
          operations: [
            {
              name: "quick_scan",
              maturity: "ga",
              lifecycle: "active",
              operation_class: "read",
              side_effect_destinations: [],
              requires_approval: false,
              reason: null,
              source_url: null,
              source_version: null,
              last_verified_at: null,
              input_schema_digest: null,
              output_schema_digest: null,
            },
          ],
          auth_requirements: [],
          risk_tier: "low",
          data_boundary: "project",
          managed_foundry_native: false,
        },
      ],
      instances: [
        {
          id: "analysis-classify-instance",
          tenant_id: "tenant-demo",
          project_id: "project-demo",
          descriptor_id: "analysis-classify",
          descriptor_version: "1.0.0",
          discovered_provider_version: "2.1.0",
          readiness: "ready",
          health_status: "healthy",
          config_fingerprint: "fp-classify-1",
          instance_fingerprint: "sha256:classify-instance-1",
          unavailable_reason: null,
          discovered_at: "2026-01-01T00:00:00Z",
          registered_by: "platform",
        },
        {
          id: "analysis-legacy-extract-instance",
          tenant_id: "tenant-demo",
          project_id: "project-demo",
          descriptor_id: "analysis-legacy-extract",
          descriptor_version: "1.0.0",
          discovered_provider_version: "1.0.0",
          readiness: "unavailable",
          health_status: "unhealthy",
          config_fingerprint: "fp-legacy-extract-1",
          instance_fingerprint: null,
          unavailable_reason: "Provider quota exhausted.",
          discovered_at: "2026-01-01T00:00:00Z",
          registered_by: "platform",
        },
      ],
      warnings: [],
      refreshed_at: "2026-01-01T00:00:00Z",
    });
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "",
          status: "",
          web_access: "",
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
    const contract = screen.getByLabelText("Behavioral contract");
    await waitFor(() =>
      expect(
        within(contract).getByText("Indexed PubMed abstracts (2015–present)"),
      ).toBeInTheDocument(),
    );
    expect(within(contract).getByText("citation_lookup")).toBeInTheDocument();
    expect(within(contract).getByText("Workspace Drive")).toBeInTheDocument();
    expect(
      within(contract).getByText(/no permissions.*· workspace$/),
    ).toBeInTheDocument();
    expect(within(contract).getByText(/Stats reviewer \(researcher\)/)).toBeInTheDocument();
    expect(within(contract).getByText(/— attached/)).toBeInTheDocument();
    expect(within(contract).getAllByText("unknown").length).toBe(2);
    expect(within(contract).getByText(/Requires approval/)).toBeInTheDocument();
    expect(
      within(contract).getAllByText(/No approval required/).length,
    ).toBeGreaterThan(0);
    expect(
      within(contract).getByText(/provider contract v2\.1/),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/Destinations: internal-store/),
    ).toBeInTheDocument();
    // Descriptor unresolved: falls back to the pinned binding's raw descriptor_id/operation.
    expect(within(contract).getByText("unresolved-descriptor-op")).toBeInTheDocument();
    expect(
      within(contract).getByText(/unresolved-operation/),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(
        /Stale: This binding's capability descriptor is no longer resolvable/,
      ),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(
        /Stale: This binding's discovered instance is no longer resolvable/,
      ),
    ).toBeInTheDocument();
    // GA operation with no pinned instance (`instance_id: null`): attachable
    // on maturity+lifecycle alone, and renders with no "· instance …"
    // fragment at all.
    const quickScanItem = within(contract)
      .getByText(/quick_scan/)
      .closest("li");
    expect(quickScanItem).toHaveAttribute("data-attachable", "true");
    expect(quickScanItem).toHaveAttribute("data-stale", "false");
    expect(quickScanItem?.textContent).not.toMatch(/· instance/);
    expect(within(contract).getByText("Quick scan")).toBeInTheDocument();
    // `maturity` and `lifecycle` are two independent fields on the operation
    // (verified against the backend's real `OperationMaturity`/
    // `OperationLifecycle` enums, commit `5dab8b7`): a `ga`-maturity
    // operation can still be permanently non-attachable via a `retired` (with
    // a surfaced reason) or `deprecated` (no reason provided) lifecycle.
    expect(within(contract).getAllByText("ga").length).toBeGreaterThanOrEqual(2);
    expect(within(contract).getByText("retired")).toBeInTheDocument();
    expect(within(contract).getByText("deprecated")).toBeInTheDocument();
    expect(
      within(contract).getByText(
        /Retired: Superseded by analysis-summarize v2; sunset 2026-12-01\./,
      ),
    ).toBeInTheDocument();
    expect(
      within(contract).getByText(/Deprecated — no reason provided\./),
    ).toBeInTheDocument();
    expect(
      within(contract).getAllByText("Not available yet.").length,
    ).toBeGreaterThan(0);
    expect(
      within(contract).getAllByText(/Not discovered yet/).length,
    ).toBeGreaterThanOrEqual(2);

    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    expect(screen.getByText(/pinned to the undiscovered tier/)).toBeInTheDocument();
  });

  it("shows an explicit unavailable note (not a crash or silent gap) when capability discovery fails, while still rendering the pinned bindings", async () => {
    jest.mocked(getAgentReleases).mockRejectedValue(new ApiError("no releases yet", 404));
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          capabilities: [
            {
              descriptor_id: "web-search",
              descriptor_version: "1.0.0",
              descriptor_digest: null,
              operation: "search",
              instance_id: "web-search-instance-1",
              instance_fingerprint: null,
              pinned_provider_version: null,
              input_schema_digest: null,
              output_schema_digest: null,
              config: {},
              config_hash: null,
              connection_ref: null,
              policy_ref: null,
              attached_by: "researcher@example.com",
              attached_at: "2026-01-01T00:00:00Z",
            },
          ],
        },
      }),
    );
    jest.mocked(getCapabilityDiscovery).mockRejectedValue(
      new ApiError("capability discovery unavailable", 503),
    );
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "",
          status: "",
          web_access: "",
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
    const contract = screen.getByLabelText("Behavioral contract");
    await waitFor(() =>
      expect(
        within(contract).getByText(
          /Live descriptor\/instance enrichment unavailable/,
        ),
      ).toBeInTheDocument(),
    );
    // The binding itself still renders — degraded, not hidden — falling back
    // to its own pinned descriptor/operation ids since nothing resolved.
    expect(within(contract).getByText("web-search")).toBeInTheDocument();
    expect(within(contract).getAllByText(/search/).length).toBeGreaterThan(0);
  });

  it("expands Advanced to reveal schema, runtime, identity, and the specialist hint including the live deployment id", async () => {
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
    expect(screen.getByText(/pinned to the primary tier/)).toBeInTheDocument();
    expect(screen.getByText("literature")).toBeInTheDocument();
    expect(screen.getByText("gpt-5-primary")).toBeInTheDocument();
    expect(screen.getByText(/Attach a specialist/)).toBeInTheDocument();
    expect(screen.getByText(/describe it in Build/)).toBeInTheDocument();
  });

  it("switches tabs and renders the corresponding tab panel", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentDeployment).mockResolvedValue({ status: "not_deployed", version: null });
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await user.click(screen.getByRole("tab", { name: "Deploy" }));
    await waitFor(() =>
      expect(screen.getByText(/Only platform owners publish new versions/)).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("tab", { name: "Versions" }));
    expect(screen.getByText(/Every release below is immutable/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Test" }));
    expect(screen.getByTestId("studio-stub")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Evaluate" }));
    expect(screen.getByLabelText("Advisory evaluation")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Monitor" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Health and usage")).toBeInTheDocument(),
    );
  });

  it("renders per-scope memory controls and requires an explicit confirmation dialog before forgetting a scope", async () => {
    jest.mocked(forgetAgentMemoryScope).mockResolvedValue({
      scope: "conversation",
      enabled: false,
      default_enabled: false,
      retention_days: null,
      provider: null,
      access: "Not configured",
    });
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          memory: {
            scopes: [
              {
                scope: "conversation",
                enabled: true,
                default_enabled: false,
                retention_days: 30,
                provider: "workspace-store",
                access: "Read/write by this agent only",
              },
            ],
          },
        },
      }),
    );
    const user = userEvent.setup();
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await waitFor(() => expect(screen.getByText(/30-day retention/)).toBeInTheDocument());
    expect(screen.getByText("conversation")).toBeInTheDocument();

    // Clicking Forget must never call the API directly — it opens a
    // confirmation dialog explaining the scope/retention and that the
    // deletion is irreversible.
    await user.click(screen.getByRole("button", { name: "Forget" }));
    expect(forgetAgentMemoryScope).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: /Forget conversation memory\?/ });
    expect(within(dialog).getByText(/cannot be undone/)).toBeInTheDocument();
    expect(within(dialog).getByText(/retained for 30 days/)).toBeInTheDocument();

    // Cancel must close the dialog without ever calling the API.
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(forgetAgentMemoryScope).not.toHaveBeenCalled();

    // Confirming calls the API exactly once and shows an audited outcome.
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await user.click(
      screen.getByRole("button", { name: "Forget permanently" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Forget requested for conversation memory\..*recorded to the audit log/),
      ).toBeInTheDocument(),
    );
    expect(forgetAgentMemoryScope).toHaveBeenCalledTimes(1);
    expect(forgetAgentMemoryScope).toHaveBeenCalledWith("literature", "conversation");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Non-color-only: success is also named in visible text, not just green.
    expect(screen.getByText("Success")).toBeInTheDocument();
  });

  it("closing the confirmation dialog via the backdrop never calls the Forget API", async () => {
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          memory: {
            scopes: [
              {
                scope: "conversation",
                enabled: true,
                default_enabled: false,
                retention_days: 30,
                provider: "workspace-store",
                access: "Read/write by this agent only",
              },
            ],
          },
        },
      }),
    );
    const user = userEvent.setup();
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await waitFor(() => expect(screen.getByText(/30-day retention/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Forget" }));
    const backdrop = screen.getByRole("presentation");
    await user.click(backdrop);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(forgetAgentMemoryScope).not.toHaveBeenCalled();

    // The dialog's own close (X) button is an equally valid, non-mutating
    // dismissal path, distinct from the backdrop and the Cancel button.
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await user.click(screen.getByRole("button", { name: "Cancel forget" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(forgetAgentMemoryScope).not.toHaveBeenCalled();
  });

  it("shows a classified error message after confirming a memory forget that fails", async () => {
    jest.mocked(forgetAgentMemoryScope).mockRejectedValue(new ApiError("nope", 404));
    jest.mocked(getAgentDraft).mockResolvedValue(
      draftView({
        contract: {
          ...emptyContract(),
          memory: {
            scopes: [
              {
                scope: "user",
                enabled: false,
                default_enabled: false,
                retention_days: null,
                provider: null,
                access: "Not configured",
              },
            ],
          },
        },
      }),
    );
    const user = userEvent.setup();
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
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
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Forget" })).toHaveLength(1),
    );
    expect(screen.getByText("user")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Forget" }));
    await user.click(
      screen.getByRole("button", { name: "Forget permanently" }),
    );
    const errorMessage = await screen.findByText(
      /This feature's backend endpoint isn't implemented/,
    );
    expect(errorMessage).toHaveAttribute("role", "alert");
    expect(errorMessage).toHaveAttribute("data-tone", "unavailable");
    // Non-color-only: the tone is also named in visible text.
    expect(within(errorMessage).getByText("Not available yet")).toBeInTheDocument();
  });

  it("has no detectable accessibility violations", async () => {
    const data = workspaceData({
      agents: [
        {
          id: "literature",
          name: "literature-agent",
          deployment: "",
          model_tier: "primary",
          status: "Active",
          web_access: "none",
          workflow_steps: [],
        },
      ],
    });
    const { container } = render(
      <AgentWorkspaceView
        agentId="literature"
        data={data}
        onRefresh={jest.fn()}
        onBack={jest.fn()}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
