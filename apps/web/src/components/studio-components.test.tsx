import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  AutomationStudio,
  DatasetStudio,
  GrantStudio,
  InstitutionalStudio,
  LiteratureStudio,
  MatchingStudio,
} from "@/components/studio-components";
import { uploadLibraryItem } from "@/lib/api";
import type { WorkspaceData } from "@/lib/api";
import type {
  AutomationStudioResult,
  GrantStudioResult,
  InstitutionalStudioResult,
  LiteratureStudioResult,
  MatchingStudioResult,
  StudioRun,
} from "@/lib/types";

jest.mock("@/lib/api", () => ({
  uploadLibraryItem: jest.fn(),
}));

jest.mock("@/components/research-markdown", () => ({
  ResearchMarkdown: () => null,
}));

const mockedUploadLibraryItem = jest.mocked(uploadLibraryItem);

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

describe("LiteratureStudio", () => {
  const literatureResult: LiteratureStudioResult = {
    run: baseRun({ capability: "literature" }),
    protocol: {
      research_question: "q",
      date_from: 2020,
      date_to: 2026,
      sources: ["PubMed"],
      inclusion_criteria: ["Primary study"],
      exclusion_criteria: ["Duplicate"],
    },
    search_queries: ["q"],
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
        decision: "include",
        reason: "Matches protocol",
        duplicate_group: null,
      },
    ],
    extraction_matrix: [
      {
        source_id: "source-1",
        method: "Method A",
        population: "Pop A",
        outcome: "Outcome A",
        limitation: "Limitation A",
        citation_ids: ["cite-1"],
      },
      {
        source_id: "source-2",
        method: "Method B",
        population: "Pop B",
        outcome: "Outcome B",
        limitation: "Limitation B",
        citation_ids: ["cite-2"],
      },
    ],
    synthesis: ["Synthesis paragraph."],
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
        page_start: 1,
      },
      {
        id: "cite-2",
        title: "Study B",
        section: "Results",
        quote: "Quote B",
        source_id: "source-2",
        checksum: "sha256:b",
        license: "CC BY",
        chunk_id: "chunk-2",
        page_start: 4,
      },
    ],
    insight: {
      agent_name: "Literature synthesis",
      content: "Analysis",
      evidence_state: "verified",
      online_research_used: false,
      referenced_source_ids: ["source-1"],
      unresolved_source_ids: ["source-2"],
    },
  };

  it("switches Screen/Extract/Synthesize/Audit tabs to distinct content", async () => {
    const user = userEvent.setup();
    render(
      <LiteratureStudio
        result={literatureResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("Study A")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Method A")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Extract" }));
    expect(screen.getByDisplayValue("Method A")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exclude" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Synthesize" }));
    expect(screen.getByText("Synthesis paragraph.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Audit" }));
    expect(screen.getByText("Claim & citation audit")).toBeInTheDocument();
    expect(screen.getByText("Resolved")).toBeInTheDocument();
    expect(screen.getByText("Unresolved")).toBeInTheDocument();
  });

  it("adds and removes inclusion/exclusion criteria and sends them on run", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <LiteratureStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.type(
      screen.getByPlaceholderText("Add inclusion criterion"),
      "New criterion",
    );
    await user.click(
      screen.getByRole("button", { name: "Add inclusion criterion" }),
    );
    expect(screen.getByText("New criterion")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "Remove inclusion criterion: Methods available",
      }),
    );
    expect(screen.queryByText("Methods available")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Search & screen evidence" }),
    );
    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.inclusion_criteria).toContain("New criterion");
    expect(inputs.inclusion_criteria).not.toContain("Methods available");
  });

  it("records Include/Exclude/Maybe screening decisions and updates downstream counts", async () => {
    const user = userEvent.setup();
    render(
      <LiteratureStudio
        result={literatureResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    const metricLine = document.querySelector(".metric-line");
    expect(metricLine?.textContent).toContain("2 included");

    const studyARow = screen.getByText("Study A").closest(".screening-record");
    expect(studyARow).not.toBeNull();
    await user.click(
      within(studyARow as HTMLElement).getByRole("button", {
        name: "Exclude",
      }),
    );

    await user.click(screen.getByRole("button", { name: "Extract" }));
    expect(screen.queryByDisplayValue("Method A")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Method B")).toBeInTheDocument();
  });

  it("edits an extraction cell and exports the current version as CSV", async () => {
    const user = userEvent.setup();
    jest
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    render(
      <LiteratureStudio
        result={literatureResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Extract" }));
    const methodField = screen.getByLabelText("Method for Study A");
    await user.clear(methodField);
    await user.type(methodField, "Revised method");
    expect(methodField).toHaveValue("Revised method");

    const exportButton = screen.getByRole("button", { name: "Export CSV" });
    expect(exportButton).toBeEnabled();
    await user.click(exportButton);
    expect(screen.getByRole("status")).toHaveTextContent(
      /exported 2 extraction rows/i,
    );
  });
});

describe("GrantStudio", () => {
  const grantResult: GrantStudioResult = {
    run: baseRun({ capability: "grant" }),
    opportunity: {
      canonical_url: "https://www.grants.gov/",
      deadline: "2026-10-15",
      identifier: "SORI-2026-01",
      sponsor: "Example Federal Research Office",
      status: "Open",
      title: "Open Research Infrastructure Opportunity",
    },
    requirements: [
      {
        id: "summary",
        text: "Project summary",
        category: "Narrative",
        status: "mapped",
        evidence_ids: ["cite-1"],
      },
      {
        id: "budget",
        text: "Budget justification",
        category: "Budget",
        status: "needs_input",
        evidence_ids: [],
      },
    ],
    fact_gaps: [],
    specific_aims: ["Aim one."],
    sections: [
      {
        id: "significance",
        title: "Significance",
        status: "draft",
        word_count: 2,
        body: "Significance body text.",
        evidence_ids: [],
      },
    ],
    readiness: 80,
    blockers: [],
    citations: [
      {
        id: "cite-1",
        title: "Open Research Infrastructure Opportunity",
        section: "Eligibility",
        quote: "Applicants must summarize the project in two pages.",
        source_id: "notice-1",
        checksum: "sha256:notice",
        license: "Public domain",
        chunk_id: "chunk-notice-1",
        page_start: 1,
      },
    ],
  };

  const workspaceData: Pick<WorkspaceData, "connectors"> = {
    connectors: [
      {
        id: "grants_gov",
        name: "Grants.gov",
        category: "Funding",
        description: "Authoritative U.S. federal opportunity records.",
        auth_kind: "None",
        secret_status: "Not required",
        enabled: true,
        test_status: "ready",
        last_tested_at: null,
        assigned_agents: ["grant"],
        terms_url: "https://www.grants.gov/web/grants/legal-privacy.html",
        data_boundary: "Public metadata only.",
        capabilities: ["Opportunities"],
      },
    ],
  };

  it("switches document tabs between drafted and not-yet-drafted sections", async () => {
    const user = userEvent.setup();
    render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as WorkspaceData}
      />,
    );

    expect(screen.getByText("Aim one.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Significance" }));
    expect(screen.getByText("Significance body text.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approach" }));
    expect(
      screen.getByText(/Not yet drafted for this section/i),
    ).toBeInTheDocument();
  });

  it("runs a distinct red-team pass separate from the primary build action", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        data={workspaceData as WorkspaceData}
      />,
    );

    await user.click(screen.getByRole("button", { name: /red-team draft/i }));
    expect(onRun).toHaveBeenCalledTimes(1);
    const [capability, objective, options] = onRun.mock.calls[0];
    expect(capability).toBe("grant");
    expect(objective).toMatch(/red-team/i);
    expect(options.inputs.red_team_pass).toBe(true);
  });

  it("selects funding sources and records a connector draft request requiring review", async () => {
    const user = userEvent.setup();
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as WorkspaceData}
      />,
    );

    const fundingPanel = screen.getByLabelText("Funding source discovery");
    expect(within(fundingPanel).getByText("Grants.gov")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Request a new connector" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: /request a new connector/i,
    });
    expect(
      within(dialog).getByText(/records a draft request only/i),
    ).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Connector name"), "NSF Awards");
    await user.type(
      within(dialog).getByLabelText("Base URL"),
      "https://api.nsf.gov",
    );
    await user.type(
      within(dialog).getByLabelText("Justification"),
      "Needed for federal award discovery.",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Save draft request" }),
    );

    expect(screen.getByText("NSF Awards")).toBeInTheDocument();
    expect(screen.getByText("Draft — needs review")).toBeInTheDocument();
  });

  it("filters opportunity discovery to selected governed connectors and populates the opportunity ID", async () => {
    const user = userEvent.setup();
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as WorkspaceData}
      />,
    );

    const discoveryPanel = screen.getByLabelText("Opportunity discovery");
    expect(
      within(discoveryPanel).getByText("Grants.gov"),
    ).toBeInTheDocument();

    await user.type(
      within(discoveryPanel).getByLabelText("Search funding opportunities"),
      "nothing matches",
    );
    expect(
      within(discoveryPanel).getByText(
        /no net-new opportunities match this query/i,
      ),
    ).toBeInTheDocument();

    await user.clear(
      within(discoveryPanel).getByLabelText("Search funding opportunities"),
    );
    await user.click(
      within(discoveryPanel).getByRole("button", {
        name: "Use as opportunity source",
      }),
    );
    expect(
      (screen.getByLabelText("Opportunity ID") as HTMLInputElement).value,
    ).toMatch(/GRANTS_GOV-LEAD-/i);
  });

  it("opens source evidence for a mapped requirement and reports gaps for an unmapped one", async () => {
    const user = userEvent.setup();
    render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as WorkspaceData}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /project summary/i }),
    );
    const mappedDialog = screen.getByRole("dialog", {
      name: "Project summary",
    });
    expect(
      within(mappedDialog).getByText(
        /Applicants must summarize the project in two pages\./,
      ),
    ).toBeInTheDocument();
    await user.click(
      within(mappedDialog).getByLabelText("Close requirement detail"),
    );

    await user.click(
      screen.getByRole("button", { name: /budget justification/i }),
    );
    const gapDialog = screen.getByRole("dialog", {
      name: "Budget justification",
    });
    expect(
      within(gapDialog).getByText(
        /no source evidence is linked to this requirement yet/i,
      ),
    ).toBeInTheDocument();
  });
});

