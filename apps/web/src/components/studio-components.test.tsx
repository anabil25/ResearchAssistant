import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  AutomationStudio,
  DatasetStudio,
  GrantStudio,
  InstitutionalStudio,
  LiteratureStudio,
  MatchingStudio,
  StudioForCapability,
} from "@/components/studio-components";
import { uploadLibraryItem } from "@/lib/api";
import type { WorkspaceData } from "@/lib/api";
import { isBlockingModalOpen } from "@/lib/blocking-modal";

/**
 * `userEvent.setup` with the artificial inter-event delay removed.
 *
 * userEvent v14 defaults to `delay: 0`, which is not "no delay" -- it awaits a
 * real `setTimeout(..., 0)` between *every* dispatched event. An omnibus test
 * here performs 25+ interactions plus multi-character `type()` calls, so it
 * accumulates well over a hundred of those hops. In isolation each costs
 * roughly nothing; under the full `--runInBand` suite, with a large
 * accumulated heap and a busy event loop, each hop costs far more and varies
 * run to run. That is what made this file's slowest tests sit at ~3.0s and
 * ~4.0s against Jest's 5s default -- ~80% of the budget consumed by waiting
 * that serves no purpose -- and flake intermittently only when the whole
 * suite is loaded.
 *
 * `delay: null` removes the waiting and nothing else: every event userEvent
 * would dispatch is still dispatched, in the same order, through the same
 * code paths. It is safe here specifically because `studio-components.tsx`
 * contains no `setTimeout`/`setInterval`/`requestAnimationFrame` and no
 * debounce, so no assertion in this file depends on time passing between
 * events.
 *
 * Deliberately not fixed by raising the Jest timeout: that hides the
 * contention instead of removing it, and a flake at 5s becomes the same flake
 * at 10s.
 */
function setupUser(
  options: Parameters<typeof userEvent.setup>[0] = {},
): ReturnType<typeof userEvent.setup> {
  return userEvent.setup({ delay: null, ...options });
}
import type {
  AutomationStudioResult,
  DatasetStudioResult,
  GrantStudioResult,
  InstitutionalStudioResult,
  LiteratureStudioResult,
  MatchingStudioResult,
  StudioRun,
  WorkflowBlueprint,
} from "@/lib/types";

jest.mock("@/lib/api", () => ({
  uploadLibraryItem: jest.fn(),
}));

jest.mock("@/components/research-markdown", () => ({
  ResearchMarkdown: ({
    content,
    label,
    unresolvedSourceIds = [],
  }: {
    content: string;
    label?: string;
    unresolvedSourceIds?: string[];
  }) => (
    <div data-testid="research-markdown">
      <strong>{label}</strong>
      <p>{content}</p>
      <span>{unresolvedSourceIds.join(",")}</span>
    </div>
  ),
}));

const mockedUploadLibraryItem = jest.mocked(uploadLibraryItem);
const workflowBlueprint: WorkflowBlueprint = {
  capability: "literature",
  title: "Verified review",
  purpose: "Document deterministic workflow ownership.",
  primary_artifact: "Evidence package",
  online_research_policy: "Opt-in public metadata only.",
  stages: [
    {
      id: "protocol",
      label: "Protocol",
      description: "Lock scope and dates.",
      owner: "PI",
      human_checkpoint: true,
    },
    {
      id: "audit",
      label: "Audit",
      description: "Verify claims and citations.",
      owner: "Reviewer",
      human_checkpoint: false,
    },
  ],
};

afterEach(() => {
  jest.restoreAllMocks();
  mockedUploadLibraryItem.mockReset();
});

/**
 * Forces a genuine `click` event through to a React `onClick` handler on an
 * element that React currently considers `disabled`, to test a handler-level
 * guard independent of (i.e. not merely relying on) the disabled attribute
 * blocking the click. A disabled form control never dispatches a real click
 * in either a browser or jsdom, and merely mutating the DOM `disabled`
 * property is *not* sufficient to bypass this: React's own event
 * delegation (`getListener`) additionally checks its own internally
 * recorded props snapshot for the element (not the live DOM attribute)
 * before invoking `onClick` on a `button`/`input`/`select`/`textarea`, and
 * refuses to deliver the event at all if that snapshot says `disabled`.
 * This clears both the DOM property and React's internal snapshot so the
 * click genuinely reaches the production handler, which must then apply
 * its own (independent) guard.
 */
