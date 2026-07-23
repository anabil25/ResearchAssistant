import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import {
  AgentRegistryCard,
  AgentRegistryView,
  capabilityTitle,
  lifecycleFromStatus,
} from "@/components/agent-registry";
import type { AgentCatalogEntry } from "@/lib/agent-catalog";
import {
  createAgentDraft,
  forkAgent,
  getAgentEvaluation,
  getAgentHealth,
  getAgentVersions,
  ApiError,
  type WorkspaceData,
} from "@/lib/api";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ApiError: actual.ApiError,
    createAgentDraft: jest.fn(),
    forkAgent: jest.fn(),
    getAgentEvaluation: jest.fn(),
    getAgentHealth: jest.fn(),
    getAgentVersions: jest.fn(),
  };
});

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

describe("pure helpers", () => {
  it("capabilityTitle returns null for a null capability and a label otherwise", () => {
    expect(capabilityTitle(null)).toBeNull();
    expect(capabilityTitle("literature")).toBe("Literature Studio");
  });

  it("capabilityTitle falls back to the raw id when no capability card matches", () => {
    expect(capabilityTitle("unknown_capability" as never)).toBe(
      "unknown_capability",
    );
  });

  it("lifecycleFromStatus normalizes status text", () => {
    expect(lifecycleFromStatus(undefined)).toBe("draft");
    expect(lifecycleFromStatus("Active")).toBe("active");
    expect(lifecycleFromStatus("Deprecated")).toBe("deprecated");
  });
});

const literatureEntry: AgentCatalogEntry = {
  id: "literature",
  name: "literature-agent",
  ownerKind: "platform",
  purpose: "Produces skeptical, source-grounded literature comparisons.",
  boundary: "Analyzes only server-authorized evidence.",
  knowledge: ["paper"],
  tools: [],
  modelTier: "primary",
  outputContract: "LiteratureSynthesisV2",
  workflowSteps: ["protocol", "search"],
  publicWebBoundary: "none",
  connectorSources: [],
  capability: "literature",
  specialists: [],
};