describe("MatchingStudio", () => {
  const matchingResult: MatchingStudioResult = {
    run: baseRun({ capability: "matching" }),
    criteria: [],
    matches: [
      {
        id: "match-1",
        name: "Dr. Amara Osei",
        kind: "person",
        score: 88,
        freshness: "Updated 2 days ago",
        strengths: ["Genomics"],
        gaps: [],
        hard_filters_passed: true,
        components: [
          {
            criterion_id: "expertise",
            label: "Expertise match",
            weight: 0.6,
            match: 0.9,
            contribution: 54,
            evidence_id: "cite-1",
          },
        ],
      },
      {
        id: "match-2",
        name: "Core Genomics Facility",
        kind: "facility",
        score: 71,
        freshness: "Updated 5 days ago",
        strengths: ["Sequencing"],
        gaps: [],
        hard_filters_passed: true,
        components: [
          {
            criterion_id: "capacity",
            label: "Capacity match",
            weight: 0.4,
            match: 0.7,
            contribution: 28,
            evidence_id: "cite-2",
          },
        ],
      },
    ],
    shortlist_ids: [],
    citations: [],
  };

  it("sends selected record types and hard filters as run inputs", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <MatchingStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: "Methods" }));
    await user.click(
      screen.getByRole("checkbox", { name: "Current institutional record" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );

    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.record_kinds).toContain("method");
    expect(inputs.hard_filters).not.toContain("current_institutional_record");
    expect(inputs.hard_filters).toContain("source_evidence_available");
  });

  it("toggles a persistent shortlist and shows a transparent score comparison", async () => {
    const user = userEvent.setup();
    render(
      <MatchingStudio
        result={matchingResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /add dr\. amara osei to shortlist/i }),
    );
    await user.click(
      screen.getByRole("button", {
        name: /add core genomics facility to shortlist/i,
      }),
    );
    expect(screen.getByText("Shortlist (2)")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Compare shortlisted" }),
    );
    expect(screen.getByText("Top evidence factors")).toBeInTheDocument();
    expect(screen.getByText("Expertise match (54.0)")).toBeInTheDocument();
  });

  it("controls public/institutional source selection and keeps Work IQ disabled, sending sources on run", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const data: Pick<WorkspaceData, "connectors"> = {
      connectors: [
        {
          id: "openalex",
          name: "OpenAlex",
          category: "Discovery",
          description: "Open catalog of works, people, and institutions.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["matching"],
          terms_url: "https://docs.openalex.org/",
          data_boundary: "Public metadata only.",
          capabilities: ["Search", "Entity leads"],
        },
      ],
    };
    render(
      <MatchingStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        data={data as WorkspaceData}
      />,
    );

    const workIqToggle = screen.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    expect(workIqToggle).toBeDisabled();
    expect(workIqToggle).not.toBeChecked();

    await user.click(
      screen.getByRole("checkbox", { name: "Institutional directory" }),
    );
    await user.click(screen.getByRole("checkbox", { name: "OpenAlex" }));
    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );

    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.sources).not.toContain("institutional");
    expect(inputs.sources).not.toContain("openalex");
  });
});

