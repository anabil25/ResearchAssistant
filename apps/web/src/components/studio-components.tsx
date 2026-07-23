"use client";

import {
  Button,
} from "@fluentui/react-components";
import {
  BarChart3,
  BookOpen,
  CheckCircle2,
  CircleDashed,
  Clock3,
  FileSearch2,
  FileText,
  FlaskConical,
  Globe2,
  Landmark,
  Lock,
  Pencil,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Star,
  Trash2,
  Upload,
  Users,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  lazy,
  Suspense,
  useState,
  type ChangeEvent,
  type ReactNode,
} from "react";

import {
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";
import type {
  AutomationStep,
  AutomationStudioResult,
  Citation,
  CapabilityId,
  DatasetStudioResult,
  GrantStudioResult,
  InstitutionalStudioResult,
  LiteratureStudioResult,
  MatchingStudioResult,
  RankedEntity,
  StudioResult,
  WorkflowBlueprint,
} from "@/lib/types";

const ResearchMarkdown = lazy(async () => ({
  default: (await import("@/components/research-markdown")).ResearchMarkdown,
}));

export interface StudioRunOptions {
  onlineResearch?: boolean;
  inputs?: Record<string, unknown>;
}

interface StudioProps {
  result: StudioResult | null;
  running: boolean;
  error: string | null;
  workflow?: WorkflowBlueprint;
  data?: WorkspaceData | null;
  onRefresh?: () => Promise<void>;
  onNavigateToRun?: (runId: string) => void;
  onRun: (
    capability: CapabilityId,
    objective: string,
    options?: StudioRunOptions,
  ) => Promise<void>;
}

interface StudioHeaderProps {
  icon: LucideIcon;
  eyebrow: string;
  title: string;
  description: string;
  workflow?: WorkflowBlueprint;
  status: string;
}

function StudioHeader({
  icon: Icon,
  eyebrow,
  title,
  description,
  workflow,
  status,
}: StudioHeaderProps) {
  return (
    <>
      <header className="studio-header">
        <div className="studio-title-row">
          <span className="studio-icon" aria-hidden="true">
            <Icon size={21} />
          </span>
          <div>
            <span className="eyebrow">{eyebrow}</span>
            <h1>{title}</h1>
            <p>{description}</p>
          </div>
        </div>
        <span className="status-chip">{status}</span>
      </header>
      {workflow ? (
        <ol
          className="workflow-ribbon"
          aria-label={`${title} workflow`}
          tabIndex={0}
        >
          {workflow.stages.map((stage, index) => (
            <li key={stage.id}>
              <span>{index + 1}</span>
              <div>
                <strong>{stage.label}</strong>
                <small>{stage.owner}</small>
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </>
  );
}

function RunButton({
  running,
  disabled,
  children,
}: {
  running: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <Button
      appearance="primary"
      className="primary-button"
      type="submit"
      disabled={running || disabled}
      icon={
        running ? (
          <CircleDashed className="spin" size={17} />
        ) : (
          <Play size={16} />
        )
      }
    >
      {running ? "Running workflow..." : children}
    </Button>
  );
}

function StudioError({ message }: { message: string | null }) {
  return message ? (
    <div className="error-banner" role="alert">
      <ShieldCheck size={17} />
      <span>{message}</span>
    </div>
  ) : null;
}

function OnlineResearchToggle({
  enabled,
  onChange,
  note,
}: {
  enabled: boolean;
  onChange: (enabled: boolean) => void;
  note: string;
}) {
  return (
    <label className="policy-toggle">
      <span className="toggle-copy">
        <Globe2 size={17} />
        <span>
          <strong>Current public research</strong>
          <small>{note}</small>
        </span>
      </span>
      <input
        type="checkbox"
        checked={enabled}
        onChange={(event) => onChange(event.target.checked)}
      />
    </label>
  );
}

function InsightCard({ result }: { result: StudioResult }) {
  if (!result.insight) return null;
  return (
    <article className="model-insight">
      <div>
        <Sparkles size={16} />
        <strong>Hosted Agent analysis</strong>
        <span>{result.insight.evidence_state.replaceAll("_", " ")}</span>
      </div>
      <Suspense fallback={<p>Rendering structured analysis...</p>}>
        <ResearchMarkdown
          content={result.insight.content}
          citations={result.citations}
          unresolvedSourceIds={result.insight.unresolved_source_ids ?? []}
          label="Hosted Agent analysis"
        />
      </Suspense>
    </article>
  );
}

type LiteratureTab = "screen" | "extract" | "synthesize" | "audit";
type ScreeningDecisionValue = "include" | "exclude" | "maybe";
type ExtractionRow = LiteratureStudioResult["extraction_matrix"][number];
type ExtractionField = "method" | "population" | "outcome" | "limitation";

function normalizeDecision(value: string): ScreeningDecisionValue {
  if (value === "exclude" || value === "maybe") return value;
  return "include";
}

function csvEscape(value: string): string {
  const escaped = value.replaceAll('"', '""');
  return /[",\n]/.test(value) ? `"${escaped}"` : escaped;
}

export function LiteratureStudio({
  result,
  running,
  error,
  workflow,
  onRun,
}: StudioProps) {
  const [question, setQuestion] = useState(
    "Compare current approaches to auditable retrieval-augmented research synthesis.",
  );
  const [dateFrom, setDateFrom] = useState("2020");
  const [dateTo, setDateTo] = useState("2026");
  const [online, setOnline] = useState(false);
  const [sources, setSources] = useState([
    "PubMed",
    "Europe PMC",
    "Crossref",
    "OpenAlex",
  ]);
  const [inclusionCriteria, setInclusionCriteria] = useState([
    "Primary or benchmark study",
    "Methods available",
    "Limitations reported",
  ]);
  const [exclusionCriteria, setExclusionCriteria] = useState([
    "No extractable evidence",
    "Duplicate record",
  ]);
  const [newInclusion, setNewInclusion] = useState("");
  const [newExclusion, setNewExclusion] = useState("");
  const [tab, setTab] = useState<LiteratureTab>("screen");
  const [decisions, setDecisions] = useState<
    Record<string, ScreeningDecisionValue>
  >({});
  const [extractionEdits, setExtractionEdits] = useState<
    Record<string, Partial<Record<ExtractionField, string>>>
  >({});
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const literature =
    result && "extraction_matrix" in result
      ? (result as LiteratureStudioResult)
      : null;
  const sourceOptions = [
    "PubMed",
    "Europe PMC",
    "Crossref",
    "OpenAlex",
    "arXiv",
    "ClinicalTrials.gov",
  ];

  const includedCount = literature
    ? literature.screening.filter(
        (item) => (decisions[item.source_id] ?? "include") === "include",
      ).length
    : 0;
  const excludedCount = literature
    ? literature.screening.filter(
        (item) => decisions[item.source_id] === "exclude",
      ).length
    : 0;
  const maybeCount = literature
    ? literature.screening.filter(
        (item) => decisions[item.source_id] === "maybe",
      ).length
    : 0;
  const visibleExtraction = literature
    ? literature.extraction_matrix.filter(
        (row) => decisions[row.source_id] !== "exclude",
      )
    : [];
  const unresolvedCount =
    literature?.insight?.unresolved_source_ids?.length ?? 0;
  const resolvedCount =
    literature?.insight?.referenced_source_ids?.length ??
    literature?.citations.length ??
    0;

  const addCriterion = (
    kind: "inclusion" | "exclusion",
    value: string,
  ) => {
    const trimmed = value.trim();
    if (!trimmed) return;
    if (kind === "inclusion") {
      setInclusionCriteria((current) =>
        current.includes(trimmed) ? current : [...current, trimmed],
      );
      setNewInclusion("");
    } else {
      setExclusionCriteria((current) =>
        current.includes(trimmed) ? current : [...current, trimmed],
      );
      setNewExclusion("");
    }
  };

  const extractionValue = (row: ExtractionRow, field: ExtractionField) =>
    extractionEdits[row.source_id]?.[field] ?? row[field];

  const updateExtraction = (
    sourceId: string,
    field: ExtractionField,
    value: string,
  ) => {
    setExtractionEdits((current) => ({
      ...current,
      [sourceId]: { ...current[sourceId], [field]: value },
    }));
    setExportStatus(null);
  };

  const exportExtractionCsv = () => {
    const header = ["source_id", "method", "population", "outcome", "limitation"];
    const rows = visibleExtraction.map((row) => [
      row.source_id,
      extractionValue(row, "method"),
      extractionValue(row, "population"),
      extractionValue(row, "outcome"),
      extractionValue(row, "limitation"),
    ]);
    const csv = [header, ...rows]
      .map((cols) => cols.map(csvEscape).join(","))
      .join("\n");
    const filename = `extraction-matrix-${literature?.run.id ?? "draft"}.csv`;
    const anchor = document.createElement("a");
    anchor.href = `data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setExportStatus(
      `Exported ${rows.length} extraction row${rows.length === 1 ? "" : "s"} as ${filename}.`,
    );
  };

  return (
    <div className="studio-page literature-studio">
      <StudioHeader
        icon={BookOpen}
        eyebrow="Evidence review"
        title="Literature Studio"
        description="Design the protocol first, then screen, extract, synthesize, and audit every claim."
        workflow={workflow}
        status={literature ? "Synthesis complete" : "Protocol draft"}
      />
      <StudioError message={error} />

      <form
        className="literature-protocol"
        onSubmit={(event) => {
          event.preventDefault();
          void onRun("literature", question, {
            onlineResearch: online,
            inputs: {
              date_from: Number(dateFrom),
              date_to: Number(dateTo),
              sources,
              inclusion_criteria: inclusionCriteria,
              exclusion_criteria: exclusionCriteria,
              ...(online
                ? {
                    public_search_query: question,
                    public_research_acknowledged: true,
                  }
                : {}),
            },
          });
        }}
      >
        <section className="panel protocol-editor">
          <div className="panel-heading">
            <div>
              <span className="step-number">01</span>
              <h2>Review protocol</h2>
            </div>
            <span className="subtle-chip">Editable</span>
          </div>
          <label className="field">
            <span>Research question</span>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              rows={4}
            />
          </label>
          <div className="field-row">
            <label className="field">
              <span>Published from</span>
              <input
                type="number"
                value={dateFrom}
                onChange={(event) => setDateFrom(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Through</span>
              <input
                type="number"
                value={dateTo}
                onChange={(event) => setDateTo(event.target.value)}
              />
            </label>
          </div>
          <div className="criteria-block">
            <span>Include</span>
            <div className="tag-list">
              {inclusionCriteria.map((criterion) => (
                <button
                  type="button"
                  key={criterion}
                  onClick={() =>
                    setInclusionCriteria((current) =>
                      current.filter((item) => item !== criterion),
                    )
                  }
                  aria-label={`Remove inclusion criterion: ${criterion}`}
                >
                  {criterion}
                  <X size={12} aria-hidden="true" />
                </button>
              ))}
            </div>
            <div className="criteria-add-row">
              <input
                aria-label="Add inclusion criterion"
                placeholder="Add inclusion criterion"
                value={newInclusion}
                onChange={(event) => setNewInclusion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addCriterion("inclusion", newInclusion);
                  }
                }}
              />
              <button
                type="button"
                onClick={() => addCriterion("inclusion", newInclusion)}
                aria-label="Add inclusion criterion"
              >
                <Plus size={14} />
              </button>
            </div>
          </div>
          <div className="criteria-block">
            <span>Exclude</span>
            <div className="tag-list muted-tags">
              {exclusionCriteria.map((criterion) => (
                <button
                  type="button"
                  key={criterion}
                  onClick={() =>
                    setExclusionCriteria((current) =>
                      current.filter((item) => item !== criterion),
                    )
                  }
                  aria-label={`Remove exclusion criterion: ${criterion}`}
                >
                  {criterion}
                  <X size={12} aria-hidden="true" />
                </button>
              ))}
            </div>
            <div className="criteria-add-row">
              <input
                aria-label="Add exclusion criterion"
                placeholder="Add exclusion criterion"
                value={newExclusion}
                onChange={(event) => setNewExclusion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addCriterion("exclusion", newExclusion);
                  }
                }}
              />
              <button
                type="button"
                onClick={() => addCriterion("exclusion", newExclusion)}
                aria-label="Add exclusion criterion"
              >
                <Plus size={14} />
              </button>
            </div>
          </div>
        </section>

        <aside className="panel source-strategy" aria-label="Source strategy">
          <div className="panel-heading">
            <div>
              <span className="step-number">02</span>
              <h2>Source strategy</h2>
            </div>
            <FileSearch2 size={18} />
          </div>
          <fieldset>
            <legend>Scholarly sources</legend>
            {sourceOptions.map((source) => (
              <label className="check-row" key={source}>
                <input
                  type="checkbox"
                  checked={sources.includes(source)}
                  onChange={(event) =>
                    setSources((current) =>
                      event.target.checked
                        ? [...current, source]
                        : current.filter((item) => item !== source),
                    )
                  }
                />
                <span>{source}</span>
              </label>
            ))}
          </fieldset>
          <OnlineResearchToggle
            enabled={online}
            onChange={setOnline}
            note="Off by default. Public protocol text only."
          />
          <RunButton running={running}>Search & screen evidence</RunButton>
        </aside>
      </form>

      <section className="studio-results" aria-live="polite">
        <div className="result-toolbar">
          <div>
            <span className="eyebrow">Review workspace</span>
            <h2>Screening & extraction</h2>
          </div>
          <div className="segmented-control" aria-label="Literature output view">
            <button
              type="button"
              data-active={tab === "screen"}
              aria-pressed={tab === "screen"}
              onClick={() => setTab("screen")}
            >
              Screen
            </button>
            <button
              type="button"
              data-active={tab === "extract"}
              aria-pressed={tab === "extract"}
              onClick={() => setTab("extract")}
            >
              Extract
            </button>
            <button
              type="button"
              data-active={tab === "synthesize"}
              aria-pressed={tab === "synthesize"}
              onClick={() => setTab("synthesize")}
            >
              Synthesize
            </button>
            <button
              type="button"
              data-active={tab === "audit"}
              aria-pressed={tab === "audit"}
              onClick={() => setTab("audit")}
            >
              Audit
            </button>
          </div>
        </div>
        {literature ? (
          <div className="literature-output-grid">
            {tab === "screen" ? (
              <article className="panel screening-board">
                <div className="metric-line">
                  <span>
                    <strong>{literature.candidate_count}</strong> candidates
                  </span>
                  <span>
                    <strong>{includedCount}</strong> included
                  </span>
                  <span>
                    <strong>{excludedCount}</strong> excluded
                  </span>
                  <span>
                    <strong>{maybeCount}</strong> maybe
                  </span>
                </div>
                <div className="record-list">
                  {literature.screening.map((item, index) => {
                    const decision =
                      decisions[item.source_id] ??
                      normalizeDecision(item.decision);
                    return (
                      <div
                        className="screening-record"
                        key={`${item.source_id}-${index}`}
                      >
                        <span className="decision-mark">
                          <CheckCircle2 size={17} />
                        </span>
                        <div>
                          <strong>{item.title}</strong>
                          <p>{item.reason}</p>
                        </div>
                        <div
                          className="screening-decision-controls"
                          role="group"
                          aria-label={`Screening decision for ${item.title}`}
                        >
                          {(
                            ["include", "maybe", "exclude"] as const
                          ).map((value) => (
                            <button
                              type="button"
                              key={value}
                              data-active={decision === value}
                              data-decision={value}
                              onClick={() =>
                                setDecisions((current) => ({
                                  ...current,
                                  [item.source_id]: value,
                                }))
                              }
                            >
                              {value === "include"
                                ? "Include"
                                : value === "exclude"
                                  ? "Exclude"
                                  : "Maybe"}
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </article>
            ) : null}
            {tab === "extract" ? (
              <article className="panel extraction-board">
                <div className="panel-heading">
                  <h3>Extraction matrix</h3>
                  <span>{visibleExtraction.length} studies</span>
                  <button
                    type="button"
                    className="secondary-button"
                    disabled={!visibleExtraction.length}
                    onClick={
                      visibleExtraction.length ? exportExtractionCsv : undefined
                    }
                  >
                    Export CSV
                  </button>
                </div>
                {exportStatus ? (
                  <div className="save-status" role="status">
                    {exportStatus}
                  </div>
                ) : null}
                {visibleExtraction.length ? (
                  visibleExtraction.map((row, index) => {
                    const rowTitle =
                      literature?.screening.find(
                        (item) => item.source_id === row.source_id,
                      )?.title ?? row.source_id;
                    return (
                      <div
                        className="extraction-row"
                        key={`${row.source_id}-${index}`}
                      >
                        <strong>{rowTitle}</strong>
                        <label className="field">
                          <span>Method</span>
                          <input
                            value={extractionValue(row, "method")}
                            aria-label={`Method for ${rowTitle}`}
                            onChange={(event) =>
                              updateExtraction(
                                row.source_id,
                                "method",
                                event.target.value,
                              )
                            }
                          />
                        </label>
                        <label className="field">
                          <span>Population</span>
                          <input
                            value={extractionValue(row, "population")}
                            aria-label={`Population for ${rowTitle}`}
                            onChange={(event) =>
                              updateExtraction(
                                row.source_id,
                                "population",
                                event.target.value,
                              )
                            }
                          />
                        </label>
                        <label className="field">
                          <span>Outcome</span>
                          <textarea
                            value={extractionValue(row, "outcome")}
                            aria-label={`Outcome for ${rowTitle}`}
                            rows={2}
                            onChange={(event) =>
                              updateExtraction(
                                row.source_id,
                                "outcome",
                                event.target.value,
                              )
                            }
                          />
                        </label>
                        <label className="field">
                          <span>Limitation</span>
                          <textarea
                            value={extractionValue(row, "limitation")}
                            aria-label={`Limitation for ${rowTitle}`}
                            rows={2}
                            onChange={(event) =>
                              updateExtraction(
                                row.source_id,
                                "limitation",
                                event.target.value,
                              )
                            }
                          />
                        </label>
                      </div>
                    );
                  })
                ) : (
                  <p className="muted-copy">
                    No included study currently has extractable fields. Mark
                    a screening decision as Include or Maybe to populate this
                    matrix.
                  </p>
                )}
              </article>
            ) : null}
            {tab === "synthesize" ? (
              <article className="panel synthesis-card">
                <span className="eyebrow">Audited synthesis</span>
                {literature.synthesis.map((paragraph) => (
                  <p key={paragraph}>{paragraph}</p>
                ))}
              </article>
            ) : null}
            {tab === "audit" ? (
              <article className="panel audit-board">
                <div className="panel-heading">
                  <h3>Claim & citation audit</h3>
                  <span>{literature.citations.length} citations</span>
                </div>
                <div className="metric-line">
                  <span>
                    <strong>{resolvedCount}</strong> resolved
                  </span>
                  <span>
                    <strong>{unresolvedCount}</strong> unresolved
                  </span>
                  <span>
                    <strong>{excludedCount}</strong> excluded records
                  </span>
                </div>
                <div className="audit-citation-list">
                  {literature.citations.map((citation) => {
                    const isUnresolved = (
                      literature.insight?.unresolved_source_ids ?? []
                    ).includes(citation.source_id);
                    return (
                      <div className="audit-citation-row" key={citation.id}>
                        <span
                          className={
                            isUnresolved
                              ? "audit-flag unresolved"
                              : "audit-flag resolved"
                          }
                        >
                          {isUnresolved ? "Unresolved" : "Resolved"}
                        </span>
                        <div>
                          <strong>{citation.title}</strong>
                          <small>{citation.section}</small>
                        </div>
                      </div>
                    );
                  })}
                </div>
                {excludedCount ? (
                  <div className="audit-exclusions">
                    <strong>Excluded from synthesis</strong>
                    {literature.screening
                      .filter(
                        (item) => decisions[item.source_id] === "exclude",
                      )
                      .map((item) => (
                        <p key={item.source_id}>
                          {item.title} — {item.reason}
                        </p>
                      ))}
                  </div>
                ) : null}
              </article>
            ) : null}
          </div>
        ) : (
          <div className="empty-workspace">
            <Search size={24} />
            <strong>No screening run yet</strong>
            <p>
              Lock the protocol and start a search to populate screening
              decisions, extraction fields, and claim-level evidence.
            </p>
          </div>
        )}
        {literature ? <InsightCard result={literature} /> : null}
      </section>
    </div>
  );
}

type GrantSection = "aims" | "significance" | "approach";
type GrantAction = "draft" | "red_team";
type GrantRequirementItem = GrantStudioResult["requirements"][number];

interface DraftConnectorRequest {
  id: string;
  name: string;
  category: string;
  baseUrl: string;
  justification: string;
  requestedAt: string;
}

export function GrantStudio({
  result,
  running,
  error,
  workflow,
  onRun,
  data,
}: StudioProps) {
  const [objective, setObjective] = useState(
    "Develop a competitive application for an open research infrastructure program.",
  );
  const [opportunityId, setOpportunityId] = useState("SORI-2026-01");
  const [factsConfirmed, setFactsConfirmed] = useState(false);
  const [online, setOnline] = useState(false);
  const [section, setSection] = useState<GrantSection>("aims");
  const [lastAction, setLastAction] = useState<GrantAction>("draft");
  const [fundingSources, setFundingSources] = useState<string[]>([
    "grants_gov",
    "nih_reporter",
  ]);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [draftRequests, setDraftRequests] = useState<DraftConnectorRequest[]>(
    [],
  );
  const [discoveryQuery, setDiscoveryQuery] = useState("");
  const [discoveryCapability, setDiscoveryCapability] = useState("All");
  const [selectedRequirement, setSelectedRequirement] =
    useState<GrantRequirementItem | null>(null);
  const grant =
    result && "requirements" in result ? (result as GrantStudioResult) : null;
  const requirements: GrantRequirementItem[] =
    grant?.requirements ??
    [
      {
        id: "summary",
        text: "Project summary",
        category: "Narrative",
        status: "not parsed",
        evidence_ids: [],
      },
      {
        id: "aims",
        text: "Specific aims",
        category: "Narrative",
        status: "not parsed",
        evidence_ids: [],
      },
      {
        id: "dmp",
        text: "Data-management plan",
        category: "Attachment",
        status: "not parsed",
        evidence_ids: [],
      },
    ];
  const fundingConnectors = (data?.connectors ?? []).filter((connector) =>
    connector.assigned_agents.includes("grant"),
  );
  const discoverableConnectors = fundingConnectors.filter((connector) =>
    fundingSources.includes(connector.id),
  );
  const discoveryCapabilities = [
    "All",
    ...Array.from(
      new Set(discoverableConnectors.flatMap((connector) => connector.capabilities)),
    ),
  ];
  const discoveryResults = discoverableConnectors.filter((connector) => {
    const matchesCapability =
      discoveryCapability === "All" ||
      connector.capabilities.includes(discoveryCapability);
    const matchesQuery = `${connector.name} ${connector.description}`
      .toLowerCase()
      .includes(discoveryQuery.trim().toLowerCase());
    return matchesCapability && matchesQuery;
  });
  const sectionLabels: Record<GrantSection, string> = {
    aims: "Specific aims",
    significance: "Significance",
    approach: "Approach",
  };
  const draftedSection =
    section === "aims"
      ? null
      : (grant?.sections ?? []).find((item) =>
          item.title.toLowerCase().includes(section),
        );

  const runGrant = (action: GrantAction) => {
    setLastAction(action);
    void onRun(
      "grant",
      action === "red_team"
        ? `${objective} Run a structured red-team review of compliance risks, missing evidence, and likely reviewer objections before export.`
        : objective,
      {
        onlineResearch: online,
        inputs: {
          opportunity_id: opportunityId,
          sponsor: "Example Federal Research Office",
          project_facts: factsConfirmed
            ? ["Research office sponsor confirmed", "PI role confirmed"]
            : [],
          funding_sources: fundingSources,
          red_team_pass: action === "red_team",
          ...(online
            ? {
                public_search_query: `${opportunityId} public funding opportunity requirements`,
                public_research_acknowledged: true,
              }
            : {}),
        },
      },
    );
  };

  return (
    <div className="studio-page grant-studio">
      <StudioHeader
        icon={FileText}
        eyebrow="Application lifecycle"
        title="Grant Studio"
        description="Turn the authoritative notice and verified project facts into a compliance-mapped, red-teamed package."
        workflow={workflow}
        status={
          grant
            ? lastAction === "red_team"
              ? `${grant.readiness}% ready · red-team pass`
              : `${grant.readiness}% ready`
            : "Opportunity intake"
        }
      />
      <StudioError message={error} />

      <section className="grant-opportunity-strip">
        <div>
          <span className="eyebrow">Funding opportunity</span>
          <strong>
            {grant?.opportunity.title ??
              "Open Research Infrastructure Opportunity"}
          </strong>
          <small>
            {grant?.opportunity.sponsor ??
              "Example Federal Research Office"}
          </small>
        </div>
        <div className="opportunity-facts">
          <span>
            Notice <strong>{opportunityId}</strong>
          </span>
          <span>
            Deadline <strong>{grant?.opportunity.deadline ?? "2026-10-15"}</strong>
          </span>
          <span>
            Status <strong className="green-text">Open</strong>
          </span>
        </div>
      </section>

      <form
        className="grant-workspace"
        onSubmit={(event) => {
          event.preventDefault();
          runGrant("draft");
        }}
      >
        <aside
          className="panel requirement-matrix"
          aria-label="Grant requirement matrix"
        >
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Authoritative notice</span>
              <h2>Requirement matrix</h2>
            </div>
            <span className="subtle-chip">
              {grant ? `${requirements.length} mapped` : "Awaiting parse"}
            </span>
          </div>
          <label className="field">
            <span>Opportunity ID</span>
            <input
              value={opportunityId}
              onChange={(event) => setOpportunityId(event.target.value)}
            />
          </label>
          <div className="requirements-list">
            {requirements.map((item) => (
              <button
                type="button"
                key={item.id}
                disabled={!grant}
                title={
                  grant
                    ? "Open source evidence and mapping for this requirement."
                    : "Requirement evidence opens after the notice is parsed."
                }
                onClick={() => grant && setSelectedRequirement(item)}
              >
                <span
                  className={
                    item.status === "mapped" ? "requirement-done" : undefined
                  }
                >
                  {item.status === "mapped" ? (
                    <CheckCircle2 size={15} />
                  ) : (
                    <CircleDashed size={15} />
                  )}
                </span>
                <span>
                  <strong>{item.text}</strong>
                  <small>{item.category}</small>
                </span>
              </button>
            ))}
          </div>
          <OnlineResearchToggle
            enabled={online}
            onChange={setOnline}
            note="Only the public funding notice may leave the workspace."
          />
          <div className="funding-source-panel" aria-label="Funding source discovery">
            <span className="funding-source-heading">Funding source discovery</span>
            {fundingConnectors.length ? (
              fundingConnectors.map((connector) => (
                <label className="check-row" key={connector.id}>
                  <input
                    type="checkbox"
                    checked={fundingSources.includes(connector.id)}
                    onChange={(event) =>
                      setFundingSources((current) =>
                        event.target.checked
                          ? [...current, connector.id]
                          : current.filter((item) => item !== connector.id),
                      )
                    }
                  />
                  <span>{connector.name}</span>
                </label>
              ))
            ) : (
              <p className="muted-copy">
                No funding connectors are assigned yet. Assign one in Project
                Settings → Connectors.
              </p>
            )}
            <button
              type="button"
              className="secondary-button full-button"
              onClick={() => setBuilderOpen(true)}
            >
              Request a new connector
            </button>
            {draftRequests.length ? (
              <div className="draft-request-list">
                {draftRequests.map((request) => (
                  <div className="draft-request-row" key={request.id}>
                    <strong>{request.name}</strong>
                    <span className="subtle-chip">Draft — needs review</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          <div
            className="funding-source-panel"
            aria-label="Opportunity discovery"
          >
            <span className="funding-source-heading">Opportunity discovery</span>
            <label className="search-field">
              <Search size={16} />
              <span className="sr-only">Search funding opportunities</span>
              <input
                value={discoveryQuery}
                onChange={(event) => setDiscoveryQuery(event.target.value)}
                placeholder="Search funding opportunities"
                aria-label="Search funding opportunities"
                disabled={!discoverableConnectors.length}
              />
            </label>
            <div
              className="filter-pills"
              role="group"
              aria-label="Opportunity capability filter"
            >
              {discoveryCapabilities.map((capability) => (
                <button
                  type="button"
                  key={capability}
                  data-active={discoveryCapability === capability}
                  onClick={() => setDiscoveryCapability(capability)}
                >
                  {capability}
                </button>
              ))}
            </div>
            {discoverableConnectors.length ? (
              discoveryResults.length ? (
                <div className="draft-request-list">
                  {discoveryResults.map((connector) => (
                    <div className="draft-request-row" key={connector.id}>
                      <span>
                        <strong>{connector.name}</strong>
                        <span className="connector-capability-list">
                          {connector.capabilities.join(", ")}
                        </span>
                      </span>
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() =>
                          setOpportunityId(
                            `${connector.id.toUpperCase()}-LEAD-${new Date().getFullYear()}`,
                          )
                        }
                      >
                        Use as opportunity source
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted-copy">
                  No net-new opportunities match this query in the selected
                  connectors.
                </p>
              )
            ) : (
              <p className="muted-copy">
                Select at least one funding connector above to discover
                opportunities.
              </p>
            )}
          </div>
        </aside>

        <section className="panel grant-editor">
          <div className="document-toolbar">
            <div className="document-tabs">
              {(Object.keys(sectionLabels) as GrantSection[]).map((key) => (
                <button
                  type="button"
                  key={key}
                  data-active={section === key}
                  aria-pressed={section === key}
                  onClick={() => setSection(key)}
                >
                  {sectionLabels[key]}
                </button>
              ))}
            </div>
            <span>Draft v0.8</span>
          </div>
          <label className="field">
            <span>Project framing</span>
            <textarea
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              rows={3}
            />
          </label>
          {section === "aims" ? (
            <div className="grant-document">
              <span className="document-label">SPECIFIC AIMS</span>
              {(grant?.specific_aims ?? [
                "Parse the notice and verify project facts before drafting.",
                "Map every claim and commitment to its owner or source.",
                "Evaluate feasibility, compliance, and reviewer objections.",
              ]).map((aim, index) => (
                <div className="aim-block" key={aim}>
                  <span>{index + 1}</span>
                  <p>{aim}</p>
                </div>
              ))}
            </div>
          ) : (
            <div className="grant-document">
              <span className="document-label">
                {sectionLabels[section].toUpperCase()}
              </span>
              {draftedSection ? (
                <p>{draftedSection.body}</p>
              ) : (
                <p className="muted-copy">
                  Not yet drafted for this section. Run the studio to
                  generate a citation-backed draft.
                </p>
              )}
            </div>
          )}
          <div className="editor-actions">
            <RunButton running={running && lastAction === "draft"}>
              Parse notice & build package
            </RunButton>
            <button
              className="secondary-button"
              type="button"
              disabled={running}
              onClick={() => runGrant("red_team")}
            >
              <ShieldCheck size={16} />
              {running && lastAction === "red_team"
                ? "Red-teaming..."
                : "Red-team draft"}
            </button>
          </div>
        </section>

        <aside
          className="panel grant-readiness"
          aria-label="Grant readiness and project facts"
        >
          <div className="readiness-ring">
            <strong>{grant?.readiness ?? 34}%</strong>
            <span>review readiness</span>
          </div>
          <div className="panel-heading">
            <h2>Project fact inventory</h2>
          </div>
          <label className="check-row emphasis-check">
            <input
              type="checkbox"
              checked={factsConfirmed}
              onChange={(event) => setFactsConfirmed(event.target.checked)}
            />
            <span>
              <strong>Core project facts verified</strong>
              <small>PI, sponsor, scope, and available resources</small>
            </span>
          </label>
          {(grant?.fact_gaps ?? []).map((gap) => (
            <div className="fact-gap" key={gap.id}>
              <span>{gap.status}</span>
              <strong>{gap.label}</strong>
              <p>{gap.guidance}</p>
            </div>
          ))}
          {grant?.blockers.length ? (
            <div className="blocker-callout">
              <Lock size={16} />
              <span>
                Export blocked by: <strong>{grant.blockers.join(", ")}</strong>
              </span>
            </div>
          ) : null}
        </aside>
      </form>
      {grant ? <InsightCard result={grant} /> : null}
      {builderOpen ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="connector-builder-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">Draft only</span>
                <h2 id="connector-builder-title">Request a new connector</h2>
              </div>
              <button
                aria-label="Close connector request dialog"
                onClick={() => setBuilderOpen(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              This records a draft request only. An administrator must
              review, verify terms of use, and provision the connector
              before it can run in any studio.
            </p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                const name = String(form.get("name")).trim();
                const baseUrl = String(form.get("baseUrl")).trim();
                const justification = String(form.get("justification")).trim();
                if (!name || !baseUrl || !justification) return;
                setDraftRequests((current) => [
                  ...current,
                  {
                    id: `draft-${Date.now()}`,
                    name,
                    category: String(form.get("category")),
                    baseUrl,
                    justification,
                    requestedAt: new Date().toISOString(),
                  },
                ]);
                setBuilderOpen(false);
              }}
            >
              <label className="field">
                <span>Connector name</span>
                <input name="name" required minLength={2} />
              </label>
              <label className="field">
                <span>Category</span>
                <select name="category" defaultValue="Funding">
                  <option>Funding</option>
                  <option>Literature</option>
                  <option>Datasets</option>
                  <option>Identity</option>
                </select>
              </label>
              <label className="field">
                <span>Base URL</span>
                <input name="baseUrl" type="url" required />
              </label>
              <label className="field">
                <span>Justification</span>
                <textarea name="justification" required rows={3} />
              </label>
              <div className="modal-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => setBuilderOpen(false)}
                >
                  Cancel
                </button>
                <button className="primary-button" type="submit">
                  Save draft request
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
      {selectedRequirement && grant ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="requirement-detail-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">{selectedRequirement.category}</span>
                <h2 id="requirement-detail-title">
                  {selectedRequirement.text}
                </h2>
              </div>
              <button
                aria-label="Close requirement detail"
                onClick={() => setSelectedRequirement(null)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              Status:{" "}
              <strong>
                {selectedRequirement.status.replaceAll("_", " ")}
              </strong>
            </p>
            {(() => {
              const evidence = grant.citations.filter((citation) =>
                selectedRequirement.evidence_ids.includes(citation.id),
              );
              return evidence.length ? (
                <dl className="citation-detail-facts">
                  {evidence.map((citation) => (
                    <div key={citation.id}>
                      <dt>{citation.title}</dt>
                      <dd>
                        {citation.section} · {citation.quote}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p className="muted-copy">
                  No source evidence is linked to this requirement yet.
                </p>
              );
            })()}
          </div>
        </div>
      ) : null}
    </div>
  );
}

const RECORD_TYPE_OPTIONS: { label: string; kind: string }[] = [
  { label: "People", kind: "person" },
  { label: "Facilities", kind: "facility" },
  { label: "Equipment", kind: "equipment" },
  { label: "Methods", kind: "method" },
  { label: "Templates", kind: "template" },
];

const HARD_FILTER_OPTIONS: { id: string; label: string }[] = [
  { id: "current_institutional_record", label: "Current institutional record" },
  { id: "source_evidence_available", label: "Source evidence available" },
];

export function MatchingStudio({
  result,
  running,
  error,
  workflow,
  onRun,
  data,
}: StudioProps) {
  const [query, setQuery] = useState(
    "Find genomics and reproducibility collaborators with computational methods experience.",
  );
  const [online, setOnline] = useState(false);
  const [selectedMatch, setSelectedMatch] = useState(0);
  const [recordTypes, setRecordTypes] = useState<string[]>(
    RECORD_TYPE_OPTIONS.slice(0, 3).map((option) => option.kind),
  );
  const [hardFilters, setHardFilters] = useState<string[]>(
    HARD_FILTER_OPTIONS.map((option) => option.id),
  );
  const [sources, setSources] = useState<string[]>([
    "institutional",
    "openalex",
    "nih_reporter",
  ]);
  const [shortlist, setShortlist] = useState<string[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const matching =
    result && "matches" in result ? (result as MatchingStudioResult) : null;
  const selected = matching?.matches[selectedMatch];
  const shortlistedMatches: RankedEntity[] = (matching?.matches ?? []).filter(
    (match) => shortlist.includes(match.id),
  );
  const publicSources = (data?.connectors ?? []).filter((connector) =>
    connector.assigned_agents.includes("matching"),
  );

  const toggleShortlist = (id: string) => {
    setShortlist((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  };

  const toggleSource = (id: string, checked: boolean) => {
    setSources((current) =>
      checked ? [...current, id] : current.filter((item) => item !== id),
    );
  };

  return (
    <div className="studio-page matching-studio">
      <StudioHeader
        icon={Users}
        eyebrow="Transparent discovery"
        title="Matching Explorer"
        description="Apply hard filters before an evidence-weighted score, then confirm availability with a human owner."
        workflow={workflow}
        status={matching ? `${matching.matches.length} verified records` : "Criteria setup"}
      />
      <StudioError message={error} />
      <form
        className="matching-layout"
        onSubmit={(event) => {
          event.preventDefault();
          setSelectedMatch(0);
          void onRun("matching", query, {
            onlineResearch: online,
            inputs: {
              record_kinds: recordTypes,
              hard_filters: hardFilters,
              sources,
              ...(online
                ? {
                    public_search_query: query,
                    public_research_acknowledged: true,
                  }
                : {}),
            },
          });
        }}
      >
        <aside
          className="panel matching-filters"
          aria-label="Matching criteria and filters"
        >
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Step 1</span>
              <h2>Match criteria</h2>
            </div>
          </div>
          <label className="field">
            <span>Expertise, method, or need</span>
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              rows={4}
            />
          </label>
          <div className="filter-group">
            <span>Record types</span>
            {RECORD_TYPE_OPTIONS.map((option) => (
              <label className="check-row" key={option.kind}>
                <input
                  type="checkbox"
                  checked={recordTypes.includes(option.kind)}
                  onChange={(event) =>
                    setRecordTypes((current) =>
                      event.target.checked
                        ? [...current, option.kind]
                        : current.filter((item) => item !== option.kind),
                    )
                  }
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
          <div className="filter-group">
            <span>Hard filters</span>
            {HARD_FILTER_OPTIONS.map((option) => (
              <label className="check-row" key={option.id}>
                <input
                  type="checkbox"
                  checked={hardFilters.includes(option.id)}
                  onChange={(event) =>
                    setHardFilters((current) =>
                      event.target.checked
                        ? [...current, option.id]
                        : current.filter((item) => item !== option.id),
                    )
                  }
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
          <div className="filter-group" aria-label="Matching sources">
            <span>Sources</span>
            <label className="check-row">
              <input
                type="checkbox"
                checked={sources.includes("institutional")}
                onChange={(event) =>
                  toggleSource("institutional", event.target.checked)
                }
              />
              <span>Institutional directory</span>
            </label>
            {publicSources.length ? (
              publicSources.map((connector) => (
                <label className="check-row" key={connector.id}>
                  <input
                    type="checkbox"
                    checked={sources.includes(connector.id)}
                    disabled={!connector.enabled}
                    onChange={(event) =>
                      toggleSource(connector.id, event.target.checked)
                    }
                  />
                  <span>{connector.name}</span>
                  {!connector.enabled ? (
                    <span className="connector-capability-list">
                      Disabled in Settings
                    </span>
                  ) : null}
                </label>
              ))
            ) : (
              <p className="muted-copy">
                No public connectors are assigned to Matching yet. Assign one
                in Project Settings → Connectors.
              </p>
            )}
            <label
              className="check-row emphasis-check work-iq-toggle"
              title="Work IQ requires tenant-level Microsoft Graph consent that has not been granted."
            >
              <input
                type="checkbox"
                checked={false}
                disabled
                aria-describedby="matching-work-iq-note"
              />
              <span>
                <strong>Work IQ collaboration signals</strong>
                <small id="matching-work-iq-note">
                  Disabled — requires tenant Microsoft Graph consent this
                  workspace has not been granted.
                </small>
              </span>
            </label>
          </div>
          <OnlineResearchToggle
            enabled={online}
            onChange={setOnline}
            note="Public metadata is a lead, not availability proof."
          />
          <RunButton running={running}>Build verified shortlist</RunButton>
        </aside>

        <section className="matching-results">
          <div className="matching-toolbar">
            <div>
              <span className="eyebrow">Ranked results</span>
              <h2>Evidence-backed shortlist</h2>
            </div>
            <span className="subtle-chip">
              {matching ? `${matching.matches.length} matches` : "No run"}
            </span>
          </div>
          {matching ? (
            <div className="match-card-list">
              {matching.matches.map((match, index) => (
                <article
                  className="match-card"
                  data-active={index === selectedMatch}
                  key={match.id}
                >
                  <button
                    type="button"
                    className="match-select"
                    onClick={() => setSelectedMatch(index)}
                    aria-pressed={index === selectedMatch}
                  >
                    <span className="match-avatar">
                      {match.kind === "person" ? (
                        match.name
                          .split(" ")
                          .slice(-2)
                          .map((part) => part[0])
                          .join("")
                      ) : (
                        <Landmark size={18} />
                      )}
                    </span>
                    <span className="match-copy">
                      <span>
                        <strong>{match.name}</strong>
                        <small>{match.kind}</small>
                      </span>
                      <span className="match-tags">
                        {match.strengths.slice(0, 3).map((strength) => (
                          <small key={strength}>{strength}</small>
                        ))}
                      </span>
                      <span className="freshness">
                        {match.freshness}
                        {!match.hard_filters_passed
                          ? " · hard filter gap"
                          : ""}
                      </span>
                    </span>
                  </button>
                  <div className="match-card-actions">
                    <span className="score-badge">{match.score}</span>
                    <button
                      type="button"
                      className="shortlist-toggle"
                      data-active={shortlist.includes(match.id)}
                      aria-pressed={shortlist.includes(match.id)}
                      aria-label={`${
                        shortlist.includes(match.id) ? "Remove" : "Add"
                      } ${match.name} ${
                        shortlist.includes(match.id)
                          ? "from shortlist"
                          : "to shortlist"
                      }`}
                      onClick={() => toggleShortlist(match.id)}
                    >
                      <Star
                        size={16}
                        fill={
                          shortlist.includes(match.id)
                            ? "currentColor"
                            : "none"
                        }
                      />
                    </button>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="empty-workspace compact-empty">
              <Users size={24} />
              <strong>No shortlist yet</strong>
              <p>Define criteria to resolve and score authorized records.</p>
            </div>
          )}
          {shortlistedMatches.length ? (
            <div className="shortlist-panel">
              <div className="panel-heading">
                <h3>Shortlist ({shortlistedMatches.length})</h3>
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => setCompareOpen((current) => !current)}
                >
                  {compareOpen ? "Hide comparison" : "Compare shortlisted"}
                </button>
              </div>
              {compareOpen ? (
                <div className="shortlist-compare" role="table">
                  <div className="shortlist-compare-row shortlist-compare-head" role="row">
                    <span role="columnheader">Name</span>
                    <span role="columnheader">Score</span>
                    <span role="columnheader">Top evidence factors</span>
                  </div>
                  {shortlistedMatches.map((match) => (
                    <div className="shortlist-compare-row" role="row" key={match.id}>
                      <strong role="rowheader">{match.name}</strong>
                      <span role="cell">{match.score}</span>
                      <span role="cell">
                        {match.components
                          .slice(0, 2)
                          .map(
                            (component) =>
                              `${component.label} (${component.contribution.toFixed(1)})`,
                          )
                          .join(", ")}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </section>

        <aside
          className="panel score-explainer"
          aria-label="Match score explanation"
        >
          <span className="eyebrow">Why this match</span>
          <h2>{selected?.name ?? "Select a result"}</h2>
          {selected ? (
            <>
              <div className="large-score">
                <strong>{selected.score}</strong>
                <span>/ 100</span>
              </div>
              <div className="score-components">
                {selected.components.map((component) => (
                  <div key={component.criterion_id}>
                    <span>
                      <strong>{component.label}</strong>
                      <small>{Math.round(component.weight * 100)}% weight</small>
                    </span>
                    <span>{component.contribution.toFixed(1)}</span>
                    <div>
                      <i style={{ width: `${component.match * 100}%` }} />
                    </div>
                  </div>
                ))}
              </div>
              <div className="confirmation-note">
                <ShieldCheck size={17} />
                Availability remains unconfirmed until an institutional owner
                reviews outreach.
              </div>
            </>
          ) : (
            <p className="muted-copy">
              Score components and their evidence appear here. Models do not
              choose weights or calculate the total.
            </p>
          )}
        </aside>
      </form>
      {matching ? <InsightCard result={matching} /> : null}
    </div>
  );
}

const MAX_INLINE_DATASET_BYTES = 5_000_000;

type DatasetAssetMode = "sample" | "large" | "upload";

export function DatasetStudio({
  result,
  running,
  error,
  workflow,
  onRun,
  onRefresh,
}: StudioProps) {
  const [objective, setObjective] = useState(
    "Profile the pilot outcome dataset and plan a descriptive group comparison.",
  );
  const [assetMode, setAssetMode] = useState<DatasetAssetMode>("sample");
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [csvText, setCsvText] = useState<string | null>(null);
  const [fileKind, setFileKind] = useState<"csv" | "json" | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [csvReadStatus, setCsvReadStatus] = useState<
    "idle" | "reading" | "ready" | "error"
  >("idle");
  const [planApproved, setPlanApproved] = useState(false);
  const [libraryUploadStatus, setLibraryUploadStatus] = useState<
    "idle" | "uploading" | "uploaded" | "error"
  >("idle");
  const [libraryUploadError, setLibraryUploadError] = useState<string | null>(
    null,
  );
  const dataset =
    result && "analysis_plan" in result ? (result as DatasetStudioResult) : null;
  const largeAsset = assetMode === "large";

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null;
    event.target.value = "";
    if (!file) return;
    const extension = file.name.split(".").pop()?.toLowerCase();
    const isCsv = extension === "csv" || file.type === "text/csv";
    const isJson = extension === "json" || file.type === "application/json";
    if (!isCsv && !isJson) {
      setFileError("Only .csv or .json files are supported here.");
      return;
    }
    if (file.size > MAX_INLINE_DATASET_BYTES) {
      setFileError(
        `Files must be ${MAX_INLINE_DATASET_BYTES / 1_000_000} MB or smaller in this workspace. Larger assets need the estimate-and-approve path.`,
      );
      return;
    }
    setFileError(null);
    setUploadedFile(file);
    setFileKind(isCsv ? "csv" : "json");
    setAssetMode("upload");
    setLibraryUploadStatus("idle");
    setLibraryUploadError(null);
    setPlanApproved(false);
    setCsvText(null);
    if (isCsv) {
      setCsvReadStatus("reading");
      const reader = new FileReader();
      reader.onload = () => {
        setCsvText(typeof reader.result === "string" ? reader.result : null);
        setCsvReadStatus("ready");
      };
      reader.onerror = () => {
        setCsvText(null);
        setCsvReadStatus("error");
        setFileError(
          "This CSV file could not be read. Choose a different file and try again.",
        );
      };
      reader.readAsText(file);
    } else {
      setCsvReadStatus("ready");
    }
  };

  const uploadToLibrary = (file: File, kind: "csv" | "json") => {
    setLibraryUploadStatus("uploading");
    setLibraryUploadError(null);
    const form = new FormData();
    form.append("title", file.name);
    form.append("kind", "Dataset");
    form.append("license", "Project supplied");
    form.append(
      "description",
      `Dataset uploaded from Dataset Lab for deterministic profiling (${kind}).`,
    );
    form.append("source", "Workspace upload");
    form.append("file", file);
    void uploadLibraryItem(form)
      .then(async () => {
        setLibraryUploadStatus("uploaded");
        await onRefresh?.();
      })
      .catch((uploadError: unknown) => {
        setLibraryUploadStatus("error");
        setLibraryUploadError(
          uploadError instanceof Error
            ? uploadError.message
            : "Upload to Library failed.",
        );
      });
  };

  const runDisabled =
    running ||
    !planApproved ||
    (assetMode === "upload" && !uploadedFile) ||
    csvReadStatus === "reading";

  return (
    <div className="studio-page dataset-studio">
      <StudioHeader
        icon={BarChart3}
        eyebrow="Deterministic analysis"
        title="Dataset Lab"
        description="Approve the bounded input, then let the Foundry Dataset Agent analyze it with the managed Code Interpreter tool."
        workflow={workflow}
        status={dataset ? dataset.run.status.replaceAll("_", " ") : "Asset selection"}
      />
      <StudioError message={error} />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (runDisabled) return;
          const inputs =
            assetMode === "upload" && uploadedFile
              ? {
                  filename: uploadedFile.name,
                  estimated_bytes: uploadedFile.size,
                  compute_adapter_configured: true,
                  analysis_approved: planApproved,
                  ...(csvText ? { csv_text: csvText } : {}),
                }
              : assetMode === "large"
                ? {
                    filename: "clinical-events-archive.parquet",
                    estimated_bytes: 1_200_000_000_000,
                    compute_adapter_configured: true,
                    analysis_approved: planApproved,
                  }
                : {
                    filename: "pilot-outcomes.csv",
                    estimated_bytes: 4_000_000,
                    compute_adapter_configured: true,
                    analysis_approved: planApproved,
                  };
          void onRun("dataset", objective, { inputs });
        }}
      >
        <section className="asset-picker" aria-label="Dataset assets">
          <button
            type="button"
            data-active={assetMode === "sample"}
            onClick={() => {
              setAssetMode("sample");
              setPlanApproved(false);
            }}
          >
            <span className="asset-icon">
              <FileText size={19} />
            </span>
            <span>
              <strong>pilot-outcomes.csv</strong>
              <small>4 MB · Workspace upload · Ready</small>
            </span>
            <CheckCircle2 size={18} />
          </button>
          <button
            type="button"
            data-active={largeAsset}
            onClick={() => {
              setAssetMode("large");
              setPlanApproved(false);
            }}
          >
            <span className="asset-icon">
              <FlaskConical size={19} />
            </span>
            <span>
              <strong>clinical-events-archive.parquet</strong>
              <small>1.2 TB · Project storage · Estimate required</small>
            </span>
            <Clock3 size={18} />
          </button>
          <label
            className="asset-upload-tile"
            data-active={assetMode === "upload"}
            data-read-status={csvReadStatus}
          >
            <span className="asset-icon">
              <Upload size={19} />
            </span>
            <span>
              <strong>{uploadedFile ? uploadedFile.name : "Upload a dataset"}</strong>
              <small>
                {uploadedFile
                  ? assetMode === "upload" && csvReadStatus === "reading"
                    ? `Reading ${fileKind?.toUpperCase()}…`
                    : `${(uploadedFile.size / 1_000_000).toFixed(2)} MB · ${fileKind?.toUpperCase()}`
                  : "CSV or JSON · up to 5 MB"}
              </small>
            </span>
            <input
              type="file"
              accept=".csv,.json,text/csv,application/json"
              aria-label="Upload a dataset file"
              onChange={handleFileChange}
            />
          </label>
          <label className="field analysis-question">
            <span>Analysis objective</span>
            <input
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
            />
          </label>
          <RunButton running={running} disabled={runDisabled}>
            Analyze with Foundry Code Interpreter
          </RunButton>
        </section>

        <section className="dataset-intake-status panel">
          {fileError ? (
            <div className="error-banner" role="alert">
              <ShieldCheck size={15} />
              <span>{fileError}</span>
            </div>
          ) : null}
          {uploadedFile && fileKind ? (
            <div className="upload-actions">
              <button
                type="button"
                className="secondary-button"
                disabled={libraryUploadStatus === "uploading"}
                onClick={() => uploadToLibrary(uploadedFile, fileKind)}
              >
                {libraryUploadStatus === "uploading"
                  ? "Uploading…"
                  : libraryUploadStatus === "uploaded"
                    ? "Uploaded to Library"
                    : "Upload to Library"}
              </button>
              {fileKind === "json" ? (
                <span className="muted-copy">
                  JSON preview only uploads to Library. Deterministic
                  profiling currently supports bounded CSV input.
                </span>
              ) : null}
              {libraryUploadError ? (
                <span className="error-banner" role="alert">
                  {libraryUploadError}
                </span>
              ) : null}
            </div>
          ) : null}
          <label className="check-row plan-approval-check">
            <input
              type="checkbox"
              checked={planApproved}
              onChange={(event) => setPlanApproved(event.target.checked)}
            />
            <span>
              I approve sending this bounded dataset to the Foundry Dataset
              Agent and its project-scoped Code Interpreter. Public/synthetic
              accelerator data only.
            </span>
          </label>
        </section>

        <div className="dataset-grid">
          <section className="panel schema-browser">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">Computed profile</span>
                <h2>Schema & quality</h2>
              </div>
              <span className="subtle-chip">
                {dataset
                  ? dataset.profile_status === "computed"
                    ? `${dataset.row_count} rows · ${dataset.column_count} columns`
                    : "Estimate only · no profile"
                  : "Not profiled"}
              </span>
            </div>
            {dataset?.profile_status === "computed" ? (
              <div className="schema-table" role="table">
                <div className="schema-row schema-head" role="row">
                  <span>Field</span>
                  <span>Type</span>
                  <span>Missing</span>
                  <span>Range / values</span>
                </div>
                {dataset.fields.map((field) => (
                  <div className="schema-row" role="row" key={field.name}>
                    <strong>{field.name}</strong>
                    <span>{field.data_type}</span>
                    <span>{field.missing}</span>
                    <span>{field.range_or_values}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-workspace compact-empty">
                <BarChart3 size={23} />
                <strong>
                  {dataset ? "Asset not profiled" : "No computed profile"}
                </strong>
                <p>
                  {dataset?.profile_note ??
                    "Select an asset and run the deterministic profiler."}
                </p>
              </div>
            )}
            {dataset?.quality_findings.map((finding) => (
              <div className="quality-finding" key={finding}>
                <CheckCircle2 size={15} />
                <span>{finding}</span>
              </div>
            ))}
          </section>

          <section className="panel analysis-notebook">
            <div className="panel-heading">
              <div>
                <span className="eyebrow">Analysis plan</span>
                <h2>Methods before prose</h2>
              </div>
              <span className="subtle-chip">
                {planApproved ? "Plan approved" : "Pending approval"}
              </span>
            </div>
            {(dataset?.analysis_plan ?? [
              {
                id: "profile",
                question: "What are the structure, ranges, and missingness?",
                method: "Deterministic profile",
                status: "ready",
                deterministic: true,
              },
              {
                id: "compare",
                question: "How do observed outcomes differ by group?",
                method: "Descriptive grouped comparison",
                status: "planned",
                deterministic: true,
              },
            ]).map((step, index) => (
              <article className="notebook-cell" key={step.id}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{step.question}</strong>
                  <p>{step.method}</p>
                </div>
                <small>{step.status}</small>
              </article>
            ))}
            {dataset?.interpretation.map((paragraph) => (
              <div className="interpretation-cell" key={paragraph}>
                <Sparkles size={16} />
                <p>{paragraph}</p>
              </div>
            ))}
          </section>

          <aside
            className="panel compute-proposal"
            aria-label="Compute proposal"
          >
            <span className="eyebrow">Execution boundary</span>
            <h2>Compute adapter</h2>
            {dataset ? (
              <>
                <strong>{dataset.compute_proposal.adapter}</strong>
                <dl>
                  <div>
                    <dt>Data size</dt>
                    <dd>
                      {(dataset.compute_proposal.estimated_bytes / 1e9).toFixed(
                        1,
                      )}{" "}
                      GB
                    </dd>
                  </div>
                  <div>
                    <dt>Estimate</dt>
                    <dd>
                      ${dataset.compute_proposal.estimated_cost_usd ?? "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>Duration</dt>
                    <dd>
                      {dataset.compute_proposal.estimated_minutes ?? "—"} min
                    </dd>
                  </div>
                </dl>
                <ol className="adapter-stages">
                  {dataset.compute_proposal.stages.map((stage, index) => (
                    <li key={stage}>
                      <span>{index + 1}</span>
                      {stage}
                    </li>
                  ))}
                </ol>
                {dataset.compute_proposal.approval_required ? (
                  <div className="approval-needed">
                    <Lock size={16} />
                    Human approval required before submit
                  </div>
                ) : (
                  <div className="local-compute">
                    <CheckCircle2 size={16} />
                    Safe for bounded local computation
                  </div>
                )}
              </>
            ) : (
              <p className="muted-copy">
                Large assets receive a cost and duration estimate before any
                external job can be approved.
              </p>
            )}
          </aside>
        </div>
      </form>
      {dataset ? <InsightCard result={dataset} /> : null}
    </div>
  );
}

const CORPUS_SCOPES: {
  id: string;
  label: string;
  detail: string;
  locked: boolean;
}[] = [
  { id: "irb", label: "IRB & human subjects", detail: "18 documents", locked: false },
  { id: "records", label: "Research records", detail: "9 documents", locked: false },
  { id: "governance", label: "Data governance", detail: "14 documents", locked: false },
  { id: "legal_hold", label: "Legal hold", detail: "Restricted", locked: true },
];

export function InstitutionalStudio({
  result,
  running,
  error,
  workflow,
  onRun,
}: StudioProps) {
  const [question, setQuestion] = useState(
    "When must generative AI use be disclosed in an IRB protocol?",
  );
  const [corpusScopes, setCorpusScopes] = useState<Record<string, boolean>>({
    irb: true,
    records: true,
    governance: true,
    legal_hold: false,
  });
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(
    null,
  );
  const institutional =
    result && "abstained" in result
      ? (result as InstitutionalStudioResult)
      : null;

  return (
    <div className="studio-page institutional-studio">
      <StudioHeader
        icon={Landmark}
        eyebrow="Authorized institutional evidence"
        title="Institutional Q&A"
        description="Resolve access and policy versions before answering. If the corpus cannot support an answer, abstain."
        workflow={workflow}
        status="Public web disabled"
      />
      <StudioError message={error} />
      <div className="institutional-grid">
        <aside
          className="panel corpus-scope"
          aria-label="Authorized institutional corpus"
        >
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Retrieval scope</span>
              <h2>Authorized corpus</h2>
            </div>
            <Lock size={17} />
          </div>
          {CORPUS_SCOPES.map((scope) => (
            <label
              className="corpus-row"
              key={scope.id}
              title={
                scope.locked
                  ? "This corpus is under legal hold and cannot be included."
                  : undefined
              }
            >
              <input
                type="checkbox"
                checked={corpusScopes[scope.id]}
                disabled={scope.locked}
                onChange={(event) =>
                  setCorpusScopes((current) => ({
                    ...current,
                    [scope.id]: event.target.checked,
                  }))
                }
              />
              <span>
                <strong>{scope.label}</strong>
                <small>{scope.detail}</small>
              </span>
              {scope.locked ? (
                <Lock size={14} />
              ) : (
                <CheckCircle2 size={15} />
              )}
            </label>
          ))}
          <div className="identity-boundary">
            <ShieldCheck size={18} />
            <div>
              <strong>Identity-bound retrieval</strong>
              <span>demo · researchers · grant-reviewers</span>
            </div>
          </div>
          <div className="web-disabled">
            <Globe2 size={17} />
            <span>
              <strong>Public web unavailable</strong>
              <small>Institutional prompts never route to web search.</small>
            </span>
          </div>
        </aside>

        <section className="qa-workspace">
          <form
            className="question-composer"
            onSubmit={(event) => {
              event.preventDefault();
              void onRun("institutional_qa", question, {
                inputs: {
                  scope: "IRB and research compliance",
                  corpus_scopes: CORPUS_SCOPES.filter(
                    (scope) => !scope.locked && corpusScopes[scope.id],
                  ).map((scope) => scope.id),
                },
              });
            }}
          >
            <label>
              <span className="sr-only">Institutional question</span>
              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                rows={3}
              />
            </label>
            <RunButton running={running}>Resolve policy answer</RunButton>
          </form>
          {institutional ? (
            <article
              className={
                institutional.abstained
                  ? "answer-card abstained-answer"
                  : "answer-card"
              }
            >
              <div className="answer-status">
                {institutional.abstained ? (
                  <CircleDashed size={19} />
                ) : (
                  <ShieldCheck size={19} />
                )}
                <span>
                  <strong>
                    {institutional.abstained
                      ? "Answer gap"
                      : "Grounded answer"}
                  </strong>
                  <small>
                    {institutional.versions.length} effective version
                    {institutional.versions.length === 1 ? "" : "s"} checked
                  </small>
                </span>
              </div>
              <h2>{question}</h2>
              <p>
                {institutional.answer ??
                  "The authorized corpus does not support a reliable answer."}
              </p>
              {institutional.citations.map((citation) => (
                <button
                  className="inline-citation"
                  type="button"
                  key={citation.id}
                  onClick={() => setSelectedCitation(citation)}
                >
                  {citation.title} · {citation.section} · p.
                  {citation.page_start}
                </button>
              ))}
              {institutional.escalation ? (
                <div className="escalation-note">
                  <Users size={16} />
                  {institutional.escalation}
                </div>
              ) : null}
            </article>
          ) : (
            <div className="empty-workspace qa-empty">
              <Landmark size={25} />
              <strong>Ask from authorized policy</strong>
              <p>
                Answers include version, effective date, section, and a direct
                passage—or an explicit abstention.
              </p>
            </div>
          )}
        </section>

        <aside
          className="panel version-inspector"
          aria-label="Policy version inspector"
        >
          <span className="eyebrow">Version resolution</span>
          <h2>Policy timeline</h2>
          {(institutional?.versions ?? []).map((version) => (
            <div className="version-row" key={version.source_id}>
              <span />
              <div>
                <strong>{version.title}</strong>
                <small>
                  v{version.version} · Effective {version.effective_date}
                </small>
              </div>
              <em>{version.status}</em>
            </div>
          ))}
          {!institutional?.versions.length ? (
            <p className="muted-copy">
              Effective and superseded versions appear after retrieval.
            </p>
          ) : null}
          {institutional?.conflicts.map((conflict) => (
            <div className="conflict-card" key={conflict.topic}>
              <strong>{conflict.topic}</strong>
              <p>{conflict.description}</p>
            </div>
          ))}
        </aside>
      </div>

      <section className="panel work-iq-panel" aria-label="Work IQ readiness">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Not configured</span>
            <h2>Work IQ readiness</h2>
          </div>
          <span className="subtle-chip">Disabled by default</span>
        </div>
        <label className="check-row emphasis-check work-iq-toggle">
          <input
            type="checkbox"
            checked={false}
            disabled
            aria-describedby="work-iq-readiness-note"
          />
          <span>
            <strong>Enable Work IQ readiness signals</strong>
            <small>
              Off and disabled — this workspace has not granted the
              tenant-level Microsoft Graph consent Work IQ requires.
            </small>
          </span>
        </label>
        <p id="work-iq-readiness-note" className="muted-copy">
          Turning this on in a real deployment requires an administrator to
          grant Graph consent and configure a Work IQ connector; nothing here
          simulates that connection.
        </p>
      </section>

      {institutional ? <InsightCard result={institutional} /> : null}
      {selectedCitation ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="citation-detail-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">Evidence detail</span>
                <h2 id="citation-detail-title">{selectedCitation.title}</h2>
              </div>
              <button
                aria-label="Close evidence detail"
                onClick={() => setSelectedCitation(null)}
              >
                <X size={19} />
              </button>
            </div>
            <p>{selectedCitation.quote}</p>
            <dl className="citation-detail-facts">
              <div>
                <dt>Section</dt>
                <dd>{selectedCitation.section}</dd>
              </div>
              <div>
                <dt>Page</dt>
                <dd>
                  {selectedCitation.page_start ?? "—"}
                  {selectedCitation.page_end
                    ? `–${selectedCitation.page_end}`
                    : ""}
                </dd>
              </div>
              <div>
                <dt>Source ID</dt>
                <dd>{selectedCitation.source_id}</dd>
              </div>
              <div>
                <dt>Checksum</dt>
                <dd>{selectedCitation.checksum}</dd>
              </div>
              <div>
                <dt>License</dt>
                <dd>{selectedCitation.license}</dd>
              </div>
            </dl>
          </div>
        </div>
      ) : null}
    </div>
  );
}

const AUTOMATION_STEP_KINDS: AutomationStep["kind"][] = [
  "activity",
  "fan_out",
  "agent",
  "approval",
  "external_action",
];
const MAX_WORKFLOW_STEPS = 8;
const MIN_ZOOM = 50;
const MAX_ZOOM = 150;

interface CatalogItem {
  key: string;
  label: string;
  group: "Agent" | "Tool" | "Studio";
  description: string;
  authorized: boolean;
  stepKind: AutomationStep["kind"];
}

const AUTOMATION_STUDIO_CATALOG: {
  id: CapabilityId;
  label: string;
  description: string;
}[] = [
  {
    id: "literature",
    label: "Literature Studio",
    description: "Search, screen, extract, and synthesize evidence.",
  },
  {
    id: "grant",
    label: "Grant Studio",
    description: "Parse notices and draft compliance-mapped packages.",
  },
  {
    id: "matching",
    label: "Matching Explorer",
    description: "Score verified collaborator and resource leads.",
  },
  {
    id: "dataset",
    label: "Dataset Lab",
    description: "Profile bounded datasets deterministically.",
  },
  {
    id: "institutional_qa",
    label: "Institutional Q&A",
    description: "Answer from authorized institutional corpora only.",
  },
];

function buildCatalogItems(data: WorkspaceData | null | undefined): CatalogItem[] {
  const agents: CatalogItem[] = (data?.agents ?? []).map((agent) => ({
    key: `agent-${agent.id}`,
    label: agent.name,
    group: "Agent",
    description: `${agent.deployment} · ${agent.model_tier} tier · ${agent.web_access}`,
    authorized: agent.status === "Active",
    stepKind: "agent",
  }));
  const tools: CatalogItem[] = (data?.connectors ?? []).map((connector) => ({
    key: `tool-${connector.id}`,
    label: connector.name,
    group: "Tool",
    description: connector.description,
    authorized: connector.enabled,
    stepKind: "external_action",
  }));
  const studios: CatalogItem[] = AUTOMATION_STUDIO_CATALOG.map((item) => ({
    key: `studio-${item.id}`,
    label: item.label,
    group: "Studio",
    description: item.description,
    authorized: true,
    stepKind: "fan_out",
  }));
  return [...agents, ...tools, ...studios];
}

function defaultAutomationSteps(): AutomationStep[] {
  return [
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
  ];
}

interface StepDraft {
  label: string;
  kind: AutomationStep["kind"];
  dependsOn: string[];
  retryLimit: number;
  approvalRequired: boolean;
}

export function AutomationStudio({
  result,
  running,
  error,
  workflow,
  onRun,
  data,
  onNavigateToRun,
}: StudioProps) {
  const [template, setTemplate] = useState("evidence-review-v2");
  const [trigger, setTrigger] = useState("Manual");
  const [zoom, setZoom] = useState(100);
  const [steps, setSteps] = useState<AutomationStep[]>(
    defaultAutomationSteps(),
  );
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<StepDraft | null>(null);
  const [addingStep, setAddingStep] = useState(false);
  const [newStepDraft, setNewStepDraft] = useState<StepDraft>({
    label: "",
    kind: "activity",
    dependsOn: [],
    retryLimit: 1,
    approvalRequired: false,
  });
  const [activated, setActivated] = useState(false);
  const [activationConfirmOpen, setActivationConfirmOpen] = useState(false);
  const [catalogPreviewKey, setCatalogPreviewKey] = useState<string | null>(
    null,
  );
  const [graphDraftVersion, setGraphDraftVersion] = useState(1);
  const [cloneStatus, setCloneStatus] = useState<string | null>(null);
  const automation =
    result && "template_id" in result
      ? (result as AutomationStudioResult)
      : null;
  const templates = [
    ["evidence-review-v2", "Evidence review", "Ingest → screen → synthesize"],
    ["grant-review-v2", "Grant red team", "Draft → compliance → approval"],
    ["dataset-profile-v2", "Dataset profile", "Validate → compute → interpret"],
  ];
  const catalogItems = buildCatalogItems(data);
  const catalogLoading = data === null || data === undefined;
  const orchestrationRuns = (data?.runs ?? []).filter(
    (run) => run.capability === "orchestration",
  );

  const dependedOnIds = new Set(steps.flatMap((step) => step.depends_on));
  const canActivate = automation
    ? automation.dry_run_status === "passed" &&
      automation.validation_errors.length === 0
    : false;

  const startEdit = (step: AutomationStep) => {
    setAddingStep(false);
    setEditingId(step.id);
    setDraft({
      label: step.label,
      kind: step.kind,
      dependsOn: step.depends_on,
      retryLimit: step.retry_limit,
      approvalRequired: step.approval_required,
    });
  };

  const saveEdit = (id: string, nextDraft: StepDraft) => {
    setSteps((current) =>
      current.map((step) =>
        step.id === id
          ? {
              ...step,
              label: nextDraft.label.trim() || step.label,
              kind: nextDraft.kind,
              depends_on: nextDraft.dependsOn,
              retry_limit: Math.min(5, Math.max(0, nextDraft.retryLimit)),
              approval_required: nextDraft.approvalRequired,
            }
          : step,
      ),
    );
    setEditingId(null);
    setDraft(null);
  };

  const removeStep = (id: string) => {
    setSteps((current) => current.filter((step) => step.id !== id));
  };

  const addStep = (form: StepDraft & { id: string }) => {
    setSteps((current) => [
      ...current,
      {
        id: form.id,
        label: form.label.trim(),
        kind: form.kind,
        depends_on: form.dependsOn,
        retry_limit: Math.min(5, Math.max(0, form.retryLimit)),
        approval_required: form.approvalRequired,
      },
    ]);
    setAddingStep(false);
  };

  return (
    <div className="studio-page automation-studio">
      <StudioHeader
        icon={Workflow}
        eyebrow="Durable orchestration"
        title="Workflow Automation"
        description="Build typed graphs with retries, compensation, and named human gates—not a generic agent chat."
        workflow={workflow}
        status={
          automation
            ? `Dry run ${automation.dry_run_status}`
            : "Builder draft"
        }
      />
      <StudioError message={error} />
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void onRun(
            "orchestration",
            "Validate and dry run the configured evidence workflow.",
            { inputs: { template_id: template, trigger, steps } },
          );
        }}
      >
        <section className="template-strip">
          {templates.map(([id, title, description]) => (
            <button
              type="button"
              data-active={template === id}
              key={id}
              onClick={() => setTemplate(id)}
            >
              <span className="template-icon">
                <Workflow size={18} />
              </span>
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
              {template === id ? <CheckCircle2 size={17} /> : null}
            </button>
          ))}
          <label className="field trigger-field">
            <span>Trigger</span>
            <select
              value={trigger}
              onChange={(event) => setTrigger(event.target.value)}
            >
              <option>Manual</option>
              <option>Library upload</option>
              <option>Schedule</option>
              <option>API event</option>
            </select>
          </label>
        </section>

        <div className="automation-builder">
          <section className="workflow-canvas">
            <div className="canvas-toolbar">
              <div>
                <span className="eyebrow">
                  Version{" "}
                  {automation ? automation.graph_version : graphDraftVersion.toFixed(1)}
                </span>
                <h2>Evidence review graph</h2>
              </div>
              <div>
                <button
                  type="button"
                  disabled={zoom <= MIN_ZOOM}
                  aria-label="Zoom out"
                  onClick={() =>
                    setZoom((current) => Math.max(MIN_ZOOM, current - 10))
                  }
                >
                  −
                </button>
                <span>{zoom}%</span>
                <button
                  type="button"
                  disabled={zoom >= MAX_ZOOM}
                  aria-label="Zoom in"
                  onClick={() =>
                    setZoom((current) => Math.min(MAX_ZOOM, current + 10))
                  }
                >
                  +
                </button>
              </div>
            </div>
            <div
              className="workflow-graph"
              style={{ transform: `scale(${zoom / 100})` }}
            >
              {steps.map((step, index) => (
                <div className="graph-step-wrap" key={step.id}>
                  <article
                    className="graph-node"
                    data-kind={step.kind}
                    data-approval={step.approval_required}
                  >
                    <span>
                      {step.kind === "agent" ? (
                        <Sparkles size={16} />
                      ) : step.approval_required ? (
                        <Lock size={16} />
                      ) : (
                        <Workflow size={16} />
                      )}
                    </span>
                    <strong>{step.label}</strong>
                    <small>{step.kind.replaceAll("_", " ")}</small>
                    {step.retry_limit ? (
                      <em>{step.retry_limit} retries</em>
                    ) : null}
                  </article>
                  {index < steps.length - 1 ? (
                    <span className="graph-connector" aria-hidden="true" />
                  ) : null}
                </div>
              ))}
            </div>
            <div className="dry-run-console">
              <span className="console-light" />
              <strong>Dry-run validation</strong>
              <span>
                {automation
                  ? `${automation.validation_errors.length} graph errors · ${automation.dry_run_status}`
                  : "Not run · external side effects disabled"}
              </span>
            </div>
            {automation?.validation_errors.length ? (
              <ul className="validation-error-list">
                {automation.validation_errors.map((message) => (
                  <li key={message}>{message}</li>
                ))}
              </ul>
            ) : null}
          </section>

          <aside
            className="panel workflow-inspector"
            aria-label="Workflow execution controls"
          >
            <span className="eyebrow">Graph policy</span>
            <h2>Execution controls</h2>
            <div className="inspector-row">
              <Clock3 size={16} />
              <span>
                <strong>Bounded retries</strong>
                <small>Exponential backoff · max 5</small>
              </span>
            </div>
            <div className="inspector-row">
              <Lock size={16} />
              <span>
                <strong>1 activation gate</strong>
                <small>Review authorizes the exact downstream graph</small>
              </span>
            </div>
            <div className="inspector-row">
              <ShieldCheck size={16} />
              <span>
                <strong>Idempotent actions</strong>
                <small>Destination + artifact version bound</small>
              </span>
            </div>
            <RunButton running={running}>Validate & dry run</RunButton>
            <button
              className="secondary-button full-button"
              type="button"
              disabled={!canActivate || activated}
              title={
                !canActivate
                  ? "Run a passing dry run with zero graph errors before activation."
                  : undefined
              }
              onClick={() => setActivationConfirmOpen(true)}
            >
              {activated ? "Activated (draft workspace)" : "Activate after approval"}
            </button>
          </aside>
        </div>

        <section
          className="panel workflow-catalog"
          aria-label="Workflow capability catalog"
        >
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Authorized capabilities</span>
              <h2>Capability catalog</h2>
            </div>
            <span className="subtle-chip">{catalogItems.length} available</span>
          </div>
          {catalogLoading ? (
            <p className="muted-copy">Loading workspace catalog…</p>
          ) : catalogItems.length ? (
            <div className="step-editor-list">
              {catalogItems.map((item) => {
                const atCapacity = steps.length >= MAX_WORKFLOW_STEPS;
                return (
                  <div className="step-editor-row" key={item.key}>
                    <div>
                      <strong>{item.label}</strong>
                      <small>
                        {item.group}
                        {!item.authorized ? " · not authorized" : ""}
                      </small>
                      {catalogPreviewKey === item.key ? (
                        <p className="muted-copy">{item.description}</p>
                      ) : null}
                    </div>
                    <div className="step-editor-actions">
                      <button
                        type="button"
                        aria-label={`Preview ${item.label}`}
                        onClick={() =>
                          setCatalogPreviewKey((current) =>
                            current === item.key ? null : item.key,
                          )
                        }
                      >
                        {catalogPreviewKey === item.key ? "Hide" : "Preview"}
                      </button>
                      <button
                        type="button"
                        disabled={!item.authorized || atCapacity}
                        title={
                          !item.authorized
                            ? "This capability is not authorized for this workspace yet."
                            : atCapacity
                              ? `Workflow already has the maximum of ${MAX_WORKFLOW_STEPS} steps.`
                              : undefined
                        }
                        onClick={
                          !item.authorized || atCapacity
                            ? undefined
                            : () =>
                                addStep({
                                  id: `${item.key}-${Date.now().toString(36)}`,
                                  label: item.label,
                                  kind: item.stepKind,
                                  dependsOn: [],
                                  retryLimit: 1,
                                  approvalRequired: false,
                                })
                        }
                      >
                        Add to graph
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="muted-copy">
              No authorized agents, tools, or studios are available yet.
            </p>
          )}
        </section>

        <section className="panel step-editor" aria-label="Workflow step editor">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Builder</span>
              <h2>
                Steps ({steps.length}/{MAX_WORKFLOW_STEPS})
              </h2>
            </div>
            <button
              type="button"
              className="secondary-button"
              disabled={steps.length >= MAX_WORKFLOW_STEPS}
              onClick={() => {
                setEditingId(null);
                setNewStepDraft({
                  label: "",
                  kind: "activity",
                  dependsOn: [],
                  retryLimit: 1,
                  approvalRequired: false,
                });
                setAddingStep(true);
              }}
            >
              <Plus size={14} />
              Add step
            </button>
          </div>
          <div className="step-editor-list">
            {steps.map((step) => (
              <div className="step-editor-row" key={step.id}>
                {editingId === step.id && draft ? (
                  <StepDraftForm
                    draft={draft}
                    steps={steps}
                    excludeId={step.id}
                    onDraftChange={setDraft}
                    onCancel={() => {
                      setEditingId(null);
                      setDraft(null);
                    }}
                    onCommit={() => saveEdit(step.id, draft)}
                    commitLabel="Save"
                  />
                ) : (
                  <>
                    <div>
                      <strong>{step.label}</strong>
                      <small>
                        {step.kind.replaceAll("_", " ")} · depends on{" "}
                        {step.depends_on.join(", ") || "none"} ·{" "}
                        {step.retry_limit} retries
                        {step.approval_required ? " · approval gate" : ""}
                      </small>
                    </div>
                    <div className="step-editor-actions">
                      <button
                        type="button"
                        aria-label={`Configure ${step.label}`}
                        onClick={() => startEdit(step)}
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        type="button"
                        aria-label={`Remove ${step.label}`}
                        disabled={steps.length <= 1 || dependedOnIds.has(step.id)}
                        title={
                          dependedOnIds.has(step.id)
                            ? "Another step depends on this one. Remove the dependency first."
                            : steps.length <= 1
                              ? "A workflow needs at least one step."
                              : undefined
                        }
                        onClick={
                          steps.length <= 1 || dependedOnIds.has(step.id)
                            ? undefined
                            : () => removeStep(step.id)
                        }
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
          {addingStep ? (
            <StepDraftForm
              draft={newStepDraft}
              steps={steps}
              onDraftChange={setNewStepDraft}
              onCancel={() => setAddingStep(false)}
              onCommit={() =>
                addStep({
                  ...newStepDraft,
                  id: `step-${Date.now().toString(36)}`,
                })
              }
              commitLabel="Add"
              isNew
            />
          ) : null}
        </section>
      </form>

      <section
        className="panel workflow-run-manager"
        aria-label="Workflow run management"
      >
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Durable execution</span>
            <h2>Run management</h2>
          </div>
          <span className="subtle-chip">{orchestrationRuns.length} runs</span>
        </div>
        {cloneStatus ? (
          <div className="save-status" role="status">
            {cloneStatus}
          </div>
        ) : null}
        {orchestrationRuns.length ? (
          <div className="step-editor-list">
            {orchestrationRuns.map((run) => (
              <div className="step-editor-row" key={run.id}>
                <div>
                  <strong>{run.title}</strong>
                  <small>
                    {run.durable_instance_id} · Graph{" "}
                    {automation && automation.run.id === run.id
                      ? automation.graph_version
                      : graphDraftVersion.toFixed(1)}
                  </small>
                </div>
                <em className={`table-status ${run.status}`}>
                  {run.status.replaceAll("_", " ")}
                </em>
                <div className="step-editor-actions">
                  <button
                    type="button"
                    disabled={!onNavigateToRun}
                    onClick={() => onNavigateToRun?.(run.id)}
                  >
                    Inspect
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setSteps((current) =>
                        current.map((step) => ({ ...step })),
                      );
                      setActivated(false);
                      setGraphDraftVersion((current) => current + 1);
                      setCloneStatus(
                        `Cloned ${run.title} into a new draft (v${(
                          graphDraftVersion + 1
                        ).toFixed(1)}). Validate and dry run before activating.`,
                      );
                    }}
                  >
                    Clone
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Pausing requires the Durable Task Scheduler control plane, which this workspace does not expose yet."
                  >
                    Pause
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Resuming requires the Durable Task Scheduler control plane, which this workspace does not expose yet."
                  >
                    Resume
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Retrying requires the Durable Task Scheduler control plane, which this workspace does not expose yet."
                  >
                    Retry
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Cancelling requires the Durable Task Scheduler control plane, which this workspace does not expose yet."
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted-copy">
            No durable orchestration runs yet. Validate and dry run to create
            one.
          </p>
        )}
      </section>
      {automation ? <InsightCard result={automation} /> : null}
      {activationConfirmOpen ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="activate-workflow-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">Confirm activation</span>
                <h2 id="activate-workflow-title">
                  Activate graph {automation?.graph_version ?? "2.0"}
                </h2>
              </div>
              <button
                aria-label="Close activation dialog"
                onClick={() => setActivationConfirmOpen(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              This authorizes the exact validated graph
              {automation?.graph_hash
                ? ` (hash ${automation.graph_hash.slice(0, 12)}…)`
                : ""}{" "}
              to run on its trigger. Activation is recorded only in this
              workspace session — connect a real approval and scheduling
              system before production use.
            </p>
            <div className="modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setActivationConfirmOpen(false)}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => {
                  setActivated(true);
                  setActivationConfirmOpen(false);
                }}
              >
                Confirm activation
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function StepDraftForm({
  draft,
  steps,
  excludeId,
  onDraftChange,
  onCancel,
  onCommit,
  commitLabel,
  isNew,
}: {
  draft: StepDraft;
  steps: AutomationStep[];
  excludeId?: string;
  onDraftChange: (draft: StepDraft) => void;
  onCancel: () => void;
  onCommit: () => void;
  commitLabel: string;
  isNew?: boolean;
}) {
  const dependencyOptions = steps.filter((step) => step.id !== excludeId);
  return (
    <div className="step-editor-form">
      <label className="field">
        <span>Step label</span>
        <input
          value={draft.label}
          onChange={(event) =>
            onDraftChange({ ...draft, label: event.target.value })
          }
        />
      </label>
      <label className="field">
        <span>Kind</span>
        <select
          value={draft.kind}
          onChange={(event) =>
            onDraftChange({
              ...draft,
              kind: event.target.value as AutomationStep["kind"],
            })
          }
        >
          {AUTOMATION_STEP_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind.replaceAll("_", " ")}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Retry limit (0-5)</span>
        <input
          type="number"
          min={0}
          max={5}
          value={draft.retryLimit}
          onChange={(event) =>
            onDraftChange({ ...draft, retryLimit: Number(event.target.value) })
          }
        />
      </label>
      {dependencyOptions.length ? (
        <fieldset className="step-depends-on">
          <legend>Depends on</legend>
          {dependencyOptions.map((option) => (
            <label className="check-row" key={option.id}>
              <input
                type="checkbox"
                checked={draft.dependsOn.includes(option.id)}
                onChange={(event) =>
                  onDraftChange({
                    ...draft,
                    dependsOn: event.target.checked
                      ? [...draft.dependsOn, option.id]
                      : draft.dependsOn.filter((id) => id !== option.id),
                  })
                }
              />
              <span>{option.label}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <label className="check-row">
        <input
          type="checkbox"
          checked={draft.approvalRequired}
          onChange={(event) =>
            onDraftChange({ ...draft, approvalRequired: event.target.checked })
          }
        />
        <span>Approval required</span>
      </label>
      <div className="step-editor-actions">
        <button type="button" className="secondary-button" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="primary-button"
          disabled={isNew ? !draft.label.trim() : false}
          onClick={onCommit}
        >
          {commitLabel}
        </button>
      </div>
    </div>
  );
}

export function StudioForCapability({
  capability,
  ...props
}: StudioProps & { capability: CapabilityId }) {
  switch (capability) {
    case "literature":
      return <LiteratureStudio {...props} />;
    case "grant":
      return <GrantStudio {...props} />;
    case "matching":
      return <MatchingStudio {...props} />;
    case "dataset":
      return <DatasetStudio {...props} />;
    case "institutional_qa":
      return <InstitutionalStudio {...props} />;
    case "orchestration":
      return <AutomationStudio {...props} />;
  }
}