describe("AgentRegistryCard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("renders a researcher-owned agent without a fork button and with no capability chip", () => {
    const researcherEntry: AgentCatalogEntry = {
      ...literatureEntry,
      id: "custom-agent",
      name: "custom-agent",
      ownerKind: "researcher",
      capability: null,
    };
    render(
      <AgentRegistryCard
        entry={researcherEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    expect(screen.getByText("Researcher-owned")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Fork for my workspace/ }),
    ).not.toBeInTheDocument();
  });

  it("shows live deployment facts and singular run/workflow counts when data is present", () => {
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
      runs: [
        {
          id: "run-1",
          durable_instance_id: "inst-1",
          project_id: "demo-project",
          capability: "literature",
          title: "Run 1",
          status: "completed",
          progress: 100,
          started_at: "2026-07-16T12:00:00Z",
          completed_at: "2026-07-16T12:05:00Z",
        },
      ] as unknown as WorkspaceData["runs"],
      workflows: [
        {
          id: "wf-1",
          capability: "literature",
          name: "Weekly literature scan",
          trigger: "schedule",
          steps: [],
        },
      ] as unknown as WorkspaceData["workflows"],
    });
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={data}
        onOpenAgent={jest.fn()}
      />,
    );
    expect(screen.getByText("gpt-5-primary")).toBeInTheDocument();
    expect(screen.getByText("1 run")).toBeInTheDocument();
    expect(screen.getByText("1 workflow")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
  });

  it("shows 'Not discovered yet' facts and zero counts when there is no live agent data", () => {
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    expect(screen.getByText("Not discovered yet")).toBeInTheDocument();
    expect(screen.getByText("0 runs")).toBeInTheDocument();
    expect(screen.getByText("0 workflows")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("invokes onOpenAgent when the card body is clicked", async () => {
    const user = userEvent.setup();
    const onOpenAgent = jest.fn();
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={onOpenAgent}
      />,
    );
    await user.click(screen.getByText("literature-agent"));
    expect(onOpenAgent).toHaveBeenCalledWith("literature");
  });

  it("falls back to zero usage counts and no live facts when workspace data is null", () => {
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={null}
        onOpenAgent={jest.fn()}
      />,
    );
    expect(screen.getByText("0 runs")).toBeInTheDocument();
    expect(screen.getByText("0 workflows")).toBeInTheDocument();
    expect(screen.getByText("Not discovered yet")).toBeInTheDocument();
  });

  it("loads live health, evaluation, and versions on disclosure and shows real recorded versions", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "healthy",
      last_checked_at: "2026-07-16T12:00:00Z",
      detail: "All checks passing.",
    });
    jest.mocked(getAgentEvaluation).mockResolvedValue({
      advisory: true,
      citation_resolution: 92,
      claim_entailment: 88,
      retrieval_completeness: 95,
      last_run_at: "2026-07-16T12:00:00Z",
      hard_gates: [],
    });
    jest.mocked(getAgentVersions).mockResolvedValue([
      {
        version: "1.0.0",
        created_at: "2026-07-01T12:00:00Z",
        created_by: "platform",
        status: "active",
        changelog: "Initial release.",
      },
    ]);
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: /Live evaluation, health & versions/ }),
    );
    await waitFor(() => expect(screen.getByText("healthy")).toBeInTheDocument());
    expect(screen.getByText("92% citation resolution")).toBeInTheDocument();
    expect(screen.getByText("1 recorded")).toBeInTheDocument();
    expect(getAgentHealth).toHaveBeenCalledTimes(1);

    // Collapse then re-expand: must not refetch once results are cached.
    const toggle = screen.getByRole("button", {
      name: /Live evaluation, health & versions/,
    });
    await user.click(toggle);
    await user.click(toggle);
    expect(getAgentHealth).toHaveBeenCalledTimes(1);
  });

  it("shows the immutable-baseline fallback when versions are empty and partial-success fallbacks for failed calls", async () => {
    const user = userEvent.setup();
    jest.mocked(getAgentHealth).mockResolvedValue({
      state: "unknown",
      last_checked_at: null,
      detail: "Not yet checked.",
    });
    jest.mocked(getAgentEvaluation).mockRejectedValue(new Error("no eval"));
    jest.mocked(getAgentVersions).mockResolvedValue([]);
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: /Live evaluation, health & versions/ }),
    );
    await waitFor(() => expect(screen.getByText("unknown")).toBeInTheDocument());
    expect(screen.getAllByText("Not available yet").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Immutable baseline only — no version history yet"),
    ).toBeInTheDocument();
  });

  it("shows the health fallback and a metric em dash when health fails but evaluation succeeds with a null metric", async () => {
    const user = userEvent.setup();
    jest
      .mocked(getAgentHealth)
      .mockRejectedValue(new ApiError("no health endpoint", 404));
    jest.mocked(getAgentEvaluation).mockResolvedValue({
      advisory: true,
      citation_resolution: null,
      claim_entailment: null,
      retrieval_completeness: null,
      last_run_at: null,
      hard_gates: [],
    });
    jest.mocked(getAgentVersions).mockResolvedValue([]);
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: /Live evaluation, health & versions/ }),
    );
    await waitFor(() =>
      expect(
        screen.getByText("—% citation resolution"),
      ).toBeInTheDocument(),
    );
    // Health specifically failed while evaluation/versions succeeded.
    expect(screen.getAllByText("Not available yet").length).toBeGreaterThan(0);
  });

  it("shows an async-state banner when all three live calls fail", async () => {
    const user = userEvent.setup();
    jest
      .mocked(getAgentHealth)
      .mockRejectedValue(new ApiError("no health endpoint", 404));
    jest
      .mocked(getAgentEvaluation)
      .mockRejectedValue(new ApiError("no eval endpoint", 404));
    jest
      .mocked(getAgentVersions)
      .mockRejectedValue(new ApiError("no versions endpoint", 404));
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    const toggle = screen.getByRole("button", {
      name: /Live evaluation, health & versions/,
    });
    await user.click(toggle);
    await waitFor(() =>
      expect(screen.getByText("Not available yet")).toBeInTheDocument(),
    );
    expect(getAgentHealth).toHaveBeenCalledTimes(1);

    // Collapsing (without re-expanding) must not trigger another fetch.
    await user.click(toggle);
    expect(getAgentHealth).toHaveBeenCalledTimes(1);
  });

  it("forks a platform agent successfully", async () => {
    const user = userEvent.setup();
    jest.mocked(forkAgent).mockResolvedValue({ id: "draft-42" });
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Fork for my workspace" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText("Fork created as draft draft-42."),
      ).toBeInTheDocument(),
    );
  });

  it("shows a classified error message when forking fails", async () => {
    const user = userEvent.setup();
    jest
      .mocked(forkAgent)
      .mockRejectedValue(new ApiError("no fork endpoint", 404));
    render(
      <AgentRegistryCard
        entry={literatureEntry}
        data={workspaceData()}
        onOpenAgent={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Fork for my workspace" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/This feature's backend endpoint isn't implemented/),
      ).toBeInTheDocument(),
    );
  });
});