function forceClickBypassingReactDisabled(element: HTMLElement): void {
  (element as HTMLButtonElement).disabled = false;
  const propsKey = Object.keys(element).find((key) =>
    key.startsWith("__reactProps$"),
  );
  if (propsKey) {
    const target = element as unknown as Record<string, Record<string, unknown>>;
    target[propsKey] = { ...target[propsKey], disabled: false };
  }
  fireEvent.click(element);
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
    const user = setupUser();
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
    expect(document.querySelector('[data-audit-status="warning"]')).toHaveTextContent(
      /unresolved references found/i,
    );
  });

  it("marks the audit tab not-verified when no hosted-agent insight is present, even with resolved citations", async () => {
    const user = setupUser();
    const noInsightResult = {
      ...literatureResult,
      insight: undefined,
    } as unknown as LiteratureStudioResult;
    render(
      <LiteratureStudio
        result={noInsightResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Audit" }));
    expect(
      document.querySelector('[data-audit-status="not-verified"]'),
    ).toHaveTextContent(/not verified — no hosted-agent insight/i);
    expect(document.querySelector('[data-audit-status="passed"]')).toBeNull();
    expect(document.querySelector('[data-audit-status="warning"]')).toBeNull();
  });

  it("adds and removes inclusion/exclusion criteria and sends them on run", async () => {
    const user = setupUser();
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
    const user = setupUser();
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
    const user = setupUser();
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

  it(
    "supports workflow metadata, protocol keyboard input, online research, and empty extraction states",
    async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <LiteratureStudio
        result={literatureResult}
        running={false}
        error="Protocol review required"
        onRun={onRun}
        workflow={workflowBlueprint}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Protocol review required",
    );
    expect(
      screen.getByRole("list", { name: "Literature Studio workflow" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Protocol")).toBeInTheDocument();
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "Analysis",
    );

    fireEvent.change(screen.getByLabelText("Research question"), {
      target: { value: "Audit retrieval quality" },
    });
    fireEvent.change(screen.getByLabelText("Published from"), {
      target: { value: "2018" },
    });
    fireEvent.change(screen.getByLabelText("Through"), {
      target: { value: "2019" },
    });
    await user.type(
      screen.getByPlaceholderText("Add inclusion criterion"),
      "Appendix{enter}",
    );
    await user.type(
      screen.getByPlaceholderText("Add exclusion criterion"),
      "Retracted{enter}",
    );
    await user.type(
      screen.getByPlaceholderText("Add exclusion criterion"),
      "No registry ID",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add exclusion criterion" }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove exclusion criterion: Duplicate record",
      }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "PubMed" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "arXiv" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: /current public research/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Search & screen evidence" }),
    );

    const inputs = onRun.mock.calls[0][2].inputs;
    expect(onRun.mock.calls[0][1]).toBe(
      "Audit retrieval quality",
    );
    expect(onRun.mock.calls[0][2].onlineResearch).toBe(true);
    expect(inputs.date_from).toBe(2018);
    expect(inputs.date_to).toBe(2019);
    expect(inputs.sources).toContain("arXiv");
    expect(inputs.sources).not.toContain("PubMed");
    expect(inputs.inclusion_criteria).toContain(
      "Appendix",
    );
    expect(inputs.exclusion_criteria).toContain("Retracted");
    expect(inputs.exclusion_criteria).toContain("No registry ID");
    expect(inputs.exclusion_criteria).not.toContain("Duplicate record");
    expect(inputs.public_search_query).toBe(
      "Audit retrieval quality",
    );

    fireEvent.click(
      within(
        screen.getByText("Study A").closest(".screening-record") as HTMLElement,
      ).getByRole("button", { name: "Exclude" }),
    );
    fireEvent.click(
      within(
        screen.getByText("Study B").closest(".screening-record") as HTMLElement,
      ).getByRole("button", { name: "Exclude" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Extract" }));
    expect(
      screen.getByText(/no included study currently has extractable fields/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Export CSV" }),
    ).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Audit" }));
    expect(screen.getByText("Study A — Matches protocol")).toBeInTheDocument();
    expect(screen.getByText("Study B — Matches protocol")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Screen" }));
    expect(document.querySelector(".metric-line")?.textContent).toContain(
      "2 candidates",
    );
    },
    10000,
  );

  it("blocks submission for an out-of-order or future-dated protocol window and recovers once corrected", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const { container } = render(
      <LiteratureStudio
        result={literatureResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const publishedFrom = screen.getByLabelText("Published from");
    const through = screen.getByLabelText("Through");
    const runButton = screen.getByRole("button", {
      name: "Search & screen evidence",
    });

    fireEvent.change(publishedFrom, { target: { value: "" } });
    fireEvent.change(through, { target: { value: "" } });
    expect(screen.getByRole("alert")).toHaveTextContent(
      /enter a published-from and through year/i,
    );
    expect(runButton).toBeDisabled();

    fireEvent.change(publishedFrom, { target: { value: "not-a-year" } });
    fireEvent.change(through, { target: { value: "also-not-a-year" } });
    expect(screen.getByRole("alert")).toHaveTextContent(
      /enter a published-from and through year/i,
    );
    expect(runButton).toBeDisabled();

    fireEvent.change(publishedFrom, { target: { value: "2022" } });
    fireEvent.change(through, { target: { value: "2019" } });
    expect(screen.getByRole("alert")).toHaveTextContent(
      /must not be after/i,
    );
    expect(publishedFrom).toHaveAttribute("aria-invalid", "true");
    expect(through).toHaveAttribute("aria-invalid", "true");
    expect(runButton).toBeDisabled();

    const futureYear = String(new Date().getFullYear() + 1);
    fireEvent.change(publishedFrom, { target: { value: "2020" } });
    fireEvent.change(through, { target: { value: futureYear } });
    expect(screen.getByRole("alert")).toHaveTextContent(/future/i);
    expect(runButton).toBeDisabled();

    // The Run button being `disabled` already stops a real click, but the
    // submit handler itself must independently refuse to run the search
    // whenever `dateWindowError` is set -- proving the handler-level guard
    // actually blocks submission (not merely that the button is inert), by
    // dispatching a genuine `submit` event straight at the form, bypassing
    // the disabled button entirely.
    const form = container.querySelector(
      "form.literature-protocol",
    ) as HTMLFormElement;
    fireEvent.submit(form);
    expect(onRun).not.toHaveBeenCalled();

    fireEvent.change(through, { target: { value: "2024" } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(runButton).toBeEnabled();

    await user.click(runButton);
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][2].inputs.date_from).toBe(2020);
    expect(onRun.mock.calls[0][2].inputs.date_to).toBe(2024);
  });

  it("updates every extraction field and clears the export notice on edit", async () => {
    const user = setupUser();
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
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(screen.getByRole("status")).toHaveTextContent(/exported 2 extraction rows/i);

    const populationField = screen.getByLabelText("Population for Study A");
    const outcomeField = screen.getByLabelText("Outcome for Study A");
    const limitationField = screen.getByLabelText("Limitation for Study A");
    fireEvent.change(populationField, {
      target: { value: "Population revised" },
    });
    fireEvent.change(outcomeField, { target: { value: "Outcome revised" } });
    fireEvent.change(limitationField, {
      target: { value: "Limitation revised" },
    });

    expect(populationField).toHaveValue("Population revised");
    expect(outcomeField).toHaveValue("Outcome revised");
    expect(limitationField).toHaveValue("Limitation revised");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("covers literature fallbacks for normalization, audit counts, and single-row CSV export", async () => {
    const user = setupUser();
    jest
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const fallbackResult = {
      ...literatureResult,
      run: { ...literatureResult.run, id: undefined },
      screening: [
        {
          source_id: "fallback-source",
          title: "Fallback study",
          decision: "maybe",
          reason: "Awaiting triage",
          duplicate_group: null,
        },
      ],
      extraction_matrix: [
        {
          source_id: "orphan-source",
          method: 'Quoted, "method"',
          population: "Pop only",
          outcome: "Outcome only",
          limitation: "Only line",
          citation_ids: ["cite-1"],
        },
      ],
      citations: [literatureResult.citations[0]],
      insight: {
        agent_name: "Fallback synthesis",
        content: "Fallback analysis",
        evidence_state: "verified",
        online_research_used: false,
      },
    } as unknown as LiteratureStudioResult;
    render(
      <LiteratureStudio
        result={fallbackResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Include" })).toHaveAttribute(
      "data-active",
      "false",
    );
    expect(screen.getByRole("button", { name: "Maybe" })).toHaveAttribute(
      "data-active",
      "true",
    );

    await user.click(screen.getByRole("button", { name: "Audit" }));
    expect(document.querySelector(".metric-line")?.textContent).toContain(
      "1 resolved",
    );
    expect(document.querySelector('[data-audit-status="passed"]')).toHaveTextContent(
      /passed — every citation was checked/i,
    );

    await user.click(screen.getByRole("button", { name: "Extract" }));
    expect(screen.getByText("orphan-source")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Exported 1 extraction row as extraction-matrix-draft.csv.",
    );
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "Fallback analysis",
    );
  });

  it("ignores blank or duplicate criteria changes", async () => {
    const user = setupUser();
    render(
      <LiteratureStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    const inclusionInput = screen.getByPlaceholderText("Add inclusion criterion");
    const exclusionInput = screen.getByPlaceholderText("Add exclusion criterion");

    await user.type(inclusionInput, "   ");
    await user.click(
      screen.getByRole("button", { name: "Add inclusion criterion" }),
    );
    await user.clear(inclusionInput);
    await user.type(inclusionInput, "Methods available");
    await user.click(
      screen.getByRole("button", { name: "Add inclusion criterion" }),
    );
    expect(screen.getAllByText("Methods available")).toHaveLength(1);

    await user.type(exclusionInput, "Duplicate record");
    await user.click(
      screen.getByRole("button", { name: "Add exclusion criterion" }),
    );
    expect(screen.getAllByText("Duplicate record")).toHaveLength(1);
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
    const user = setupUser();
    render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as unknown as WorkspaceData}
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
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        data={workspaceData as unknown as WorkspaceData}
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
    const user = setupUser();
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as unknown as WorkspaceData}
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
    expect(
      within(dialog).getByText(/not a Copilot SDK container/i),
    ).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Connector name"), "NSF Awards");
    await user.type(
      within(dialog).getByLabelText("Base URL"),
      "https://api.nsf.gov",
    );
    await user.type(
      within(dialog).getByLabelText("Authoritative API documentation"),
      "https://api.nsf.gov/docs",
    );
    await user.type(
      within(dialog).getByLabelText("Terms, license, and robots policy"),
      "https://api.nsf.gov/terms",
    );
    await user.type(
      within(dialog).getByLabelText("Allowed hosts and path prefixes"),
      "api.nsf.gov/v1/",
    );
    await user.selectOptions(
      within(dialog).getByLabelText("Authentication"),
      "None",
    );
    await user.type(
      within(dialog).getByLabelText("Sample query and normalized fields"),
      "award search -> id,title,url",
    );
    await user.type(
      within(dialog).getByLabelText("Justification"),
      "Needed for federal award discovery.",
    );
    await user.click(
      within(dialog).getByLabelText(/confirmed this use is permitted/i),
    );
    await user.click(
      within(dialog).getByLabelText(/generated code requires tests/i),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Save draft request" }),
    );

    expect(screen.getByText("NSF Awards")).toBeInTheDocument();
    expect(screen.getByText("Draft — needs review")).toBeInTheDocument();
  });

  it("filters opportunity discovery to selected governed connectors and populates the opportunity ID", async () => {
    const user = setupUser();
    render(
      <GrantStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as unknown as WorkspaceData}
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
    const user = setupUser();
    render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as unknown as WorkspaceData}
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

  it(
    "submits configured notice parsing inputs and explains empty connector states",
    async () => {
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <GrantStudio
        result={null}
        running={false}
        error="Notice review required"
        onRun={onRun}
        data={{ connectors: [] } as unknown as WorkspaceData}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Notice review required",
    );
    expect(
      screen.getByText(/no funding connectors are assigned yet/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/select at least one funding connector above to discover opportunities/i),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Opportunity ID"), {
      target: { value: "RFA-TRANS-77" },
    });
    fireEvent.change(screen.getByLabelText("Project framing"), {
      target: { value: "Develop a citation-backed infrastructure package." },
    });
    fireEvent.click(
      screen.getByRole("checkbox", { name: /core project facts verified/i }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: /current public research/i }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Parse notice & build package" }),
    );

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][1]).toBe(
      "Develop a citation-backed infrastructure package.",
    );
    expect(onRun.mock.calls[0][2].onlineResearch).toBe(true);
    expect(onRun.mock.calls[0][2].inputs).toMatchObject({
      opportunity_id: "RFA-TRANS-77",
      project_facts: [
        "Research office sponsor confirmed",
        "PI role confirmed",
      ],
      public_search_query: "RFA-TRANS-77 public funding opportunity requirements",
    });
    },
    10000,
  );

  it("filters discovery connectors, dismisses connector dialogs, and renders readiness blockers", async () => {
    const user = setupUser({ applyAccept: false });
    const readinessResult = {
      ...grantResult,
      fact_gaps: [
        {
          id: "gap-1",
          label: "Biosketch missing",
          guidance: "Upload the verified PI biosketch before export.",
          status: "missing",
        },
      ],
      blockers: ["budget sign-off"],
      insight: {
        agent_name: "Grant drafting",
        content: "Draft package reviewed.",
        evidence_state: "verified",
        online_research_used: false,
        referenced_source_ids: ["notice-1"],
        unresolved_source_ids: [],
      },
    } as unknown as GrantStudioResult;
    const data: Pick<WorkspaceData, "connectors"> = {
      connectors: [
        workspaceData.connectors[0],
        {
          id: "foundation_dir",
          name: "Foundation Directory",
          category: "Funding",
          description: "Private foundation opportunity records.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://foundationdirectory.example.test/",
          data_boundary: "Public metadata only.",
          capabilities: ["Awards"],
        },
      ],
    };
    render(
      <GrantStudio
        result={readinessResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
      />,
    );

    expect(screen.getByText("Biosketch missing")).toBeInTheDocument();
    expect(screen.getByText(/export blocked by/i)).toHaveTextContent(
      "budget sign-off",
    );
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "Draft package reviewed.",
    );

    await user.click(
      screen.getByRole("button", { name: "Request a new connector" }),
    );
    let dialog = screen.getByRole("dialog", {
      name: /request a new connector/i,
    });
    await user.click(
      within(dialog).getByLabelText("Close connector request dialog"),
    );
    expect(
      screen.queryByRole("dialog", { name: /request a new connector/i }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Request a new connector" }),
    );
    dialog = screen.getByRole("dialog", { name: /request a new connector/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("dialog", { name: /request a new connector/i }),
    ).not.toBeInTheDocument();

    const fundingPanel = screen.getByLabelText("Funding source discovery");
    await user.click(
      within(fundingPanel).getByRole("checkbox", {
        name: /Foundation Directory/i,
      }),
    );
    const discoveryPanel = screen.getByLabelText("Opportunity discovery");
    await user.click(screen.getByRole("button", { name: "Awards" }));
    expect(
      within(discoveryPanel).getByText("Foundation Directory"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("checkbox", { name: /Foundation Directory/i }),
    );
    expect(
      within(discoveryPanel).getByText(
        /no net-new opportunities match this query/i,
      ),
    ).toBeInTheDocument();
  });

  it("tracks red-team status while running and includes newly added funding sources", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const data: Pick<WorkspaceData, "connectors"> = {
      connectors: [
        workspaceData.connectors[0],
        {
          id: "foundation_dir",
          name: "Foundation Directory",
          category: "Funding",
          description: "Private foundation opportunity records.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://foundationdirectory.example.test/",
          data_boundary: "Public metadata only.",
          capabilities: ["Awards"],
        },
      ],
    };
    const { rerender } = render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );

    await user.click(
      screen.getByRole("checkbox", { name: /Foundation Directory/i }),
    );
    await user.click(screen.getByRole("button", { name: /red-team draft/i }));
    expect(onRun.mock.calls[0][2].inputs.sources).toContain(
      "foundation_dir",
    );
    expect(screen.getByText(/80% ready · red-team pass/i)).toBeInTheDocument();

    rerender(
      <GrantStudio
        result={grantResult}
        running
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Red-teaming..." }),
    ).toBeDisabled();
  });

  it("maps configuration_required and unavailable funding connector test_status to distinct non-runnable states and excludes them from the run payload", async () => {
    const user = setupUser();    const onRun = jest.fn().mockResolvedValue(undefined);
    const data: Pick<WorkspaceData, "connectors"> = {
      connectors: [
        workspaceData.connectors[0],
        {
          id: "foundation_dir",
          name: "Foundation Directory",
          category: "Funding",
          description: "Private foundation opportunity records.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "configuration_required",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://foundationdirectory.example.test/",
          data_boundary: "Public metadata only.",
          capabilities: ["Awards"],
        },
        {
          id: "crossref",
          name: "Crossref",
          category: "Funding",
          description: "DOI metadata and scholarly work resolution.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "unavailable",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://www.crossref.org/services/metadata-delivery/rest-api/",
          data_boundary: "Public metadata only.",
          capabilities: ["DOI resolution"],
        },
      ],
    };
    render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );

    const needsConnectionCheckbox = screen.getByRole("checkbox", {
      name: /Foundation Directory/i,
    });
    const unavailableCheckbox = screen.getByRole("checkbox", { name: /Crossref/i });
    expect(needsConnectionCheckbox).toBeDisabled();
    expect(needsConnectionCheckbox).not.toBeChecked();
    expect(unavailableCheckbox).toBeDisabled();
    expect(unavailableCheckbox).not.toBeChecked();
    expect(screen.getByText("Needs connection setup")).toBeInTheDocument();
    expect(screen.getByText("Currently unavailable")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /red-team draft/i }));
    const fundingSources = onRun.mock.calls[0][2].inputs.sources;
    expect(fundingSources).toContain("grants_gov");
    expect(fundingSources).not.toContain("foundation_dir");
    expect(fundingSources).not.toContain("crossref");
  });

  it("removes a previously discoverable connector from Opportunity discovery once a test-connection refresh reports it non-runnable", async () => {
    // Regression: `fundingSources` selection alone must not keep a
    // connector listed as a searchable/usable opportunity source once its
    // live test_status stops being runnable. Simulates the connector
    // becoming unavailable after a "Test connection" refresh (a data prop
    // update, since the connector's readiness is server-derived) while it
    // remains present in the reviewer's already-selected fundingSources.
    const readyData: Pick<WorkspaceData, "connectors"> = {
      connectors: [workspaceData.connectors[0]],
    };
    const { rerender } = render(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={readyData as unknown as WorkspaceData}
      />,
    );

    const discoveryPanel = screen.getByLabelText("Opportunity discovery");
    expect(within(discoveryPanel).getByText("Grants.gov")).toBeInTheDocument();
    const fundingPanel = screen.getByLabelText("Funding source discovery");
    expect(
      within(fundingPanel).getByRole("checkbox", { name: /Grants\.gov/i }),
    ).toBeChecked();

    const nowUnavailableData: Pick<WorkspaceData, "connectors"> = {
      connectors: [
        { ...workspaceData.connectors[0], test_status: "unavailable" },
      ],
    };
    rerender(
      <GrantStudio
        result={grantResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={nowUnavailableData as unknown as WorkspaceData}
      />,
    );

    expect(
      within(discoveryPanel).queryByText("Grants.gov"),
    ).not.toBeInTheDocument();
    expect(
      within(discoveryPanel).getByText(
        /select at least one funding connector above/i,
      ),
    ).toBeInTheDocument();
    expect(
      within(fundingPanel).getByRole("checkbox", { name: /Grants\.gov/i }),
    ).toBeDisabled();
    expect(
      within(fundingPanel).getByRole("checkbox", { name: /Grants\.gov/i }),
    ).not.toBeChecked();
    expect(screen.getByText("Currently unavailable")).toBeInTheDocument();
  });

  it("keeps incomplete connector requests out of the draft list and shows mapped requirements without matching evidence", async () => {
    const user = setupUser();
    const sparseGrant = {
      ...grantResult,
      requirements: [
        {
          id: "aim-gap",
          text: "Specific aims alignment",
          category: "Narrative",
          status: "mapped",
          evidence_ids: ["missing-citation"],
        },
      ],
      citations: [
        {
          ...grantResult.citations[0],
          id: "other-citation",
        },
      ],
      sections: undefined,
    } as unknown as GrantStudioResult;
    render(
      <GrantStudio
        result={sparseGrant}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={workspaceData as unknown as WorkspaceData}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Request a new connector" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: /request a new connector/i,
    });
    const requestForm = within(dialog).getByRole("button", {
      name: "Save draft request",
    }).closest("form") as HTMLFormElement;
    await user.type(within(dialog).getByLabelText("Connector name"), "Only name");
    fireEvent.submit(requestForm);
    await user.type(
      within(dialog).getByLabelText("Base URL"),
      "https://connector.example.test",
    );
    fireEvent.submit(requestForm);
    await user.clear(within(dialog).getByLabelText("Connector name"));
    await user.type(
      within(dialog).getByLabelText("Justification"),
      "Need a validated connector path.",
    );
    fireEvent.submit(requestForm);
    expect(screen.queryByText("Only name")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /specific aims alignment/i }),
    );
    expect(
      screen.getByText(/no source evidence is linked to this requirement yet/i),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Significance" }));
    expect(screen.getByText(/not yet drafted for this section/i)).toBeInTheDocument();
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
    const user = setupUser();
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
    const user = setupUser();
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
    const user = setupUser();
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
        data={data as unknown as WorkspaceData}
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

  it("can opt into a newly assigned public source before running", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <MatchingStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        data={{
          connectors: [
            {
              id: "europe_pmc",
              name: "Europe PMC",
              category: "Discovery",
              description: "Public biomedical literature search.",
              auth_kind: "None",
              secret_status: "Not required",
              enabled: true,
              test_status: "ready",
              last_tested_at: null,
              assigned_agents: ["matching"],
              terms_url: "https://europepmc.org/",
              data_boundary: "Public metadata only.",
              capabilities: ["Search"],
            },
          ],
        } as unknown as WorkspaceData}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: "Europe PMC" }));
    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );
    expect(onRun.mock.calls[0][2].inputs.sources).toContain("europe_pmc");
  });

  it("explains empty states and submits revised criteria with online public research", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <MatchingStudio
        result={null}
        running={false}
        error="Need authorized criteria"
        onRun={onRun}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Need authorized criteria",
    );
    expect(
      screen.getByText(/no public connectors are assigned to matching yet/i),
    ).toBeInTheDocument();
    expect(screen.getByText("No shortlist yet")).toBeInTheDocument();
    expect(
      screen.getByText(/score components and their evidence appear here/i),
    ).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Expertise, method, or need"));
    await user.type(
      screen.getByLabelText("Expertise, method, or need"),
      "Find sequencing collaborators with patient engagement methods.",
    );
    await user.click(screen.getByRole("checkbox", { name: "Facilities" }));
    await user.click(
      screen.getByRole("checkbox", { name: "Institutional directory" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: /current public research/i }),
    );
    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );

    const inputs = onRun.mock.calls[0][2].inputs;
    expect(onRun.mock.calls[0][1]).toBe(
      "Find sequencing collaborators with patient engagement methods.",
    );
    expect(onRun.mock.calls[0][2].onlineResearch).toBe(true);
    expect(inputs.record_kinds).not.toContain("facility");
    expect(inputs.sources).not.toContain("institutional");
    expect(inputs.public_search_query).toBe(
      "Find sequencing collaborators with patient engagement methods.",
    );
  });

  it("selects alternate matches, surfaces disabled connectors, and hides comparison views", async () => {
    const user = setupUser();
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
        {
          id: "nih_reporter",
          name: "NIH Reporter",
          category: "Discovery",
          description: "NIH grants and people records.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: false,
          test_status: "error",
          last_tested_at: null,
          assigned_agents: ["matching"],
          terms_url: "https://reporter.nih.gov/",
          data_boundary: "Public metadata only.",
          capabilities: ["Awards"],
        },
      ],
    };
    render(
      <MatchingStudio
        result={matchingResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
      />,
    );

    expect(screen.getByText("Disabled in Settings")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /NIH Reporter/i })).toBeDisabled();

    const facilityCard = screen
      .getByText("Core Genomics Facility")
      .closest(".match-card") as HTMLElement;
    await user.click(
      within(facilityCard).getByRole("button", {
        name: /^Core Genomics Facility/i,
      }),
    );
    expect(
      screen.getByRole("heading", { name: "Core Genomics Facility" }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /add dr\. amara osei to shortlist/i }),
    );
    await user.click(
      screen.getByRole("button", {
        name: /add core genomics facility to shortlist/i,
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Compare shortlisted" }),
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Hide comparison" }),
    );
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: /remove core genomics facility from shortlist/i,
      }),
    );
    expect(screen.getByText("Shortlist (1)")).toBeInTheDocument();
  });

  it("maps configuration_required and unavailable test_status to distinct non-runnable states and excludes them from the run payload", async () => {
    const user = setupUser();
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
        {
          id: "ror",
          name: "ROR",
          category: "Identity",
          description: "Open identifiers for research organizations.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "configuration_required",
          last_tested_at: null,
          assigned_agents: ["matching"],
          terms_url: "https://ror.org/terms/",
          data_boundary: "Public metadata only.",
          capabilities: ["Organization resolution"],
        },
        {
          id: "orcid",
          name: "ORCID",
          category: "Identity",
          description: "Public researcher identifier records.",
          auth_kind: "None",
          secret_status: "Not required",
          enabled: true,
          test_status: "unavailable",
          last_tested_at: null,
          assigned_agents: ["matching"],
          terms_url: "https://info.orcid.org/terms-of-use/",
          data_boundary: "Public metadata only.",
          capabilities: ["Identity resolution"],
        },
      ],
    };
    render(
      <MatchingStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );

    const readyCheckbox = screen.getByRole("checkbox", { name: "OpenAlex" });
    const needsConnectionCheckbox = screen.getByRole("checkbox", {
      name: /^ROR/,
    });
    const unavailableCheckbox = screen.getByRole("checkbox", {
      name: /^ORCID/,
    });

    expect(readyCheckbox).toBeEnabled();
    expect(readyCheckbox).toBeChecked();
    expect(needsConnectionCheckbox).toBeDisabled();
    expect(needsConnectionCheckbox).not.toBeChecked();
    expect(unavailableCheckbox).toBeDisabled();
    expect(unavailableCheckbox).not.toBeChecked();
    expect(screen.getByText("Needs connection setup")).toBeInTheDocument();
    expect(screen.getByText("Currently unavailable")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );

    const inputs = onRun.mock.calls[0][2].inputs;
    expect(inputs.sources).toContain("openalex");
    expect(inputs.sources).not.toContain("ror");
    expect(inputs.sources).not.toContain("orcid");
  });

  it("re-adds hard filters and flags matches that still have hard-filter gaps", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const gapResult = {
      ...matchingResult,
      matches: [
        {
          ...matchingResult.matches[0],
          hard_filters_passed: false,
        },
      ],
    } as MatchingStudioResult;
    render(
      <MatchingStudio
        result={gapResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    expect(screen.getByText(/hard filter gap/i)).toBeInTheDocument();
    await user.click(
      screen.getByRole("checkbox", { name: "Source evidence available" }),
    );
    await user.click(
      screen.getByRole("checkbox", { name: "Source evidence available" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Build verified shortlist" }),
    );
    expect(onRun.mock.calls[0][2].inputs.hard_filters).toContain(
      "source_evidence_available",
    );
  });
});

describe("DatasetStudio", () => {
  const computedDatasetResult = {
    asset_name: "pilot-outcomes.csv",
    run: baseRun({ capability: "dataset" }),
    profile_status: "computed",
    row_count: 1200,
    column_count: 4,
    fields: [
      {
        name: "participant_id",
        data_type: "string",
        missing: 0,
        range_or_values: "1,200 unique IDs",
        unique: 1200,
      },
      {
        name: "response_score",
        data_type: "number",
        missing: 3,
        range_or_values: "0-100",
        unique: 98,
      },
    ],
    quality_findings: ["3 missing response scores"],
    profile_note: "Ready for bounded computation.",
    analysis_plan: [
      {
        id: "profile",
        question: "What are the core field ranges?",
        method: "Deterministic profile",
        status: "ready",
        deterministic: true,
      },
    ],
    interpretation: ["Scores trend higher in the intervention cohort."],
    compute_proposal: {
      adapter: "Foundry Code Interpreter",
      estimated_bytes: 2_500_000_000,
      estimated_cost_usd: 1.2,
      estimated_minutes: 4,
      stages: ["Validate schema", "Profile columns"],
      approval_required: false,
    },
    citations: [],
    insight: {
      agent_name: "Dataset analysis",
      content: "Computation remained within the approved boundary.",
      evidence_state: "verified",
      online_research_used: false,
      referenced_source_ids: [],
      unresolved_source_ids: [],
    },
  } as DatasetStudioResult;
  const estimateOnlyDatasetResult = {
    ...computedDatasetResult,
    profile_status: "estimated",
    profile_note: "Await plan approval before profiling.",
    fields: [],
    quality_findings: [],
    interpretation: [],
    compute_proposal: {
      ...computedDatasetResult.compute_proposal,
      approval_required: true,
    },
  } as DatasetStudioResult;

  it("validates a bounded CSV file, uploads it, and requires plan approval before profiling", async () => {
    const user = setupUser({ applyAccept: false });
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
    expect(inputs.analysis_approved).toBe(true);
    expect(inputs.data_classification).toBe("public_or_synthetic");
  });

  it("rejects an oversized file client-side without pretending to profile it", async () => {
    const user = setupUser();
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
      [new Array(100_001).fill("a").join("")],
      "huge.csv",
      { type: "text/csv" },
    );
    await user.upload(input, oversizedFile);
    expect(
      screen.getByText(/limited to 100 kb/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    ).toBeDisabled();
  });

  it("handles invalid files, upload failures, and large-asset approvals", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const onRefresh = jest.fn().mockResolvedValue(undefined);
    mockedUploadLibraryItem.mockRejectedValueOnce(new Error("Upload denied"));
    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
        onRefresh={onRefresh}
      />,
    );

    const input = screen.getByLabelText("Upload a dataset file");
    fireEvent.change(input, {
      target: {
        files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
      },
    });
    expect(screen.getByText(
      "Only .csv or .json files are supported here.",
    )).toBeInTheDocument();

    fireEvent.change(input, {
      target: {
        files: [
          new File(['{"ok":true}'], "sample.json", {
            type: "application/json",
          }),
        ],
      },
    });
    expect(
      screen.getByText(/json preview only uploads to library/i),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Upload to Library" }),
    );
    await waitFor(() =>
      expect(screen.getByText("Upload denied")).toBeInTheDocument(),
    );
    expect(onRefresh).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", {
        name: /clinical-events-archive\.parquet/i,
      }),
    );
    await user.clear(screen.getByLabelText("Analysis objective"));
    await user.type(
      screen.getByLabelText("Analysis objective"),
      "Estimate the compute path for the clinical archive.",
    );
    await user.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    await user.click(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    );
    expect(onRun.mock.calls[0][1]).toBe(
      "Estimate the compute path for the clinical archive.",
    );
    expect(onRun.mock.calls[0][2].inputs).toMatchObject({
      filename: "clinical-events-archive.parquet",
      estimated_bytes: 1_200_000_000_000,
      analysis_approved: true,
    });

    await user.click(
      screen.getByRole("button", { name: /pilot-outcomes\.csv/i }),
    );
    expect(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    ).toBeDisabled();
  });

  it("renders computed and estimate-only dataset details", async () => {
    const { rerender } = render(
      <DatasetStudio
        result={computedDatasetResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("participant_id")).toBeInTheDocument();
    expect(screen.getByText("3 missing response scores")).toBeInTheDocument();
    expect(
      screen.getByText("Scores trend higher in the intervention cohort."),
    ).toBeInTheDocument();
    expect(screen.getByText("Foundry Code Interpreter")).toBeInTheDocument();
    expect(screen.getByText("2.5 GB")).toBeInTheDocument();
    expect(
      screen.getByText("Safe for bounded local computation"),
    ).toBeInTheDocument();
    // ResearchMarkdown is lazy-loaded (React.lazy + dynamic import), so its
    // Suspense fallback needs at least one microtask flush before the mocked
    // component appears -- findBy* awaits that instead of asserting
    // synchronously against the not-yet-resolved fallback.
    expect(
      await screen.findByTestId("research-markdown"),
    ).toHaveTextContent("Computation remained within the approved boundary.");

    rerender(
      <DatasetStudio
        result={estimateOnlyDatasetResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("Asset not profiled")).toBeInTheDocument();
    expect(screen.getByText("Await plan approval before profiling.")).toBeInTheDocument();
    expect(
      screen.getByText("Human approval required before submit"),
    ).toBeInTheDocument();
  });

  it("uses sample defaults and generic upload failure messaging", async () => {
    const user = setupUser({ applyAccept: false });
    const onRun = jest.fn().mockResolvedValue(undefined);
    mockedUploadLibraryItem.mockRejectedValueOnce("denied");
    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(['{"ok":true}'], "sample.json", {
            type: "application/json",
          }),
        ],
      },
    });
    await user.click(screen.getByRole("button", { name: "Upload to Library" }));
    await waitFor(() =>
      expect(screen.getByText("Upload to Library failed.")).toBeInTheDocument(),
    );

    await user.click(
      screen.getByRole("button", { name: /pilot-outcomes\.csv/i }),
    );
    await user.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    await user.click(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    );
    expect(onRun.mock.calls[0][2].inputs).toMatchObject({
      filename: "pilot-outcomes.csv",
      estimated_bytes: 4_000_000,
      compute_adapter_configured: true,
      analysis_approved: true,
    });
  });

  it("guards disabled dataset submits and renders missing compute estimates honestly", async () => {
    const onRun = jest.fn().mockResolvedValue(undefined);
    const estimatedUnknowns = {
      ...computedDatasetResult,
      compute_proposal: {
        ...computedDatasetResult.compute_proposal,
        estimated_cost_usd: null,
        estimated_minutes: null,
      },
    } as unknown as DatasetStudioResult;
    const { container, rerender } = render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    fireEvent.submit(container.querySelector("form") as HTMLFormElement);
    expect(onRun).not.toHaveBeenCalled();

    rerender(
      <DatasetStudio
        result={estimatedUnknowns}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("$—")).toBeInTheDocument();
    expect(screen.getByText("— min")).toBeInTheDocument();
  });

  it("handles canceled uploads and non-text CSV previews, and never enables Run for a JSON upload", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: {
          result: ArrayBuffer;
          onload: ((event: ProgressEvent<FileReader>) => void) | null;
          readAsText: () => void;
        } = {
          result: new ArrayBuffer(8),
          onload: null,
          readAsText() {
            reader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
          },
        };
        return reader as unknown as FileReader;
      });
    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: { files: [] },
    });

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [new File(["a,b\n1,2"], "binary.csv", { type: "text/csv" })],
      },
    });
    expect(screen.getByText("binary.csv")).toBeInTheDocument();

    // A JSON upload never reads bytes (no FileReader is invoked for it) and
    // must never enable Run: the request payload would omit `csv_text`
    // entirely, contradicting the visible "JSON preview only uploads to
    // Library" copy. Approving the plan alone must not be enough.
    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(['{"ok":true}'], "sample.json", {
            type: "application/json",
          }),
        ],
      },
    });
    await user.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    expect(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", {
        name: "Analyze with Foundry Code Interpreter",
      }),
    );
    expect(onRun).not.toHaveBeenCalled();
    fileReaderSpy.mockRestore();
  });

  it("[pw.dataset-upload:reading] shows a reading status and disables analysis until the CSV read resolves", async () => {
    let deliverLoad: (() => void) | null = null;
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: {
          result: string;
          onload: ((event: ProgressEvent<FileReader>) => void) | null;
          onerror: ((event: ProgressEvent<FileReader>) => void) | null;
          readAsText: () => void;
        } = {
          result: "a,b\n1,2",
          onload: null,
          onerror: null,
          readAsText() {
            deliverLoad = () =>
              reader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
          },
        };
        return reader as unknown as FileReader;
      });

    render(
      <DatasetStudio result={null} running={false} error={null} onRun={jest.fn()} />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [new File(["a,b\n1,2"], "pending.csv", { type: "text/csv" })],
      },
    });

    const tile = screen
      .getByText("pending.csv")
      .closest(".asset-upload-tile") as HTMLElement;
    expect(tile).toHaveAttribute("data-read-status", "reading");
    expect(screen.getByText(/reading csv/i)).toBeInTheDocument();

    const runButton = screen.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    fireEvent.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    expect(runButton).toBeDisabled();

    await act(async () => {
      deliverLoad?.();
    });

    expect(tile).toHaveAttribute("data-read-status", "ready");
    expect(runButton).toBeEnabled();
    fileReaderSpy.mockRestore();
  });

  it("[pw.dataset-upload:error] surfaces a read error and keeps analysis disabled", async () => {
    let deliverError: (() => void) | null = null;
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: {
          result: string | null;
          onload: ((event: ProgressEvent<FileReader>) => void) | null;
          onerror: ((event: ProgressEvent<FileReader>) => void) | null;
          readAsText: () => void;
        } = {
          result: null,
          onload: null,
          onerror: null,
          readAsText() {
            deliverError = () =>
              reader.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>);
          },
        };
        return reader as unknown as FileReader;
      });
    const onRun = jest.fn();

    render(
      <DatasetStudio result={null} running={false} error={null} onRun={onRun} />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [new File(["a,b\n1,2"], "broken.csv", { type: "text/csv" })],
      },
    });

    const tile = screen
      .getByText("broken.csv")
      .closest(".asset-upload-tile") as HTMLElement;
    expect(tile).toHaveAttribute("data-read-status", "reading");

    await act(async () => {
      deliverError?.();
    });

    expect(tile).toHaveAttribute("data-read-status", "error");
    expect(
      screen.getByText(/this csv file could not be read/i),
    ).toBeInTheDocument();
    const runButton = screen.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    // Real defect fix: runDisabled previously only checked
    // csvReadStatus === "reading", so approving the plan after a failed CSV
    // read left the button enabled and a run could be submitted with no
    // csv_text at all. csvReadStatus "error" must block the run exactly
    // like "reading" does, until a newly-selected file reaches "ready".
    fireEvent.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    expect(runButton).toBeDisabled();
    // Defense in depth: the form's onSubmit guard must also refuse to fire
    // onRun even if a disabled button were somehow bypassed (e.g. Enter key).
    fireEvent.submit(runButton.closest("form") as HTMLFormElement);
    expect(onRun).not.toHaveBeenCalled();
    fileReaderSpy.mockRestore();
  });

  it("[pw.dataset-upload:error] treats a zero-byte CSV file as a read error instead of silently reaching ready", async () => {
    // Real defect fix: FileReader succeeds with `result === ""` for an empty
    // file, and `csvText ? {csv_text: csvText} : {}` treated that empty
    // string as falsy -- silently omitting `csv_text` from the submitted
    // payload while csvReadStatus still became "ready" and the RunButton
    // enabled, so the UI claimed the asset was ready when the backend would
    // never receive any CSV content at all. Empty content must be surfaced
    // as an explicit read error, never as "ready".
    let deliverLoad: (() => void) | null = null;
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: {
          result: string;
          onload: ((event: ProgressEvent<FileReader>) => void) | null;
          onerror: ((event: ProgressEvent<FileReader>) => void) | null;
          readAsText: () => void;
        } = {
          result: "",
          onload: null,
          onerror: null,
          readAsText() {
            deliverLoad = () =>
              reader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
          },
        };
        return reader as unknown as FileReader;
      });
    const onRun = jest.fn();

    render(
      <DatasetStudio result={null} running={false} error={null} onRun={onRun} />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [new File([""], "empty.csv", { type: "text/csv" })],
      },
    });

    const tile = screen
      .getByText("empty.csv")
      .closest(".asset-upload-tile") as HTMLElement;
    expect(tile).toHaveAttribute("data-read-status", "reading");

    await act(async () => {
      deliverLoad?.();
    });

    expect(tile).toHaveAttribute("data-read-status", "error");
    expect(screen.getByText(/this csv file is empty/i)).toBeInTheDocument();

    const runButton = screen.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    fireEvent.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    expect(runButton).toBeDisabled();
    fireEvent.submit(runButton.closest("form") as HTMLFormElement);
    expect(onRun).not.toHaveBeenCalled();
    fileReaderSpy.mockRestore();
  });

  it("[pw.dataset-upload:reading] ignores a stale reader when rapid reselection supersedes it before the first read resolves", async () => {
    type MockReader = {
      result: string | null;
      onload: ((event: ProgressEvent<FileReader>) => void) | null;
      onerror: ((event: ProgressEvent<FileReader>) => void) | null;
      onabort: (() => void) | null;
      abort: jest.Mock;
      readAsText: jest.Mock;
    };
    const readers: MockReader[] = [];
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: MockReader = {
          result: null,
          onload: null,
          onerror: null,
          onabort: null,
          abort: jest.fn(),
          readAsText: jest.fn(),
        };
        readers.push(reader);
        return reader as unknown as FileReader;
      });
    const onRun = jest.fn().mockResolvedValue(undefined);

    render(
      <DatasetStudio result={null} running={false} error={null} onRun={onRun} />,
    );

    // Select the first file; its reader is created but deliberately never
    // delivered yet (readAsText is a no-op stub), simulating a slow read.
    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(["id,outcome\n1,first-file-content\n"], "first.csv", {
            type: "text/csv",
          }),
        ],
      },
    });

    // Rapidly reselect a different file before the first read resolves.
    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(["id,outcome\n2,second-file-content\n"], "second.csv", {
            type: "text/csv",
          }),
        ],
      },
    });

    expect(readers).toHaveLength(2);
    const [staleReader, currentReader] = readers;
    // The superseded reader is aborted as a defense-in-depth measure.
    expect(staleReader.abort).toHaveBeenCalledTimes(1);

    const tile = screen
      .getByText("second.csv")
      .closest(".asset-upload-tile") as HTMLElement;
    expect(tile).toHaveAttribute("data-read-status", "reading");

    // The newer (current) reader resolves first.
    currentReader.result = "id,outcome\n2,second-file-content\n";
    await act(async () => {
      currentReader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
    });
    expect(tile).toHaveAttribute("data-read-status", "ready");

    // The stale first reader resolves out of order, after abort() was
    // already called on it. Its onload must still be ignored even though it
    // fires, proving the generation guard — not just abort() — protects
    // state.
    staleReader.result = "id,outcome\n1,first-file-content\n";
    await act(async () => {
      staleReader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
    });

    expect(tile).toHaveAttribute("data-read-status", "ready");
    expect(screen.getByText("second.csv")).toBeInTheDocument();

    fireEvent.click(
      screen.getByLabelText(
        /i approve sending this bounded dataset to the foundry dataset agent/i,
      ),
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Analyze with Foundry Code Interpreter",
        }),
      );
    });

    expect(onRun).toHaveBeenCalledTimes(1);
    const inputs = onRun.mock.calls[0][2].inputs as {
      filename: string;
      csv_text?: string;
    };
    expect(inputs.filename).toBe("second.csv");
    expect(inputs.csv_text).toContain("second-file-content");
    expect(inputs.csv_text).not.toContain("first-file-content");

    fileReaderSpy.mockRestore();
  });

  it("[pw.dataset-upload:reading] ignores a stale reader's onerror after a newer file supersedes it", async () => {
    type MockReader = {
      result: string | null;
      onload: ((event: ProgressEvent<FileReader>) => void) | null;
      onerror: ((event: ProgressEvent<FileReader>) => void) | null;
      abort: jest.Mock;
      readAsText: jest.Mock;
    };
    const readers: MockReader[] = [];
    const fileReaderSpy = jest
      .spyOn(window, "FileReader")
      .mockImplementation(() => {
        const reader: MockReader = {
          result: null,
          onload: null,
          onerror: null,
          abort: jest.fn(),
          readAsText: jest.fn(),
        };
        readers.push(reader);
        return reader as unknown as FileReader;
      });

    render(
      <DatasetStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(["id,outcome\n1,first-file-content\n"], "first.csv", {
            type: "text/csv",
          }),
        ],
      },
    });
    fireEvent.change(screen.getByLabelText("Upload a dataset file"), {
      target: {
        files: [
          new File(["id,outcome\n2,second-file-content\n"], "second.csv", {
            type: "text/csv",
          }),
        ],
      },
    });

    expect(readers).toHaveLength(2);
    const [staleReader, currentReader] = readers;

    const tile = screen
      .getByText("second.csv")
      .closest(".asset-upload-tile") as HTMLElement;

    currentReader.result = "id,outcome\n2,second-file-content\n";
    await act(async () => {
      currentReader.onload?.(new ProgressEvent("load") as ProgressEvent<FileReader>);
    });
    expect(tile).toHaveAttribute("data-read-status", "ready");

    // The stale first reader fails out of order after being superseded; the
    // guard must ignore its onerror too, so no error banner appears and the
    // newer file's "ready" status/content stand untouched.
    await act(async () => {
      staleReader.onerror?.(new ProgressEvent("error") as ProgressEvent<FileReader>);
    });

    expect(tile).toHaveAttribute("data-read-status", "ready");
    expect(screen.getByText("second.csv")).toBeInTheDocument();
    expect(
      screen.queryByText(/this csv file could not be read/i),
    ).not.toBeInTheDocument();

    fileReaderSpy.mockRestore();
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
    const user = setupUser();
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
    const user = setupUser();
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
    expect(within(dialog).getByText("12–13")).toBeInTheDocument();
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

  it("submits updated questions and renders abstentions with version history", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const abstainedResult = {
      ...institutionalResult,
      abstained: true,
      answer: null,
      citations: [
        {
          ...institutionalResult.citations[0],
          page_end: 13,
        },
      ],
      conflicts: [
        {
          topic: "Retention timing",
          description: "Superseded guidance conflicts with the current handbook.",
        },
      ],
      escalation: "Escalate to research compliance counsel.",
      versions: [
        {
          source_id: "irb-handbook",
          title: "IRB Handbook",
          version: "2.0",
          effective_date: "2026-01-01",
          status: "effective",
        },
      ],
      insight: {
        agent_name: "Institutional QA",
        content: "The corpus abstained until counsel confirms the retained wording.",
        evidence_state: "insufficient",
        online_research_used: false,
        referenced_source_ids: ["irb-handbook"],
        unresolved_source_ids: [],
      },
    } as unknown as InstitutionalStudioResult;
    render(
      <InstitutionalStudio
        result={abstainedResult}
        running={false}
        error="Corpus conflict detected"
        onRun={onRun}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Corpus conflict detected",
    );
    await user.clear(screen.getByLabelText("Institutional question"));
    await user.type(
      screen.getByLabelText("Institutional question"),
      "Which policy version governs AI disclosure for oncology studies?",
    );
    await user.click(
      screen.getByRole("button", { name: "Resolve policy answer" }),
    );

    expect(onRun.mock.calls[0][1]).toBe(
      "Which policy version governs AI disclosure for oncology studies?",
    );
    expect(screen.getByText("Answer gap")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The authorized corpus does not support a reliable answer.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Escalate to research compliance counsel.")).toBeInTheDocument();
    expect(screen.getByText("Retention timing")).toBeInTheDocument();
    expect(screen.getByText("IRB Handbook")).toBeInTheDocument();
    expect(screen.getByText(/v2\.0 · Effective 2026-01-01/i)).toBeInTheDocument();
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "The corpus abstained until counsel confirms the retained wording.",
    );

    await user.click(
      screen.getByRole("button", { name: /IRB Handbook · Section 4\.2/i }),
    );
    const dialog = screen.getByRole("dialog", { name: "IRB Handbook" });
    expect(within(dialog).getByText("12–13")).toBeInTheDocument();
    expect(within(dialog).getByText("irb-handbook")).toBeInTheDocument();
    await user.click(within(dialog).getByLabelText("Close evidence detail"));
    expect(
      screen.queryByRole("dialog", { name: "IRB Handbook" }),
    ).not.toBeInTheDocument();
  });

  it("renders citation detail without a page range when only a start page is available", async () => {
    const user = setupUser();
    render(
      <InstitutionalStudio
        result={{
          ...institutionalResult,
          citations: [
            {
              ...institutionalResult.citations[0],
              page_start: null,
              page_end: undefined,
            },
          ],
        } as unknown as InstitutionalStudioResult}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /IRB Handbook · Section 4\.2/i }),
    );
    const dialog = screen.getByRole("dialog", { name: "IRB Handbook" });
    expect(within(dialog).getByText("—")).toBeInTheDocument();
  });
});