describe("DatasetStudio", () => {
  it("validates a bounded CSV file, uploads it, and requires plan approval before profiling", async () => {
    const user = userEvent.setup();
    mockedUploadLibraryItem.mockResolvedValue({
      item: {
        id: "lib-1",
        title: "sample.csv",
        kind: "Dataset",
        source: "Workspace upload",
        status: "processing",
        access: "internal",
        version: "1.0",
        checksum: "sha256:test",
        license: "Project supplied",
        added_at: "2026-07-16T12:00:00Z",
        evidence_count: 0,
        connector: "workspace",
        provider: "Workspace upload",
        description: "test",
      },
      run: {
        id: "run-ingest-1",
        durable_instance_id: "research-run-ingest-1",
        project_id: "demo-project",
        capability: "dataset",
        title: "Ingest",
        status: "planned",
        progress: 0,
        current_stage: "queued",
        owner: "Dr. Maya Chen",
        started_at: "2026-07-16T12:00:00Z",
        completed_at: null,
        artifact_count: 0,
        estimated_cost_usd: 0,
        scheduler_managed: false,
        scheduling_state: "not_managed",
      },
    });
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        onRefresh={jest.fn().mockResolvedValue(undefined)}
      />,
    );

    const runButton = screen.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    expect(runButton).toBeDisabled();

    const file = new File(["a,b\n1,2\n"], "sample.csv", { type: "text/csv" });
    await user.upload(screen.getByLabelText("Upload a dataset file"), file);
    expect(screen.getByText("sample.csv")).toBeInTheDocument();
    expect(runButton).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Upload to Library" }),
    );
    expect(mockedUploadLibraryItem).toHaveBeenCalledTimes(1);

    await user.click(
      screen.getByLabelText(
        /I approve sending this bounded dataset to the Foundry Dataset Agent/,
      ),
    );
    expect(runButton).toBeEnabled();

    await user.click(runButton);
    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.filename).toBe("sample.csv");
    expect(inputs.csv_text).toContain("a,b");
  });

  it("rejects an oversized file client-side without pretending to profile it", async () => {
    const user = userEvent.setup();
    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    const input = screen.getByLabelText("Upload a dataset file");
    expect(input).toHaveAttribute(
      "accept",
      ".csv,.json,text/csv,application/json",
    );

    const oversizedFile = new File(
      [new Array(6_000_001).fill("a").join("")],
      "huge.csv",
      { type: "text/csv" },
    );
    await user.upload(input, oversizedFile);
    expect(
      screen.getByText(/files must be 5 mb or smaller/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    ).toBeDisabled();
  });
});

