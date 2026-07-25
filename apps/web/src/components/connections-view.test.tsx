import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import {
  ConnectionsView,
  connectorStatusInfo,
} from "@/components/connections-view";
import { testConnector, updateConnector } from "@/lib/api";
import type { WorkspaceData } from "@/lib/api";
import type { ConnectorSetting } from "@/lib/types";

jest.mock("@/lib/api", () => ({
  testConnector: jest.fn(),
  updateConnector: jest.fn(),
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

function workspaceData(
  connectors: ConnectorSetting[] = [connector()],
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
      connector_ready: connectors.filter((c) => c.test_status === "ready")
        .length,
      connector_total: connectors.length,
      last_activity_at: "2026-07-16T12:00:00Z",
      persistence: "in-memory demo",
    },
    library: [],
    runs: [],
    approvals: [],
    connectors,
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
  };
}

describe("connectorStatusInfo", () => {
  it("classifies every connector state truthfully", () => {
    expect(connectorStatusInfo(connector({ enabled: false })).tone).toBe(
      "disabled",
    );
    expect(
      connectorStatusInfo(connector({ test_status: "configuration_required" }))
        .tone,
    ).toBe("configuration-required");
    expect(
      connectorStatusInfo(connector({ test_status: "unavailable" })).tone,
    ).toBe("unavailable");
    expect(
      connectorStatusInfo(connector({ test_status: "ready_with_key" })).tone,
    ).toBe("warning");
    expect(connectorStatusInfo(connector({ test_status: "ready" })).tone).toBe(
      "ready",
    );
    expect(
      connectorStatusInfo(connector({ test_status: "untested" })).tone,
    ).toBe("untested");
  });
});

describe("ConnectionsView loading and empty states", () => {
  it("shows a loading state when data has not arrived yet", () => {
    render(<ConnectionsView data={null} onRefresh={jest.fn()} />);
    expect(screen.getByText("Loading connections…")).toBeInTheDocument();
  });

  it("shows empty states when there are no connectors at all", () => {
    render(<ConnectionsView data={workspaceData([])} onRefresh={jest.fn()} />);
    expect(
      screen.getByText("No connections match this filter"),
    ).toBeInTheDocument();
    expect(screen.getByText("No connection selected")).toBeInTheDocument();
  });

  it("is free of detectable accessibility violations", async () => {
    const { container } = render(
      <ConnectionsView data={workspaceData()} onRefresh={jest.fn()} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ConnectionsView catalog filtering and selection", () => {
  const connectors = [
    connector({ id: "pubmed", name: "PubMed", category: "Literature" }),
    connector({
      id: "grants_gov",
      name: "Grants.gov",
      category: "Funding",
      test_status: "configuration_required",
    }),
    connector({ id: "orcid", name: "ORCID", category: "Identity" }),
    connector({
      id: "some_dataset_source",
      name: "Dataset Source",
      category: "Datasets",
    }),
  ];

  it("filters the catalog by search text", async () => {
    const user = userEvent.setup();
    render(
      <ConnectionsView data={workspaceData(connectors)} onRefresh={jest.fn()} />,
    );
    await user.type(
      screen.getByPlaceholderText("Search connections"),
      "orcid",
    );
    const catalog = screen
      .getByText("Connection catalog")
      .closest(".connector-catalog") as HTMLElement;
    expect(within(catalog).getByText("ORCID")).toBeInTheDocument();
    expect(within(catalog).queryByText("PubMed")).not.toBeInTheDocument();
  });

  it("filters the catalog by category pill", async () => {
    const user = userEvent.setup();
    render(
      <ConnectionsView data={workspaceData(connectors)} onRefresh={jest.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "Funding" }));
    const catalog = screen
      .getByText("Connection catalog")
      .closest(".connector-catalog") as HTMLElement;
    expect(within(catalog).getByText("Grants.gov")).toBeInTheDocument();
    expect(within(catalog).queryByText("PubMed")).not.toBeInTheDocument();
  });

  it("shows a no-match empty state for an unmatched search", async () => {
    const user = userEvent.setup();
    render(
      <ConnectionsView data={workspaceData(connectors)} onRefresh={jest.fn()} />,
    );
    await user.type(
      screen.getByPlaceholderText("Search connections"),
      "nonexistent-source",
    );
    expect(
      screen.getByText("No connections match this filter"),
    ).toBeInTheDocument();
  });

  it("selects a different connector from the catalog and updates the manager", async () => {
    const user = userEvent.setup();
    render(
      <ConnectionsView data={workspaceData(connectors)} onRefresh={jest.fn()} />,
    );
    await user.click(
      screen.getByRole("button", { name: /ORCID Identity/ }),
    );
    const form = screen
      .getByRole("heading", { name: "Connection manager" })
      .closest("form") as HTMLElement;
    expect(
      form.querySelector(".managed-connector-title strong")?.textContent,
    ).toBe("ORCID");
  });

  it("switches the managed connector via the manager's own select dropdown and renders each category's icon", async () => {
    const user = userEvent.setup();
    render(
      <ConnectionsView data={workspaceData(connectors)} onRefresh={jest.fn()} />,
    );
    const select = screen.getByRole("combobox", {
      name: "Connection to manage",
    });
    expect(select).toHaveValue("pubmed");

    for (const id of ["orcid", "grants_gov", "some_dataset_source", "pubmed"]) {
      await user.selectOptions(select, id);
      expect(select).toHaveValue(id);
    }

    const form = screen
      .getByRole("heading", { name: "Connection manager" })
      .closest("form") as HTMLElement;
    expect(
      form.querySelector(".managed-connector-title strong")?.textContent,
    ).toBe("PubMed");
  });
});

describe("ConnectionsView connector manager actions", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("disables the enable checkbox for required baseline connections", () => {
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "pubmed" })])}
        onRefresh={jest.fn()}
      />,
    );
    expect(
      screen.getByRole("checkbox", { name: "Enable PubMed" }),
    ).toBeDisabled();
  });

  it("allows enabling/disabling a non-baseline connector and unassigning agents", async () => {
    const user = userEvent.setup();
    const europePmc = connector({
      id: "europe_pmc",
      name: "Europe PMC",
      assigned_agents: ["literature", "matching"],
    });
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    jest.mocked(updateConnector).mockResolvedValue({
      ...europePmc,
      enabled: false,
      assigned_agents: ["literature"],
    });
    render(
      <ConnectionsView data={workspaceData([europePmc])} onRefresh={onRefresh} />,
    );

    const enableCheckbox = screen.getByRole("checkbox", {
      name: "Enable Europe PMC",
    });
    expect(enableCheckbox).not.toBeDisabled();
    await user.click(enableCheckbox);
    await user.click(
      screen.getByRole("checkbox", {
        name: "Assign matching to Europe PMC",
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: "Assign grant to Europe PMC",
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
          assigned_agents: ["literature", "grant"],
        }),
      ),
    );
    await waitFor(() =>
      expect(screen.getByText(/configuration saved/i)).toBeInTheDocument(),
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it("shows an error status when saving configuration fails", async () => {
    const user = userEvent.setup();
    jest.mocked(updateConnector).mockRejectedValue(new Error("Save failed"));
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "europe_pmc", name: "Europe PMC" })])}
        onRefresh={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() =>
      expect(screen.getByText("Save failed")).toBeInTheDocument(),
    );
  });

  it("falls back to a generic message when a non-Error save rejection occurs", async () => {
    const user = userEvent.setup();
    jest.mocked(updateConnector).mockRejectedValue("boom");
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "europe_pmc", name: "Europe PMC" })])}
        onRefresh={jest.fn()}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText("Connector update failed."),
      ).toBeInTheDocument(),
    );
  });

  it("runs a connection test and reports success tone for a ready result", async () => {
    const user = userEvent.setup();
    const target = connector({ id: "europe_pmc", name: "Europe PMC" });
    jest.mocked(testConnector).mockResolvedValue({
      ...target,
      test_status: "ready",
    });
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    render(
      <ConnectionsView data={workspaceData([target])} onRefresh={onRefresh} />,
    );
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() =>
      expect(screen.getByText(/Europe PMC: Ready\./)).toBeInTheDocument(),
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it("runs a connection test and reports error tone for an unavailable result", async () => {
    const user = userEvent.setup();
    const target = connector({ id: "europe_pmc", name: "Europe PMC" });
    jest.mocked(testConnector).mockResolvedValue({
      ...target,
      test_status: "unavailable",
    });
    render(
      <ConnectionsView data={workspaceData([target])} onRefresh={jest.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    const status = await screen.findByText(/Europe PMC: Connection failed/);
    expect(status.closest('[role="status"]')?.className).toContain("error");
  });

  it("runs a connection test and reports warning tone for a setup-required result", async () => {
    const user = userEvent.setup();
    const target = connector({ id: "europe_pmc", name: "Europe PMC" });
    jest.mocked(testConnector).mockResolvedValue({
      ...target,
      test_status: "configuration_required",
    });
    render(
      <ConnectionsView data={workspaceData([target])} onRefresh={jest.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    const status = await screen.findByText(/Europe PMC: Setup required/);
    expect(status.closest('[role="status"]')?.className).toContain("warning");
  });

  it("shows an error status when the connection test rejects", async () => {
    const user = userEvent.setup();
    jest.mocked(testConnector).mockRejectedValue(new Error("Test failed"));
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "europe_pmc", name: "Europe PMC" })])}
        onRefresh={jest.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() =>
      expect(screen.getByText("Test failed")).toBeInTheDocument(),
    );
  });

  it("falls back to a generic message when a non-Error test rejection occurs", async () => {
    const user = userEvent.setup();
    jest.mocked(testConnector).mockRejectedValue("boom");
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "europe_pmc", name: "Europe PMC" })])}
        onRefresh={jest.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() =>
      expect(screen.getByText("Connector test failed.")).toBeInTheDocument(),
    );
  });

  it("links to the provider terms for the managed connector", () => {
    render(
      <ConnectionsView
        data={workspaceData([connector({ id: "pubmed" })])}
        onRefresh={jest.fn()}
      />,
    );
    const link = screen.getByRole("link", { name: /Provider terms/ });
    expect(link).toHaveAttribute(
      "href",
      "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    );
    expect(link).toHaveAttribute("data-terms-state", "ready");
  });

  it("fails closed on an unapproved terms URL instead of rendering a raw anchor", () => {
    render(
      <ConnectionsView
        data={workspaceData([
          connector({ id: "pubmed", terms_url: "https://evil.example.com/terms" }),
        ])}
        onRefresh={jest.fn()}
      />,
    );
    expect(
      screen.queryByRole("link", { name: /Provider terms/ }),
    ).not.toBeInTheDocument();
    const blocked = screen.getByRole("status", {
      name: "This link targets a host that is not on the approved list.",
    });
    expect(blocked).toHaveAttribute("data-terms-state", "blocked-url");
  });

  it("fails closed on a non-https terms URL", () => {
    render(
      <ConnectionsView
        data={workspaceData([
          connector({
            id: "pubmed",
            terms_url: "http://www.ncbi.nlm.nih.gov/home/about/policies/",
          }),
        ])}
        onRefresh={jest.fn()}
      />,
    );
    expect(
      screen.queryByRole("link", { name: /Provider terms/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("status", {
        name: "Only secure (https) links can be opened.",
      }),
    ).toBeInTheDocument();
  });

  it("shows the gateway readiness card as registered when a matching connector exists", () => {
    render(
      <ConnectionsView
        data={workspaceData([
          connector({
            id: "internal-apim",
            name: "Internal APIM Gateway",
            category: "Gateway",
          }),
        ])}
        onRefresh={jest.fn()}
      />,
    );
    expect(
      screen.getByText(/Internal APIM Gateway is registered/),
    ).toBeInTheDocument();
  });
});
