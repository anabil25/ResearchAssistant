import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RunsView, SettingsView } from "@/components/workspace-views";
import { updateConnector } from "@/lib/api";
import type { WorkspaceData } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  decideApproval: jest.fn(),
  testConnector: jest.fn(),
  updateConnector: jest.fn(),
  updateSettings: jest.fn(),
}));

function baseWorkspaceData(
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
      library_items: 0,
      active_runs: 0,
      pending_approvals: 0,
      connector_ready: 1,
      connector_total: 1,
      last_activity_at: "2026-07-16T12:00:00Z",
      persistence: "in-memory demo",
    },
    library: [],
    runs: [],
    approvals: [],
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

describe("SettingsView connector versions", () => {
  it("shows APIM/MCP/Toolbox as a truthful, clearly disabled configuration-required state", async () => {
    const user = userEvent.setup();
    const data = baseWorkspaceData();
    render(<SettingsView data={data} onRefresh={jest.fn()} />);

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));

    expect(screen.getByText("Gateway & tool versions")).toBeInTheDocument();
    const apimCard = screen
      .getByText("Azure API Management (APIM)")
      .closest(".readiness-status-card") as HTMLElement;
    expect(within(apimCard).getByText("Not configured")).toBeInTheDocument();
    expect(
      within(apimCard).getByRole("button", { name: "Promote to default" }),
    ).toBeDisabled();
    expect(
      within(apimCard).getByRole("button", { name: "Roll back" }),
    ).toBeDisabled();

    expect(screen.getByText("MCP tool registry")).toBeInTheDocument();
    expect(screen.getByText("Toolbox")).toBeInTheDocument();
  });

  it("uses truthful connector data for a gateway card when a matching connector is registered", async () => {
    const user = userEvent.setup();
    const data = baseWorkspaceData({
      connectors: [
        {
          id: "internal-apim",
          name: "Internal APIM Gateway",
          category: "Gateway",
          description: "Registered API Management instance.",
          auth_kind: "Managed identity",
          secret_status: "Not required",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: [],
          terms_url: "https://example.com/terms",
          data_boundary: "Internal only.",
          capabilities: ["Routing"],
        },
      ],
    });
    render(<SettingsView data={data} onRefresh={jest.fn()} />);

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));

    const apimCard = screen
      .getByText("Azure API Management (APIM)")
      .closest(".readiness-status-card") as HTMLElement;
    expect(within(apimCard).getByText("ready")).toBeInTheDocument();
    expect(
      within(apimCard).getByRole("button", { name: "Promote to default" }),
    ).toBeDisabled();
  });
});

describe("SettingsView connector manager", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("explains missing gateway setup without reporting a provider outage", async () => {
    const user = userEvent.setup();
    const data = baseWorkspaceData({
      connectors: [
        {
          ...baseWorkspaceData().connectors[0],
          test_status: "configuration_required",
          last_tested_at: "2026-07-16T12:00:00Z",
        },
      ],
    });
    render(<SettingsView data={data} onRefresh={jest.fn()} />);

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));

    expect(
      screen.getByRole("heading", { name: "Connector manager" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Setup required").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/The provider is not down/i),
    ).toBeInTheDocument();
    expect(screen.queryByText("Connection failed")).not.toBeInTheDocument();
  });

  it("saves enablement and specialist assignments from the manager widget", async () => {
    const user = userEvent.setup();
    const connector = {
      ...baseWorkspaceData().connectors[0],
      id: "europe_pmc",
      name: "Europe PMC",
    };
    const data = baseWorkspaceData({ connectors: [connector] });
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    jest.mocked(updateConnector).mockResolvedValue({
      ...connector,
      enabled: false,
      assigned_agents: ["literature", "matching"],
    });
    render(<SettingsView data={data} onRefresh={onRefresh} />);

    await user.click(screen.getByRole("button", { name: /Connectors 1/i }));
    await user.click(
      screen.getByRole("checkbox", { name: "Enable Europe PMC" }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: "Assign matching to Europe PMC",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() =>
      expect(updateConnector).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "europe_pmc",
          enabled: false,
          assigned_agents: ["literature", "matching"],
        }),
      ),
    );
    expect(onRefresh).toHaveBeenCalled();
  });
});

describe("RunsView run focus", () => {
  const runsData = baseWorkspaceData({
    runs: [
      {
        id: "run-1",
        durable_instance_id: "research-run-1",
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
      },
      {
        id: "run-2",
        durable_instance_id: "research-run-2",
        project_id: "demo-project",
        capability: "literature",
        title: "Literature synthesis",
        status: "completed",
        progress: 100,
        current_stage: "Complete",
        owner: "Dr. Maya Chen",
        started_at: "2026-07-16T12:00:00Z",
        completed_at: "2026-07-16T13:00:00Z",
        artifact_count: 2,
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
        title: "Release evidence review graph",
        state: "pending",
        risk: "High",
        gated_action: "Activate graph v1.0.",
        destination: "Durable Task Scheduler",
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
      },
    ],
  });

  it("preselects the run passed via focusRunId, using existing Runs state", () => {
    render(
      <RunsView data={runsData} onRefresh={jest.fn()} focusRunId="run-1" />,
    );

    expect(
      screen.getAllByText("Evidence review graph").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("Exact gated action")).toBeInTheDocument();
  });
});