describe("InstitutionalStudio", () => {
  const institutionalResult: InstitutionalStudioResult = {
    run: baseRun({ capability: "institutional_qa" }),
    abstained: false,
    answer: "Disclosure is required per policy v2.",
    citations: [
      {
        id: "cite-irb-1",
        title: "IRB Handbook",
        section: "Section 4.2",
        quote: "Generative AI use must be disclosed.",
        source_id: "irb-handbook",
        checksum: "sha256:irb",
        license: "Internal",
        chunk_id: "chunk-9",
        page_start: 12,
        page_end: 13,
      },
    ],
    conflicts: [],
    escalation: null,
    scope: "IRB and research compliance",
    versions: [],
  };

  it("controls authorized corpus scopes and locks the legal-hold corpus", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <InstitutionalStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const legalHold = screen.getByRole("checkbox", { name: /legal hold/i });
    expect(legalHold).toBeDisabled();

    await user.click(
      screen.getByRole("checkbox", { name: /research records/i }),
    );
    await user.click(
      screen.getByRole("button", { name: "Resolve policy answer" }),
    );
    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.corpus_scopes).not.toContain("records");
    expect(inputs.corpus_scopes).not.toContain("legal_hold");
    expect(inputs.corpus_scopes).toContain("irb");
  });

  it("opens an evidence detail dialog from an inline citation", async () => {
    const user = userEvent.setup();
    render(
      <InstitutionalStudio
        result={institutionalResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /IRB Handbook · Section 4\.2/i }),
    );
    const dialog = screen.getByRole("dialog", { name: "IRB Handbook" });
    expect(
      within(dialog).getByText("Generative AI use must be disclosed."),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("sha256:irb")).toBeInTheDocument();
  });

  it("shows an honest disabled default-off Work IQ readiness panel", () => {
    render(
      <InstitutionalStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    const toggle = screen.getByRole("checkbox", {
      name: /enable work iq readiness signals/i,
    });
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
  });
});