describe("AutomationStudio", () => {
  const automationResult: AutomationStudioResult = {
    run: baseRun({ capability: "orchestration" }),
    template_id: "evidence-review-v2",
    trigger: "Manual",
    // Matches AUTOMATION_TEMPLATES[0] ("evidence-review-v2")'s full,
    // unedited default step graph exactly, so this fixture represents a
    // genuine passing dry run *for the graph currently on screen* rather
    // than for some other, smaller graph -- see the "gates activation"
    // test below for why that distinction matters.
    steps: [
      {
        id: "ingest",
        label: "Ingest & verify",
        kind: "activity",
        depends_on: [],
        retry_limit: 3,
        approval_required: false,
      },
      {
        id: "retrieve",
        label: "Retrieve evidence",
        kind: "fan_out",
        depends_on: ["ingest"],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "synthesize",
        label: "Synthesize",
        kind: "agent",
        depends_on: ["retrieve"],
        retry_limit: 1,
        approval_required: false,
      },
      {
        id: "review",
        label: "Human review",
        kind: "approval",
        depends_on: ["synthesize"],
        retry_limit: 0,
        approval_required: true,
      },
      {
        id: "export",
        label: "Export",
        kind: "external_action",
        depends_on: ["review"],
        retry_limit: 2,
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
    const user = setupUser();
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
    const user = setupUser();
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
    await user.keyboard("{Enter}");
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
    await user.keyboard("{Enter}");

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
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    // A passed result the parent already happens to be holding at mount
    // does not by itself authorize activation: nothing in *this* session
    // has confirmed it corresponds to the currently displayed graph, so it
    // starts disabled until an explicit dry run is (re-)run here.
    expect(activateButton).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(onRun).toHaveBeenCalledTimes(1);
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

  it("moves focus into the dialog on open, contains Tab within it, restores focus to the trigger on close, and closes (without activating) on Escape", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    const closeButton = within(dialog).getByLabelText(
      "Close activation dialog",
    );
    // Focus enters the dialog as soon as it opens, landing on its close
    // button -- a keyboard/screen-reader user is never left stranded with
    // focus on a background element the dialog now visually covers.
    expect(closeButton).toHaveFocus();

    const cancelButton = within(dialog).getByRole("button", {
      name: "Cancel",
    });
    const confirmButton = within(dialog).getByRole("button", {
      name: "Confirm activation",
    });

    // Shift+Tab from the first focusable element (close button) wraps
    // around to the last (Confirm activation) instead of escaping the
    // dialog into the (inert) background page.
    await user.tab({ shift: true });
    expect(confirmButton).toHaveFocus();

    // Tab from the last focusable element wraps back to the first (close
    // button), keeping keyboard focus fully contained within the dialog.
    await user.tab();
    expect(closeButton).toHaveFocus();

    // Ordinary, non-wrapping Tab navigation between the wrap points still
    // works exactly as expected.
    await user.tab();
    expect(cancelButton).toHaveFocus();
    await user.tab();
    expect(confirmButton).toHaveFocus();

    // Escape behaves exactly like Cancel/the close button: it dismisses the
    // dialog but never activates.
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /activated \(draft workspace\)/i }),
    ).not.toBeInTheDocument();
    // Focus is restored to the exact trigger element that opened the
    // dialog, not merely somewhere on the page.
    expect(activateButton).toHaveFocus();
  });

  it("moves focus to the activation status instead of the now-disabled trigger after a successful activation", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm activation" }),
    );

    // A *successful* activation disables the trigger that opened the dialog.
    // Restoring focus to it would be a no-op in a real browser (disabled
    // elements refuse focus), silently dumping the keyboard user back on
    // document.body with nothing announced. Focus must land on a real,
    // focusable, relevant element instead.
    const disabledTrigger = screen.getByRole("button", {
      name: /activated \(draft workspace\)/i,
    });
    expect(disabledTrigger).toBeDisabled();
    expect(disabledTrigger).not.toHaveFocus();
    expect(document.body).not.toHaveFocus();

    const status = screen.getByTestId("workflow-activation-status");
    expect(status).toHaveFocus();
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent(/workflow activated for this draft workspace/i);
  });

  it("suppresses global shell shortcuts while the activation dialog is open and releases them when it closes", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    // Stands in for research-workbench.tsx's `window` keydown listener, which
    // is what turns Ctrl/Cmd+K into a command palette. It is registered on
    // `window`, above the portalled dialog in the DOM, so it would fire for
    // keystrokes made inside the dialog unless the dialog stops them.
    const shellShortcut = jest.fn();
    window.addEventListener("keydown", shellShortcut);

    try {
      render(
        <AutomationStudio
          result={automationResult}
          running={false}
          error={null}
          onRun={onRun}
        />,
      );

      expect(isBlockingModalOpen()).toBe(false);

      await user.click(
        screen.getByRole("button", { name: "Validate & dry run" }),
      );
      await user.click(
        screen.getByRole("button", { name: "Activate after approval" }),
      );

      // The shell is told to suppress itself for as long as the dialog lives.
      expect(isBlockingModalOpen()).toBe(true);

      shellShortcut.mockClear();
      await user.keyboard("{Control>}k{/Control}");
      // Independent of the shell's own guard: the keystroke never reaches
      // `window` at all, so a command palette cannot open on top of this
      // dialog even if the shell forgot to check.
      expect(shellShortcut).not.toHaveBeenCalled();
      expect(
        screen.getByRole("dialog", { name: /activate graph/i }),
      ).toBeInTheDocument();

      await user.keyboard("{Escape}");
      expect(
        screen.queryByRole("dialog", { name: /activate graph/i }),
      ).not.toBeInTheDocument();
      // Suppression is scoped to the dialog's lifetime, not left latched on.
      expect(isBlockingModalOpen()).toBe(false);

      shellShortcut.mockClear();
      await user.keyboard("{Control>}k{/Control}");
      expect(shellShortcut).toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", shellShortcut);
    }
  });

  it("invalidates a passing dry run after edits and while revalidation is pending or errored", async () => {
    const user = setupUser();
    let resolveRun!: () => void;
    const onRun = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const { rerender } = render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    // A passed result the parent already holds at mount does not by
    // itself authorize activation until this session runs its own dry run.
    expect(activateButton).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(
      screen.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    expect(activateButton).toBeDisabled();
    await act(async () => resolveRun());
    expect(activateButton).toBeEnabled();

    // Editing the configuration after a pass immediately invalidates it.
    await user.selectOptions(screen.getByLabelText("Trigger"), "GitHub");
    expect(activateButton).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeDisabled();
    await act(async () => resolveRun());
    // The parent hasn't applied an updated result yet -- the studio still
    // only has the stale, pre-edit "Manual" result -- so a resolved dry
    // run with no matching server-echoed content still does not enable
    // activation. A mismatched/stale result stays disabled.
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    // Once the parent applies the exact server-echoed result for the
    // configuration currently on screen, activation is enabled again.
    expect(activateButton).toBeEnabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running={false}
        error="Validation transport failed"
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running
        error={null}
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{
          ...automationResult,
          trigger: "GitHub",
          validation_errors: ["Approval policy is incomplete."],
        }}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();
  });

  it("does not re-enable activation when an edit is reverted back to the last-validated content without a fresh dry run", async () => {
    // Regression for an edit-away-then-edit-back fingerprint bypass: the
    // activation gate must track *which draft version* was actually dry
    // run, not just whether the current content happens to match the last
    // validated content again. An edit that is undone (reverted to
    // byte-identical configuration) must still require a new dry run
    // before activation is allowed.
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();

    // Edit away from the validated configuration...
    await user.selectOptions(screen.getByLabelText("Trigger"), "GitHub");
    expect(activateButton).toBeDisabled();

    // ...then edit back to byte-identical content ("Manual", matching the
    // still-current `automationResult` prop) without ever running a new
    // dry run. Content equality alone must not be enough to re-enable
    // activation.
    await user.selectOptions(screen.getByLabelText("Trigger"), "Manual");
    expect(activateButton).toBeDisabled();
  });

  it("cannot activate through a stale-open confirmation dialog after an edit invalidates the gate while it is open", async () => {
    // Regression: the "Confirm activation" button must recheck the gate at
    // confirm time, not just trust that it was valid when the dialog was
    // opened. Opening the dialog and then invalidating the draft (an edit,
    // here removing a step) before pressing "Confirm activation" must not
    // activate.
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);
    const dialog = screen.getByRole("dialog", { name: /activate graph/i });

    // Invalidate the gate while the confirmation dialog is still open.
    await user.click(
      screen.getByRole("button", { name: "Remove Export" }),
    );

    const confirmButton = within(dialog).getByRole("button", {
      name: "Confirm activation",
    });
    expect(confirmButton).toBeDisabled();

    // A disabled button never dispatches a real click (both in real
    // browsers and jsdom), so merely confirming the button stays disabled
    // does not by itself prove the handler's own `if (!canActivate) return`
    // recheck actually blocks activation -- it would pass identically if
    // that guard were deleted. Force a genuine `click` event through
    // (bypassing both the DOM `disabled` property and React's own internal
    // disabled bookkeeping) so the click is actually dispatched and reaches
    // the production handler, which still holds the real, now-invalidated
    // `canActivate = false` in its last-committed closure. If the
    // handler-level guard were removed, this would activate; because it is
    // present, it must still refuse.
    forceClickBypassingReactDisabled(confirmButton);

    expect(
      screen.queryByRole("button", { name: /activated \(draft workspace\)/i }),
    ).not.toBeInTheDocument();
    expect(activateButton).toBeDisabled();
  });

  it("adds only an authorized capability catalog entry to the graph and blocks an unauthorized one", async () => {
    const user = setupUser();
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
        data={data as unknown as WorkspaceData}
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

  it("closes a manual draft when catalog additions reach the workflow step limit", async () => {
    const user = setupUser();
    const data = {
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
      ],
      connectors: [],
      runs: [],
    } as unknown as WorkspaceData;

    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Stale ninth step");
    const staleCommit = screen.getByRole("button", { name: "Add" });
    expect(staleCommit).toBeEnabled();

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    for (const label of [
      "Literature synthesis",
      "Literature Studio",
      "Grant Studio",
    ]) {
      const catalogRow = within(catalog)
        .getByText(label)
        .closest(".step-editor-row") as HTMLElement;
      await user.click(
        within(catalogRow).getByRole("button", { name: "Add to graph" }),
      );
    }

    expect(screen.getByRole("heading", { name: "Steps (8/8)" })).toBeInTheDocument();
    expect(staleCommit).not.toBeInTheDocument();
    expect(screen.queryByText("Stale ninth step")).not.toBeInTheDocument();
  });

  it("manages workflow runs by inspecting via existing Runs state and cloning a fresh draft", async () => {
    const user = setupUser();
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
        data={data as unknown as WorkspaceData}
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

  it("submits updated templates, toggles catalog previews, and shows validation failures", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const failedResult = {
      ...automationResult,
      dry_run_status: "failed",
      validation_errors: ["Review step depends on missing evidence output."],
      insight: {
        agent_name: "Workflow automation",
        content: "Dry run failed before any external action was enabled.",
        evidence_state: "verified",
        online_research_used: false,
        referenced_source_ids: [],
        unresolved_source_ids: [],
      },
    } as AutomationStudioResult;
    render(
      <AutomationStudio
        result={failedResult}
        running={false}
        error="Dry run failed"
        onRun={onRun}
        data={{ agents: [], connectors: [], runs: [] } as unknown as WorkspaceData}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Dry run failed");
    await user.click(screen.getByRole("button", { name: /Grant red team/i }));
    expect(screen.getAllByText("Parse notice")).toHaveLength(2);
    expect(screen.queryByText("Ingest & verify")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Trigger"), "Schedule");
    await user.click(
      screen.getByRole("button", { name: "Preview Literature Studio" }),
    );
    expect(
      screen.getByText("Search, screen, extract, and synthesize evidence."),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Preview Literature Studio" }),
    );
    expect(
      screen.queryByText("Search, screen, extract, and synthesize evidence."),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );

    expect(onRun.mock.calls[0][2].inputs).toMatchObject({
      template_id: "grant-review-v2",
      trigger: "Schedule",
    });
    expect(onRun.mock.calls[0][2].inputs.steps).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "parse-notice", label: "Parse notice" }),
        expect.objectContaining({
          id: "approve-submission",
          approval_required: true,
        }),
      ]),
    );
    expect(
      screen.getByText("Review step depends on missing evidence output."),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "Dry run failed before any external action was enabled.",
    );
  });

  it("dismisses the activation dialog via both the close button and Cancel", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    // Establish a genuine local pass (matching content, matching draft
    // version) before exercising the activation dialog -- this test isn't
    // about the fingerprint/version gate itself, just the dialog UI, so
    // get past the gate the same way a real user would.
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);
    let dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(within(dialog).getByLabelText("Close activation dialog"));
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();

    await user.click(activateButton);
    dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps a step draft unsubmittable while its label is empty and discards it on cancel", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(
      within(stepEditor).getByRole("button", { name: "Add" }),
    ).toBeDisabled();
    await user.click(screen.getByLabelText("Step label"));
    await user.keyboard("{Enter}");
    expect(
      within(stepEditor).getByRole("button", { name: "Add" }),
    ).toBeDisabled();
    await user.click(within(stepEditor).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: /^Add$/ })).not.toBeInTheDocument();
  });

  it("adds a configured step, discards an edit on cancel, and removes the step", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Temporary export");
    await user.selectOptions(screen.getByLabelText("Kind"), "external_action");
    await user.click(screen.getByRole("checkbox", { name: "Ingest & verify" }));
    await user.click(screen.getByRole("checkbox", { name: "Ingest & verify" }));
    await user.click(screen.getByRole("checkbox", { name: "Approval required" }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(
      screen.getByRole("button", { name: "Configure Temporary export" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/external action · depends on none · 1 retries · approval gate/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Configure Temporary export" }),
    );
    await user.selectOptions(screen.getByLabelText("Kind"), "agent");
    await user.click(screen.getByRole("checkbox", { name: "Approval required" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      screen.getByText(/external action · depends on none · 1 retries · approval gate/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Remove Temporary export" }),
    );
    expect(screen.queryByText("Temporary export")).not.toBeInTheDocument();
  });

  it("catalogs connector tools and shows the active graph version for current runs", async () => {
    const user = setupUser();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [],
      connectors: [
        {
          id: "onedrive_export",
          name: "OneDrive Export",
          category: "Storage",
          description: "Export validated artifacts.",
          auth_kind: "OAuth",
          secret_status: "Configured",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/export",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "unready_export",
          name: "Unready Export",
          category: "Storage",
          description: "Not yet proven ready.",
          auth_kind: "OAuth",
          secret_status: "Configured",
          enabled: true,
          test_status: "error",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/unready",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "grant_only_export",
          name: "Grant-only Export",
          category: "Storage",
          description: "Ready but assigned outside orchestration.",
          auth_kind: "OAuth",
          secret_status: "Configured",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://example.test/grant-only",
          data_boundary: "Grant outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "ready_with_key_export",
          name: "Ready-with-key Export",
          category: "Storage",
          description: "Runnable using a provided API key.",
          auth_kind: "ApiKey",
          secret_status: "Configured",
          enabled: true,
          test_status: "ready_with_key",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/ready-with-key",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
      ],
      runs: [
        {
          ...automationResult.run,
          artifact_count: 0,
          capability: "orchestration",
          estimated_cost_usd: 0,
          project_id: "demo-project",
          scheduler_managed: false,
          scheduling_state: "not_managed",
          started_at: "2026-07-16T12:00:00Z",
          title: "Validated graph",
        },
      ],
    };
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
      />,
    );

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const toolRow = within(catalog)
      .getByText("OneDrive Export")
      .closest(".step-editor-row") as HTMLElement;
    await user.click(
      within(toolRow).getByRole("button", { name: "Preview OneDrive Export" }),
    );
    expect(
      within(toolRow).getByText("Export validated artifacts."),
    ).toBeInTheDocument();
    await user.click(within(toolRow).getByRole("button", { name: "Add to graph" }));
    expect(
      screen.getByRole("button", { name: "Remove OneDrive Export" }),
    ).toBeInTheDocument();
    const unreadyRow = within(catalog)
      .getByText("Unready Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(unreadyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
    const grantOnlyRow = within(catalog)
      .getByText("Grant-only Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(grantOnlyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
    // Regression: a "ready_with_key" connector (API-key-backed, not OAuth) is
    // a genuinely runnable status per the shared isConnectorRunnable/
    // connectorAvailability helper, not just "ready" — the catalog's
    // authorization check must recognize it, not silently exclude it via an
    // inline `test_status === "ready"` comparison.
    const readyWithKeyRow = within(catalog)
      .getByText("Ready-with-key Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(readyWithKeyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeEnabled();

    const runManager = screen.getByRole("region", {
      name: "Workflow run management",
    });
    expect(within(runManager).getByText("Validated graph")).toBeInTheDocument();
    expect(within(runManager).getByText(/Graph 2\.0/)).toBeInTheDocument();
  });

  // Deliberately one test despite covering three concerns: each phase below
  // consumes the graph state the previous phase produced. The single-step
  // assertions are only reachable after the removal sequence, and the
  // activation-fallback assertions need the local draft to match that reduced
  // graph -- neither state can be constructed from props, so splitting would
  // mean re-performing the removals in each test and doing strictly more
  // total work. The 15s budget reflects genuine sequential UI work, not a
  // timeout raised to paper over contention; this is not the test that
  // flaked (that one ran on the 5s default and has been split).
  it(
    "surfaces capacity, one-step, and activation fallback states without weakening guards",
    async () => {
    const user = setupUser();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [
        {
          id: "literature-agent",
          name: "Literature synthesis",
          model_tier: "Primary",
          status: "Active",
          web_access: "Opt-in public only",
          workflow_steps: ["Protocol"],
          deployment: "Foundry Hosted Agent",
        },
      ],
      connectors: [],
      runs: [],
    };
    let resolveRun!: () => void;
    const onRun = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const { rerender } = render(
      <AutomationStudio
        result={{
          ...automationResult,
          graph_version: undefined,
          graph_hash: "",
        } as unknown as AutomationStudioResult}
        running={false}
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );

    for (const label of ["Stage six", "Stage seven", "Stage eight"]) {
      await user.click(screen.getByRole("button", { name: "Add step" }));
      await user.type(screen.getByLabelText("Step label"), label);
      await user.click(screen.getByRole("button", { name: "Add" }));
    }

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const cappedAddButton = within(catalog).getAllByRole("button", {
      name: "Add to graph",
    })[0];
    expect(cappedAddButton).toBeDisabled();
    expect(cappedAddButton).toHaveAttribute(
      "title",
      "Workflow already has the maximum of 8 steps.",
    );
    fireEvent.click(cappedAddButton);
    expect(screen.getByText("Steps (8/8)")).toBeInTheDocument();

    for (const label of [
      "Stage eight",
      "Stage seven",
      "Stage six",
      "Export",
      "Human review",
      "Synthesize",
      "Retrieve evidence",
    ]) {
      await user.click(
        screen.getByRole("button", {
          name: `Remove ${label}`,
        }),
      );
    }

    const finalRemove = screen.getByRole("button", {
      name: "Remove Ingest & verify",
    });
    expect(finalRemove).toBeDisabled();
    expect(finalRemove).toHaveAttribute(
      "title",
      "A workflow needs at least one step.",
    );
    fireEvent.click(finalRemove);
    expect(screen.getByText("Steps (1/8)")).toBeInTheDocument();

      await user.click(
        screen.getByRole("button", { name: "Configure Ingest & verify" }),
      );
      expect(screen.queryByText("Depends on")).not.toBeInTheDocument();
      await user.clear(screen.getByLabelText("Step label"));
      await user.type(screen.getByLabelText("Step label"), "   ");
      await user.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getAllByText("Ingest & verify").length).toBeGreaterThan(0);

      const activateButton = screen.getByRole("button", {
        name: "Activate after approval",
      });
      expect(activateButton).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "Validate & dry run" }));
      await act(async () => resolveRun());
      // The parent hasn't applied a result matching this reduced,
      // single-step graph yet, so activation stays gated even though a
      // dry run just resolved.
      expect(activateButton).toBeDisabled();
      rerender(
        <AutomationStudio
          result={{
            ...automationResult,
            trigger: "Manual",
            steps: [automationResult.steps[0]],
            validation_errors: [],
            dry_run_status: "passed",
            graph_version: undefined,
            graph_hash: "",
          } as unknown as AutomationStudioResult}
          running={false}
          error={null}
          onRun={onRun}
          data={data as unknown as WorkspaceData}
        />,
      );
      expect(activateButton).toBeEnabled();
      await user.click(activateButton);
      const dialog = screen.getByRole("dialog", { name: /activate graph 2\.0/i });
      expect(dialog).toHaveTextContent("Activate graph 2.0");
      expect(dialog).not.toHaveTextContent("(hash");
    },
    15000,
  );
});

describe("StudioForCapability", () => {
  it.each([
    ["literature", "Literature Studio"],
    ["grant", "Grant Studio"],
    ["matching", "Matching Explorer"],
    ["dataset", "Dataset Lab"],
    ["institutional_qa", "Institutional Q&A"],
    ["orchestration", "Workflow Automation"],
  ] as const)(
    "renders the %s studio surface",
    (capability, heading) => {
      const view = render(
        <StudioForCapability
          capability={capability}
          result={null}
          running={false}
          error={null}
          onRun={jest.fn().mockResolvedValue(undefined)}
        />,
      );
      expect(
        screen.getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
      view.unmount();
    },
  );

  it("covers optional empty chrome and shared running buttons", () => {
    const { rerender } = render(
      <LiteratureStudio
        result={null}
        running={false}
        error={""}
        onRun={jest.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: /workflow/i })).not.toBeInTheDocument();
    rerender(
      <LiteratureStudio
        result={null}
        running
        error={""}
        onRun={jest.fn().mockResolvedValue(undefined)}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
  });
});