describe("AgentRegistryView", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("shows a loading state while workspace data has not arrived", () => {
    render(<AgentRegistryView data={null} onOpenAgent={jest.fn()} />);
    expect(screen.getByText("Loading agent registry…")).toBeInTheDocument();
  });

  it("lists all 9 system agents equally and an empty 'Your agents' section", () => {
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    expect(
      screen.getByText(/9 platform-owned Hosted Agent deployments/),
    ).toBeInTheDocument();
    expect(screen.getByText("No custom agents yet")).toBeInTheDocument();
  });

  it("filters agents by search text", async () => {
    const user = userEvent.setup();
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.type(
      screen.getByPlaceholderText("Search agents by name or purpose"),
      "grant",
    );
    expect(screen.getByText("grant-agent")).toBeInTheDocument();
    expect(screen.queryByText("literature-agent")).not.toBeInTheDocument();
  });

  it("shows an empty state when the search matches nothing", async () => {
    const user = userEvent.setup();
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.type(
      screen.getByPlaceholderText("Search agents by name or purpose"),
      "no-such-agent",
    );
    expect(
      screen.getByText("No agents match this filter"),
    ).toBeInTheDocument();
  });

  it("filters agents to none when the researcher owner filter is applied (catalog is all platform-owned)", async () => {
    const user = userEvent.setup();
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.click(screen.getByRole("button", { name: "Researcher" }));
    expect(
      screen.getByText("No agents match this filter"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Platform" }));
    expect(screen.getByText("literature-agent")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All" }));
    expect(screen.getByText("literature-agent")).toBeInTheDocument();
  });

  it("opens and closes the create-agent panel from both entry points and submits a template draft", async () => {
    const user = userEvent.setup();
    jest.mocked(createAgentDraft).mockResolvedValue({ id: "draft-7" });
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);

    const headerNewAgentButton = screen.getAllByRole("button", {
      name: /New agent/,
    })[0];
    await user.click(headerNewAgentButton);
    expect(
      screen.getByText("Start from a task template or a blank intent"),
    ).toBeInTheDocument();

    await user.type(
      screen.getByPlaceholderText(
        "e.g. Summarize weekly IRB submissions and flag missing consent language.",
      ),
      "Only cite recent papers.",
    );
    await user.click(
      screen.getByRole("button", { name: "Create draft agent" }),
    );
    await waitFor(() =>
      expect(createAgentDraft).toHaveBeenCalledWith({
        source: "template",
        template_capability: "literature",
        intent: "Only cite recent papers.",
      }),
    );
    await waitFor(() =>
      expect(
        screen.getByText("Draft agent draft-7 created."),
      ).toBeInTheDocument(),
    );

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(
      screen.queryByText("Start from a task template or a blank intent"),
    ).not.toBeInTheDocument();

    // Reopen via the empty-state "Your agents" action and submit a blank intent.
    const emptyStateNewAgentButton = screen.getAllByRole("button", {
      name: "New agent",
    })[1];
    await user.click(emptyStateNewAgentButton);
    await user.click(
      screen.getByRole("button", { name: "Blank conversational intent" }),
    );
    await user.type(
      screen.getByPlaceholderText(
        "e.g. Summarize weekly IRB submissions and flag missing consent language.",
      ),
      "Track new preprints daily.",
    );
    await user.click(
      screen.getByRole("button", { name: "Create draft agent" }),
    );
    await waitFor(() =>
      expect(createAgentDraft).toHaveBeenCalledWith({
        source: "blank",
        intent: "Track new preprints daily.",
      }),
    );
  });

  it("switches the template capability via the select and toggles Task template explicitly", async () => {
    const user = userEvent.setup();
    jest.mocked(createAgentDraft).mockResolvedValue({ id: "draft-9" });
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.click(screen.getAllByRole("button", { name: /New agent/ })[0]);
    await user.click(screen.getByRole("button", { name: "Task template" }));

    const select = screen.getByRole("combobox");
    await user.selectOptions(select, "grant");
    expect(select).toHaveValue("grant");

    await user.type(
      screen.getByPlaceholderText(
        "e.g. Summarize weekly IRB submissions and flag missing consent language.",
      ),
      "Focus on funded grants.",
    );
    await user.click(
      screen.getByRole("button", { name: "Create draft agent" }),
    );
    await waitFor(() =>
      expect(createAgentDraft).toHaveBeenCalledWith({
        source: "template",
        template_capability: "grant",
        intent: "Focus on funded grants.",
      }),
    );
  });

  it("shows an unavailable state with the preserved intent when draft creation fails", async () => {
    const user = userEvent.setup();
    jest
      .mocked(createAgentDraft)
      .mockRejectedValue(new ApiError("no drafts endpoint", 404));
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.click(
      screen.getAllByRole("button", { name: /New agent/ })[0],
    );
    await user.type(
      screen.getByPlaceholderText(
        "e.g. Summarize weekly IRB submissions and flag missing consent language.",
      ),
      "Draft intent text.",
    );
    await user.click(
      screen.getByRole("button", { name: "Create draft agent" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Agent creation isn't available yet/),
      ).toBeInTheDocument(),
    );
  });

  it("shows the classified message for a non-unavailable draft creation failure", async () => {
    const user = userEvent.setup();
    jest
      .mocked(createAgentDraft)
      .mockRejectedValue(new ApiError("Server exploded", 500));
    render(<AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />);
    await user.click(
      screen.getAllByRole("button", { name: /New agent/ })[0],
    );
    await user.type(
      screen.getByPlaceholderText(
        "e.g. Summarize weekly IRB submissions and flag missing consent language.",
      ),
      "Draft intent text.",
    );
    await user.click(
      screen.getByRole("button", { name: "Create draft agent" }),
    );
    await waitFor(() =>
      expect(screen.getByText("Server exploded")).toBeInTheDocument(),
    );
  });

  it("is free of detectable accessibility violations", async () => {
    const { container } = render(
      <AgentRegistryView data={workspaceData()} onOpenAgent={jest.fn()} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});