describe("AutomationStudio", () => {
  const automationResult: AutomationStudioResult = {
    run: baseRun({ capability: "orchestration" }),
    template_id: "evidence-review-v2",
    trigger: "Manual",
    steps: [
      {
        id: "ingest",
        label: "Ingest & verify",
        kind: "activity",
        depends_on: [],
        retry_limit: 3,
        approval_required: false,
      },
    ],
    validation_errors: [],
    dry_run_status: "passed",
    graph_version: "2.0",
    graph_hash: "abcdef1234567890",
    citations: [],
  };

  it("zooms the workflow graph within bounds", async () => {
    const user = userEvent.setup();
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("100%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByText("110%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(screen.getByText("90%")).toBeInTheDocument();
  });

  it("adds, configures, and removes a bounded workflow step and sends edits to dry run", async () => {
    const user = userEvent.setup();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Notify reviewer");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(within(stepEditor).getByText("Notify reviewer")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Configure Ingest & verify" }),
    );
    const retryInput = screen.getByLabelText("Retry limit (0-5)");
    await user.clear(retryInput);
    await user.type(retryInput, "2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    const steps = onRun.mock.calls[0][2].inputs.steps;
    expect(
      steps.some((step: { label: string }) => step.label === "Notify reviewer"),
    ).toBe(true);
    const ingestStep = steps.find((step: { id: string }) => step.id === "ingest");
    expect(ingestStep.retry_limit).toBe(2);
  });

  it("gates activation behind a passing dry run and an explicit confirmation", async () => {
    const user = userEvent.setup();
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm activation" }),
    );
    expect(
      screen.getByRole("button", { name: /activated \(draft workspace\)/i }),
    ).toBeDisabled();
  });

  it("adds only an authorized capability catalog entry to the graph and blocks an unauthorized one", async () => {
    const user = userEvent.setup();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [
        {
          id: "literature-agent",
          name: "Literature synthesis",
          model_tier: "Primary",
          status: "Active",
          web_access: "Opt-in public only",
          workflow_steps: ["Protocol", "Search"],
          deployment: "Foundry Hosted Agent",
        },
        {
          id: "grant-agent",
          name: "Grant drafting",
          model_tier: "Primary",
          status: "Disabled",
          web_access: "Opt-in public only",
          workflow_steps: [],
          deployment: "Foundry Hosted Agent",
        },
      ],
      connectors: [],
      runs: [],
    };
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as WorkspaceData}
      />,
    );

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const literatureRow = within(catalog)
      .getByText("Literature synthesis")
      .closest(".step-editor-row") as HTMLElement;
    await user.click(
      within(literatureRow).getByRole("button", { name: "Add to graph" }),
    );
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(
      within(stepEditor).getByText("Literature synthesis"),
    ).toBeInTheDocument();

    const grantRow = within(catalog)
      .getByText("Grant drafting")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(grantRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
  });

  it("manages workflow runs by inspecting via existing Runs state and cloning a fresh draft", async () => {
    const user = userEvent.setup();
    const onNavigateToRun = jest.fn();
    const data: Pick<WorkspaceData, "runs"> = {
      runs: [
        {
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
        },
      ],
    };
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as WorkspaceData}
        onNavigateToRun={onNavigateToRun}
      />,
    );

    const runManager = screen.getByRole("region", {
      name: "Workflow run management",
    });
    expect(
      within(runManager).getByText(/waiting for approval/i),
    ).toBeInTheDocument();

    expect(within(runManager).getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Resume" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Cancel" })).toBeDisabled();

    await user.click(
      within(runManager).getByRole("button", { name: "Inspect" }),
    );
    expect(onNavigateToRun).toHaveBeenCalledWith("run-orc-1");

    await user.click(within(runManager).getByRole("button", { name: "Clone" }));
    expect(
      within(runManager).getByText(/cloned evidence review graph into a new draft/i),
    ).toBeInTheDocument();
  });
});
