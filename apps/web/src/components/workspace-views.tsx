"use client";

import {
  ArrowUpRight,
  BarChart3,
  BookOpen,
  CheckCircle2,
  CircleDashed,
  Clock3,
  FileSearch2,
  FileText,
  FlaskConical,
  Globe2,
  History,
  Landmark,
  Library,
  Lock,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Upload,
  Users,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  decideApproval,
  testConnector,
  updateConnector,
  updateSettings,
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";
import { PolicyGatedExternalLink } from "@/components/policy-gated-external-link";
import type {
  ApprovalRecord,
  CapabilityId,
  ConnectorSetting,
  LibraryItem,
  ProjectSettings,
} from "@/lib/types";

export type WorkspaceViewId =
  | "overview"
  | "library"
  | "runs"
  | "settings"
  | CapabilityId;

export interface CapabilityCard {
  id: CapabilityId;
  title: string;
  shortTitle: string;
  description: string;
  eyebrow: string;
  icon: LucideIcon;
  accent: string;
  artifact: string;
  stages: string[];
}

interface OverviewProps {
  data: WorkspaceData | null;
  capabilities: CapabilityCard[];
  onNavigate: (view: WorkspaceViewId) => void;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

export function statusLabel(value: string): string {
  if (value === "configuration_required") return "setup required";
  if (value === "ready_with_key") return "ready, key recommended";
  return value.replaceAll("_", " ");
}

const CONNECTOR_SPECIALISTS = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "institution",
] as const;

function connectorStatusInfo(connector: ConnectorSetting): {
  label: string;
  detail: string;
  tone: string;
} {
  if (!connector.enabled) {
    return {
      label: "Disabled",
      detail:
        "This connector is intentionally disabled and will not be used by research runs.",
      tone: "disabled",
    };
  }
  if (connector.test_status === "configuration_required") {
    return {
      label: "Setup required",
      detail:
        "The provider is not down. An administrator must configure the connector gateway URL and managed identity before tests can reach it.",
      tone: "configuration-required",
    };
  }
  if (connector.test_status === "unavailable") {
    return {
      label: "Connection failed",
      detail:
        "The gateway is configured, but the latest bounded provider probe failed. Retry the test or inspect gateway logs before using this source.",
      tone: "unavailable",
    };
  }
  if (connector.test_status === "ready_with_key") {
    return {
      label: "Ready, key recommended",
      detail:
        "The connector is reachable with limited anonymous quota. Add the optional deployment-managed key for more reliable capacity.",
      tone: "warning",
    };
  }
  if (connector.test_status === "ready") {
    return {
      label: "Ready",
      detail:
        "The latest bounded probe succeeded and this connector can serve its assigned specialists.",
      tone: "ready",
    };
  }
  return {
    label: "Not tested",
    detail: "Run a bounded connection test before relying on this source.",
    tone: "untested",
  };
}

function evaluationState(
  score: number,
  gate: string,
): "ready" | "blocked" | "degraded" {
  if (score === 100) return "ready";
  if (gate === "Blocking") return "blocked";
  return "degraded";
}

export function Overview({
  data,
  capabilities,
  onNavigate,
}: OverviewProps) {
  const runs = data?.runs.slice(0, 4) ?? [];
  const summary = data?.summary;
  return (
    <div className="overview-page">
      <section className="overview-hero">
        <div className="hero-copy">
          <span className="eyebrow">Research command center</span>
          <h1>
            Move from question to
            <em> defensible evidence.</em>
          </h1>
          <p>
            Six purpose-built studios combine Microsoft Foundry Hosted Agents
            with deterministic retrieval, policy, calculations, provenance,
            and human approval.
          </p>
          <div className="hero-actions">
            <button
              className="primary-button"
              onClick={() => onNavigate("literature")}
            >
              <BookOpen size={17} />
              Start a literature review
            </button>
            <button
              className="secondary-button"
              onClick={() => onNavigate("library")}
            >
              Explore evidence library
              <ArrowUpRight size={16} />
            </button>
          </div>
        </div>
        <div className="hero-system-card">
          <div className="system-card-top">
            <span>
              <ShieldCheck size={17} />
              Evidence control plane
            </span>
            <em>Healthy</em>
          </div>
          <div className="system-orbit">
            <span className="orbit-ring orbit-one" />
            <span className="orbit-ring orbit-two" />
            <span className="orbit-core">
              <Sparkles size={24} />
            </span>
            <span className="orbit-node node-one">Search</span>
            <span className="orbit-node node-two">Foundry</span>
            <span className="orbit-node node-three">Approval</span>
          </div>
          <div className="system-card-metrics">
            <span>
              <strong>100%</strong>
              claim coverage gate
            </span>
            <span>
              <strong>Off</strong>
              web by default
            </span>
          </div>
        </div>
      </section>

      <section className="overview-metrics" aria-label="Workspace metrics">
        <article>
          <span className="metric-icon sage">
            <Library size={18} />
          </span>
          <div>
            <strong>{summary?.library_items ?? 9}</strong>
            <span>Governed library items</span>
          </div>
          <small>Checksum, license & ACL</small>
        </article>
        <article>
          <span className="metric-icon amber">
            <Clock3 size={18} />
          </span>
          <div>
            <strong>{summary?.active_runs ?? 2}</strong>
            <span>Active durable runs</span>
          </div>
          <small>Resumable & auditable</small>
        </article>
        <article>
          <span className="metric-icon rose">
            <Lock size={18} />
          </span>
          <div>
            <strong>{summary?.pending_approvals ?? 2}</strong>
            <span>Actions awaiting approval</span>
          </div>
          <small>Exact action + destination</small>
        </article>
        <article>
          <span className="metric-icon blue">
            <Globe2 size={18} />
          </span>
          <div>
            <strong>
              {summary?.connector_ready ?? 12}/{summary?.connector_total ?? 12}
            </strong>
            <span>Research connectors ready</span>
          </div>
          <small>Public metadata only</small>
        </article>
      </section>

      <section className="overview-section">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Purpose-built workspaces</span>
            <h2>Choose the artifact, not a generic chatbot</h2>
          </div>
          <p>
            Each studio has a different contract, evidence boundary, workflow,
            and release gate.
          </p>
        </div>
        <div className="capability-grid">
          {capabilities.map((capability, index) => {
            const Icon = capability.icon;
            return (
              <button
                className={`capability-card ${capability.accent}`}
                key={capability.id}
                onClick={() => onNavigate(capability.id)}
                aria-label={capability.title}
              >
                <span className="card-index">0{index + 1}</span>
                <span className="capability-icon">
                  <Icon size={20} />
                </span>
                <span className="capability-copy">
                  <small>{capability.eyebrow}</small>
                  <strong>{capability.title}</strong>
                  <span>{capability.description}</span>
                </span>
                <span className="capability-stages">
                  {capability.stages.slice(0, 4).map((stage) => (
                    <small key={stage}>{stage}</small>
                  ))}
                </span>
                <span className="capability-artifact">
                  <span>{capability.artifact}</span>
                  <ArrowUpRight size={16} />
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="overview-bottom-grid">
        <article className="panel work-in-motion">
          <div className="panel-heading">
            <div>
              <span className="eyebrow">Live workspace</span>
              <h2>Work in motion</h2>
            </div>
            <button onClick={() => onNavigate("runs")}>
              View all runs <ArrowUpRight size={15} />
            </button>
          </div>
          {runs.length ? (
            <div className="run-list">
              {runs.map((run) => (
                <button key={run.id} onClick={() => onNavigate("runs")}>
                  <span className={`run-kind ${run.capability}`}>
                    {run.capability === "literature" ? (
                      <BookOpen size={16} />
                    ) : run.capability === "grant" ? (
                      <FileText size={16} />
                    ) : run.capability === "dataset" ? (
                      <BarChart3 size={16} />
                    ) : (
                      <Landmark size={16} />
                    )}
                  </span>
                  <span className="run-copy">
                    <strong>{run.title}</strong>
                    <small>
                      {statusLabel(run.current_stage)} · {run.owner}
                    </small>
                  </span>
                  <span className="mini-progress">
                    <i style={{ width: `${run.progress}%` }} />
                  </span>
                  <em>{run.progress}%</em>
                </button>
              ))}
            </div>
          ) : (
            <div className="loading-block">Loading durable runs…</div>
          )}
        </article>

        <article className="panel governance-board">
          <span className="eyebrow">Release confidence</span>
          <h2>Governance is product state</h2>
          <div className="governance-score">
            <strong>96</strong>
            <span>/100</span>
            <small>Current demo evaluation</small>
          </div>
          {[
            ["Citation resolution", "Passed"],
            ["Tenant & ACL boundary", "Passed"],
            ["Unsupported claims", "0"],
            ["Online research", "Opt-in"],
          ].map(([label, value]) => (
            <div className="governance-row" key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </article>
      </section>
    </div>
  );
}

interface LibraryViewProps {
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
}

export function LibraryView({ data, onRefresh }: LibraryViewProps) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("All");
  const [importOpen, setImportOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailItem, setDetailItem] = useState<LibraryItem | null>(null);
  const detailTags = detailItem?.tags ?? [];
  const items = useMemo(
    () =>
      (data?.library ?? []).filter(
        (item) =>
          (kind === "All" || item.kind === kind) &&
          `${item.title} ${item.source} ${(item.tags ?? []).join(" ")}`
            .toLowerCase()
            .includes(query.toLowerCase()),
      ),
    [data?.library, kind, query],
  );
  const kinds = ["All", ...new Set((data?.library ?? []).map((item) => item.kind))];

  return (
    <div className="operational-page library-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Governed evidence</span>
          <h1>Library</h1>
          <p>
            Inspect source, version, license, checksum, access, and indexing
            state before evidence enters a workflow.
          </p>
        </div>
        <button className="primary-button" onClick={() => setImportOpen(true)}>
          <Upload size={17} />
          Ingest source
        </button>
      </header>

      <section className="library-toolbar">
        <label className="search-field">
          <Search size={17} />
          <span className="sr-only">Search library</span>
          <input
            placeholder="Search title, source, or tag"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <div className="filter-pills" aria-label="Filter library by type">
          {kinds.map((itemKind) => (
            <button
              data-active={kind === itemKind}
              key={itemKind}
              onClick={() => setKind(itemKind)}
            >
              {itemKind}
            </button>
          ))}
        </div>
      </section>

      <section className="library-table" aria-label="Evidence library items">
        <div className="library-row library-head">
          <span>Source</span>
          <span>Type & origin</span>
          <span>Governance</span>
          <span>Evidence</span>
          <span>Status</span>
        </div>
        {items.map((item) => (
          <button
            type="button"
            className="library-row"
            key={item.id}
            onClick={() => setDetailItem(item)}
            aria-haspopup="dialog"
          >
            <span className="library-title-cell">
              <span className="source-kind-icon">
                {item.kind === "Paper" ? (
                  <BookOpen size={17} />
                ) : item.kind === "Dataset" ? (
                  <BarChart3 size={17} />
                ) : item.kind === "Policy" ? (
                  <Landmark size={17} />
                ) : (
                  <FileText size={17} />
                )}
              </span>
              <span>
                <strong>{item.title}</strong>
                <small>{item.description}</small>
              </span>
            </span>
            <span>
              <strong>{item.kind}</strong>
              <small>{item.source}</small>
            </span>
            <span>
              <strong>{item.access}</strong>
              <small>
                v{item.version} · {item.license}
              </small>
            </span>
            <span>
              <strong>{item.evidence_count} passages</strong>
              <small>{item.checksum}</small>
            </span>
            <span>
              <em className={`table-status ${item.status}`}>
                {statusLabel(item.status)}
              </em>
              <small>{formatTime(item.added_at)}</small>
            </span>
          </button>
        ))}
        {!items.length ? (
          <div className="empty-table">
            <FileSearch2 size={23} />
            <strong>No sources match this view</strong>
          </div>
        ) : null}
      </section>

      {detailItem ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="library-detail-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">{detailItem.kind}</span>
                <h2 id="library-detail-title">{detailItem.title}</h2>
              </div>
              <button
                aria-label="Close source detail"
                onClick={() => setDetailItem(null)}
              >
                <X size={19} />
              </button>
            </div>
            <p>{detailItem.description}</p>
            <dl className="library-detail-facts">
              <div>
                <dt>Source</dt>
                <dd>{detailItem.source}</dd>
              </div>
              <div>
                <dt>Provider</dt>
                <dd>{detailItem.provider}</dd>
              </div>
              <div>
                <dt>Connector</dt>
                <dd>{detailItem.connector}</dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>{detailItem.version}</dd>
              </div>
              <div>
                <dt>License</dt>
                <dd>{detailItem.license}</dd>
              </div>
              <div>
                <dt>Access</dt>
                <dd>{detailItem.access}</dd>
              </div>
              <div>
                <dt>Checksum</dt>
                <dd>{detailItem.checksum}</dd>
              </div>
              <div>
                <dt>Evidence passages</dt>
                <dd>{detailItem.evidence_count}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{statusLabel(detailItem.status)}</dd>
              </div>
              <div>
                <dt>Added</dt>
                <dd>{formatTime(detailItem.added_at)}</dd>
              </div>
              {detailItem.publication_year ? (
                <div>
                  <dt>Publication year</dt>
                  <dd>{detailItem.publication_year}</dd>
                </div>
              ) : null}
              {detailItem.size_bytes ? (
                <div>
                  <dt>Size</dt>
                  <dd>{(detailItem.size_bytes / 1_000_000).toFixed(2)} MB</dd>
                </div>
              ) : null}
              {detailItem.content_type ? (
                <div>
                  <dt>Content type</dt>
                  <dd>{detailItem.content_type}</dd>
                </div>
              ) : null}
            </dl>
            {detailTags.length ? (
              <div className="tag-list">
                {detailTags.map((tag) => (
                  <span key={tag}>{tag}</span>
                ))}
              </div>
            ) : null}
            <div className="modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setDetailItem(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {importOpen ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="ingest-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">Durable ingestion</span>
                <h2 id="ingest-title">Add source to Library</h2>
              </div>
              <button
                aria-label="Close ingest dialog"
                onClick={() => setImportOpen(false)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              The source enters a staged pipeline: extract structure, apply
              checksum/license/version/ACL, then chunk and index.
            </p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                setSubmitting(true);
                setError(null);
                void uploadLibraryItem(form)
                  .then(async () => {
                    await onRefresh();
                    setImportOpen(false);
                  })
                  .catch((ingestError: unknown) =>
                    setError(
                      ingestError instanceof Error
                        ? ingestError.message
                        : "Ingestion could not be queued.",
                    ),
                  )
                  .finally(() => setSubmitting(false));
              }}
            >
              <label className="field">
                <span>Title</span>
                <input name="title" required minLength={3} />
              </label>
              <div className="field-row">
                <label className="field">
                  <span>Type</span>
                  <select name="kind" defaultValue="Paper">
                    <option>Paper</option>
                    <option>Policy</option>
                    <option>Dataset</option>
                    <option>Funding notice</option>
                    <option>Template</option>
                  </select>
                </label>
                <label className="field">
                  <span>Access</span>
                  <select name="access" defaultValue="internal">
                    <option>public</option>
                    <option>internal</option>
                    <option>restricted</option>
                  </select>
                </label>
              </div>
              <label className="field">
                <span>Source file</span>
                <input
                  name="file"
                  type="file"
                  required
                  accept=".pdf,.txt,.md,.csv,.json,application/pdf,text/plain,text/markdown,text/csv,application/json"
                />
              </label>
              <label className="field">
                <span>License / terms</span>
                <input name="license" required defaultValue="Project supplied" />
              </label>
              <div className="field-row">
                <label className="field">
                  <span>Provider / origin</span>
                  <input
                    name="source"
                    required
                    defaultValue="Workspace upload"
                  />
                </label>
                <label className="field">
                  <span>Publication year</span>
                  <input
                    name="publication_year"
                    type="number"
                    min={1000}
                    max={2100}
                    defaultValue={2026}
                  />
                </label>
              </div>
              <label className="field">
                <span>Description</span>
                <textarea name="description" required rows={3} />
              </label>
              {error ? (
                <div className="error-banner" role="alert">
                  {error}
                </div>
              ) : null}
              <div className="modal-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => setImportOpen(false)}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={submitting}
                >
                  {submitting ? "Queuing…" : "Start ingestion"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface RunsViewProps {
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
  focusRunId?: string | null;
}

export function RunsView({ data, onRefresh, focusRunId }: RunsViewProps) {
  const [filter, setFilter] = useState("All");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(
    focusRunId ?? null,
  );
  const [rationales, setRationales] = useState<Record<string, string>>({});
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState(false);
  const runs = data?.runs ?? [];
  const filtered = runs.filter((run) => {
    if (filter === "All") return true;
    if (filter === "Needs approval")
      return run.status === "waiting_for_approval";
    return statusLabel(run.status) === filter.toLowerCase();
  });
  const selected =
    filtered.find((run) => run.id === selectedRunId) ?? filtered[0] ?? null;
  const selectedStages = selected?.stages ?? [];
  const approval = data?.approvals.find(
    (item) => item.run_id === selected?.id && item.state === "pending",
  );
  const rationale = approval ? (rationales[approval.id] ?? "") : "";

  const decide = (record: ApprovalRecord, decision: "approved" | "rejected") => {
    if (rationale.trim().length < 3) {
      setDecisionError("Add a rationale before recording a decision.");
      return;
    }
    setDeciding(true);
    setDecisionError(null);
    void decideApproval(record.id, decision, rationale)
      .then(async () => {
        setRationales((current) => {
          const next = { ...current };
          delete next[record.id];
          return next;
        });
        await onRefresh();
      })
      .catch((error: unknown) =>
        setDecisionError(
          error instanceof Error ? error.message : "Decision could not be saved.",
        ),
      )
      .finally(() => setDeciding(false));
  };

  return (
    <div className="operational-page runs-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Durable execution</span>
          <h1>Runs & Approvals</h1>
          <p>
            Every displayed run maps to a durable instance. Consequential
            actions remain paused until a named reviewer records a decision.
          </p>
        </div>
        <div className="header-stat">
          <strong>
            {data?.approvals.filter((item) => item.state === "pending").length ??
              0}
          </strong>
          <span>decisions pending</span>
        </div>
      </header>

      <div className="runs-layout">
        <section className="panel runs-list-panel">
          <div className="runs-tabs" aria-label="Filter runs">
            {["All", "Needs approval", "Running", "Completed"].map((item) => (
              <button
                data-active={filter === item}
                key={item}
                onClick={() => setFilter(item)}
              >
                {item}
              </button>
            ))}
          </div>
          <div className="detailed-run-list">
            {filtered.map((run) => (
              <button
                data-active={selected?.id === run.id}
                key={run.id}
                onClick={() => setSelectedRunId(run.id)}
              >
                <span className={`run-kind ${run.capability}`}>
                  {run.capability === "literature" ? (
                    <BookOpen size={16} />
                  ) : run.capability === "grant" ? (
                    <FileText size={16} />
                  ) : run.capability === "matching" ? (
                    <Users size={16} />
                  ) : run.capability === "dataset" ? (
                    <BarChart3 size={16} />
                  ) : run.capability === "institutional_qa" ? (
                    <Landmark size={16} />
                  ) : (
                    <Workflow size={16} />
                  )}
                </span>
                <span className="run-copy">
                  <strong>{run.title}</strong>
                  <small>
                    {run.durable_instance_id} · {formatTime(run.started_at)}
                  </small>
                  <span className="run-progress-line">
                    <i style={{ width: `${run.progress}%` }} />
                  </span>
                </span>
                <span>
                  <em className={`table-status ${run.status}`}>
                    {statusLabel(run.status)}
                  </em>
                  <small>{run.progress}%</small>
                </span>
              </button>
            ))}
          </div>
        </section>

        <section className="run-detail">
          {selected ? (
            <>
              <article className="panel run-overview">
                <div className="panel-heading">
                  <div>
                    <span className="eyebrow">{selected.capability}</span>
                    <h2>{selected.title}</h2>
                  </div>
                  <em className={`table-status ${selected.status}`}>
                    {statusLabel(selected.status)}
                  </em>
                </div>
                <dl className="run-facts">
                  <div>
                    <dt>Durable instance</dt>
                    <dd>{selected.durable_instance_id}</dd>
                  </div>
                  <div>
                    <dt>Owner</dt>
                    <dd>{selected.owner}</dd>
                  </div>
                  <div>
                    <dt>Artifacts</dt>
                    <dd>{selected.artifact_count}</dd>
                  </div>
                  <div>
                    <dt>Runtime</dt>
                    <dd>
                      {selected.scheduler_managed
                        ? "Durable Task Scheduler"
                        : "Durable demo snapshot"}
                    </dd>
                  </div>
                </dl>
                <div className="run-timeline">
                  {selectedStages.length ? (
                    selectedStages.map((stage) => (
                      <div key={stage.id} data-status={stage.status}>
                        <span>
                          {stage.status === "completed" ? (
                            <CheckCircle2 size={16} />
                          ) : (
                            <CircleDashed size={16} />
                          )}
                        </span>
                        <div>
                          <strong>{stage.label}</strong>
                          <small>{stage.owner}</small>
                        </div>
                        <em>{statusLabel(stage.status)}</em>
                      </div>
                    ))
                  ) : (
                    <div data-status="completed">
                      <span>
                        <CheckCircle2 size={16} />
                      </span>
                      <div>
                        <strong>{selected.current_stage}</strong>
                        <small>Durable execution state</small>
                      </div>
                      <em>{selected.progress}%</em>
                    </div>
                  )}
                </div>
              </article>

              {approval ? (
                <article className="approval-card">
                  <div className="approval-card-heading">
                    <span className="approval-lock">
                      <Lock size={18} />
                    </span>
                    <div>
                      <span className="eyebrow">{approval.risk} risk</span>
                      <h2>{approval.title}</h2>
                    </div>
                  </div>
                  <div className="approval-boundary">
                    <div>
                      <span>Exact gated action</span>
                      <strong>{approval.gated_action}</strong>
                    </div>
                    <div>
                      <span>Destination</span>
                      <strong>{approval.destination}</strong>
                    </div>
                    <div>
                      <span>Evidence summary</span>
                      <p>{approval.evidence_summary}</p>
                    </div>
                    <div>
                      <span>Idempotency key</span>
                      <code>{approval.idempotency_key}</code>
                    </div>
                  </div>
                  <label className="field">
                    <span>Reviewer rationale</span>
                    <textarea
                      value={rationale}
                      disabled={deciding}
                      onChange={(event) =>
                        setRationales((current) => ({
                          ...current,
                          [approval.id]: event.target.value,
                        }))
                      }
                      rows={3}
                      placeholder="Record why this exact action should proceed or remain blocked."
                    />
                  </label>
                  {decisionError ? (
                    <div className="error-banner" role="alert">
                      {decisionError}
                    </div>
                  ) : null}
                  <div className="approval-actions">
                    <button
                      className="danger-button"
                      disabled={deciding}
                      onClick={() => decide(approval, "rejected")}
                    >
                      Reject action
                    </button>
                    <button
                      className="primary-button"
                      disabled={deciding}
                      onClick={() => decide(approval, "approved")}
                    >
                      <CheckCircle2 size={16} />
                      Approve exact action
                    </button>
                  </div>
                </article>
              ) : (
                <article className="panel no-approval-card">
                  <ShieldCheck size={22} />
                  <div>
                    <strong>No pending decision</strong>
                    <p>
                      This run has no consequential action waiting for the
                      current reviewer.
                    </p>
                  </div>
                </article>
              )}
            </>
          ) : (
            <div className="empty-workspace">
              <History size={24} />
              <strong>No durable runs available</strong>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

interface SettingsViewProps {
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
}

const SETTINGS_TABS = [
  "General",
  "Agents & Models",
  "Connectors",
  "Retrieval & Evidence",
  "Governance",
  "Evaluation",
  "Readiness",
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number];

const GATEWAY_VERSION_TARGETS: {
  id: string;
  label: string;
  pattern: RegExp;
}[] = [
  { id: "apim", label: "Azure API Management (APIM)", pattern: /apim/i },
  { id: "mcp", label: "MCP tool registry", pattern: /mcp/i },
  { id: "toolbox", label: "Toolbox", pattern: /toolbox/i },
];

export function SettingsView({ data, onRefresh }: SettingsViewProps) {
  const [tab, setTab] = useState<SettingsTab>("General");
  const [draft, setDraft] = useState<ProjectSettings | null>(
    data?.settings ?? null,
  );
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{
    message: string;
    tone: "success" | "warning" | "error";
  } | null>(null);
  const [connectorQuery, setConnectorQuery] = useState("");
  const [connectorCategory, setConnectorCategory] = useState("All");
  const [busyConnector, setBusyConnector] = useState<{
    id: string;
    action: "saving" | "testing";
  } | null>(null);
  const [managedConnectorId, setManagedConnectorId] = useState(
    data?.connectors[0]?.id ?? "",
  );
  const connectors = data?.connectors ?? [];
  const managedConnector =
    connectors.find((connector) => connector.id === managedConnectorId) ??
    connectors[0] ??
    null;
  const [connectorDrafts, setConnectorDrafts] = useState<
    Record<string, { enabled: boolean; assigned_agents: string[] }>
  >({});
  const connectorDraft = managedConnector
    ? (connectorDrafts[managedConnector.id] ?? {
        enabled: managedConnector.enabled,
        assigned_agents: managedConnector.assigned_agents,
      })
    : null;

  const connectorCategories = [
    "All",
    ...new Set(connectors.map((item) => item.category)),
  ];
  const visibleConnectors = connectors.filter(
    (connector) =>
      (connectorCategory === "All" ||
        connector.category === connectorCategory) &&
      `${connector.name} ${connector.description}`
        .toLowerCase()
        .includes(connectorQuery.toLowerCase()),
  );
  const gatewayVersionCards = GATEWAY_VERSION_TARGETS.map((target) => ({
    ...target,
    connector: connectors.find(
      (connector) =>
        target.pattern.test(connector.id) ||
        target.pattern.test(connector.category),
    ),
  }));
  const managedConnectorStatus = managedConnector
    ? connectorStatusInfo(managedConnector)
    : null;
  const mutateConnector = (
    connector: ConnectorSetting,
    update: Partial<ConnectorSetting>,
  ) => {
    const nextConnector = { ...connector, ...update };
    if (nextConnector.enabled && nextConnector.assigned_agents.length === 0) {
      setStatus({
        message:
          "Enabled connectors must be assigned to at least one specialist.",
        tone: "error",
      });
      return;
    }
    setBusyConnector({ id: connector.id, action: "saving" });
    setStatus(null);
    void updateConnector(nextConnector)
      .then(async () => {
        await onRefresh();
        setConnectorDrafts((current) => {
          const next = { ...current };
          delete next[connector.id];
          return next;
        });
        setStatus({
          message: `${connector.name} configuration saved.`,
          tone: "success",
        });
      })
      .catch((error: unknown) =>
        setStatus({
          message:
            error instanceof Error ? error.message : "Connector update failed.",
          tone: "error",
        }),
      )
      .finally(() => setBusyConnector(null));
  };

  const runConnectorTest = (connector: ConnectorSetting) => {
    setBusyConnector({ id: connector.id, action: "testing" });
    setStatus(null);
    void testConnector(connector.id)
      .then(async (updated) => {
        await onRefresh();
        const updatedStatus = connectorStatusInfo(updated);
        setStatus({
          message: `${updated.name}: ${updatedStatus.label}. ${updatedStatus.detail}`,
          tone:
            updatedStatus.tone === "unavailable"
              ? "error"
              : ["configuration-required", "warning", "untested"].includes(
                    updatedStatus.tone,
                  )
                ? "warning"
                : "success",
        });
      })
      .catch((error: unknown) =>
        setStatus({
          message:
            error instanceof Error ? error.message : "Connector test failed.",
          tone: "error",
        }),
      )
      .finally(() => setBusyConnector(null));
  };

  return (
    <div className="operational-page settings-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Project control plane</span>
          <h1>Project Settings</h1>
          <p>
            Configure agents, connectors, retrieval, release policy, and
            evaluations without exposing secret values.
          </p>
        </div>
        <span className="settings-environment">
          <span />
          Foundry development
        </span>
      </header>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SETTINGS_TABS.map((item) => (
            <button
              data-active={tab === item}
              key={item}
              onClick={() => setTab(item)}
            >
              {item === "General" ? (
                <Settings size={16} />
              ) : item === "Agents & Models" ? (
                <Sparkles size={16} />
              ) : item === "Connectors" ? (
                <Globe2 size={16} />
              ) : item === "Retrieval & Evidence" ? (
                <Search size={16} />
              ) : item === "Governance" ? (
                <ShieldCheck size={16} />
              ) : item === "Evaluation" ? (
                <BarChart3 size={16} />
              ) : (
                <Lock size={16} />
              )}
              {item}
              {item === "Connectors" ? (
                <em>{data?.connectors.length ?? 12}</em>
              ) : null}
            </button>
          ))}
        </nav>

        <main className="settings-content">
          {status ? (
            <div className={`save-status ${status.tone}`} role="status">
              {status.tone === "success" ? (
                <CheckCircle2 size={16} />
              ) : status.tone === "warning" ? (
                <CircleDashed size={16} />
              ) : (
                <X size={16} />
              )}
              {status.message}
            </div>
          ) : null}
          {tab === "General" ? (
            <section className="settings-section">
              <div className="settings-section-heading">
                <div>
                  <h2>Workspace profile</h2>
                  <p>
                    Project identity and behavior-safe defaults for every new
                    run.
                  </p>
                </div>
              </div>
              {draft ? (
                <form
                  className="settings-form panel"
                  onSubmit={(event) => {
                    event.preventDefault();
                    setSaving(true);
                    setStatus(null);
                    void updateSettings(draft)
                      .then(async (saved) => {
                        setDraft(saved);
                        await onRefresh();
                        setStatus({
                          message: "Project settings saved.",
                          tone: "success",
                        });
                      })
                      .catch((error: unknown) =>
                        setStatus({
                          message:
                            error instanceof Error
                              ? error.message
                              : "Settings could not be saved.",
                          tone: "error",
                        }),
                      )
                      .finally(() => setSaving(false));
                  }}
                >
                  <label className="field">
                    <span>Project name</span>
                    <input
                      value={draft.name}
                      onChange={(event) =>
                        setDraft({ ...draft, name: event.target.value })
                      }
                    />
                  </label>
                  <label className="field">
                    <span>Description</span>
                    <textarea
                      rows={4}
                      value={draft.description}
                      onChange={(event) =>
                        setDraft({ ...draft, description: event.target.value })
                      }
                    />
                  </label>
                  <div className="field-row">
                    <label className="field">
                      <span>Default classification</span>
                      <select
                        value={draft.default_classification}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            default_classification: event.target.value,
                          })
                        }
                      >
                        <option>public</option>
                        <option>internal</option>
                        <option>confidential</option>
                        <option>restricted</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Retention (days)</span>
                      <input
                        type="number"
                        min={30}
                        max={3650}
                        value={draft.retention_days}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            retention_days: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                  </div>
                  <div className="locked-setting">
                    <Globe2 size={18} />
                    <div>
                      <strong>Online research is always opt-in per run</strong>
                      <span>
                        A global default cannot enable public web tools.
                      </span>
                    </div>
                    <span>Off</span>
                  </div>
                  <div className="settings-actions">
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={saving}
                    >
                      {saving ? "Saving…" : "Save project settings"}
                    </button>
                  </div>
                </form>
              ) : (
                <div className="loading-block">Loading project settings…</div>
              )}
            </section>
          ) : null}

          {tab === "Agents & Models" ? (
            <section className="settings-section">
              <div className="settings-section-heading">
                <div>
                  <h2>Hosted Agent topology</h2>
                  <p>
                    Direct-code Microsoft Agent Framework deployments with
                    distinct contracts and tool boundaries.
                  </p>
                </div>
                <span className="subtle-chip">
                  {data?.agents.length ?? 9} active
                </span>
              </div>
              <div className="agent-setting-grid">
                {(data?.agents ?? []).map((agent) => (
                  <article className="panel agent-setting-card" key={agent.id}>
                    <div>
                      <span className="agent-status-dot" />
                      <span>
                        <strong>{agent.name}</strong>
                        <small>{agent.deployment}</small>
                      </span>
                      <em>{agent.status}</em>
                    </div>
                    <dl>
                      <div>
                        <dt>Model tier</dt>
                        <dd>{agent.model_tier}</dd>
                      </div>
                      <div>
                        <dt>Web boundary</dt>
                        <dd>{agent.web_access}</dd>
                      </div>
                    </dl>
                    <div className="agent-workflow">
                      {agent.workflow_steps.map((step) => (
                        <span key={step}>{step}</span>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          {tab === "Connectors" ? (
            <section className="settings-section connector-settings">
              <div className="settings-section-heading">
                <div>
                  <h2>Research data connectors</h2>
                  <p>
                    Assign allowlisted public metadata sources to specific
                    specialists, inspect terms, and run bounded health tests.
                  </p>
                </div>
                <span className="subtle-chip">
                  {data?.summary.connector_ready ?? 0}/
                  {data?.summary.connector_total ?? 12} ready
                </span>
              </div>
              <div className="connector-toolbar">
                <label className="search-field">
                  <Search size={16} />
                  <span className="sr-only">Search connectors</span>
                  <input
                    value={connectorQuery}
                    onChange={(event) => setConnectorQuery(event.target.value)}
                    placeholder="Search connectors"
                  />
                </label>
                <div className="filter-pills">
                  {connectorCategories.map((category) => (
                    <button
                      data-active={connectorCategory === category}
                      key={category}
                      onClick={() => setConnectorCategory(category)}
                    >
                      {category}
                    </button>
                  ))}
                </div>
              </div>
              <div
                className="connector-management-widget panel"
                aria-labelledby="connector-manager-title"
              >
                <div className="connector-catalog">
                  <div className="connector-catalog-heading">
                    <div>
                      <strong>Connector catalog</strong>
                      <span>Select a source to inspect and manage.</span>
                    </div>
                    <span>{visibleConnectors.length}</span>
                  </div>
                  <div className="connector-grid">
                    {visibleConnectors.map((connector) => {
                      const connectorStatus = connectorStatusInfo(connector);
                      return (
                        <button
                          type="button"
                          className="connector-card"
                          data-selected={managedConnector?.id === connector.id}
                          key={connector.id}
                          onClick={() => setManagedConnectorId(connector.id)}
                        >
                          <span className="connector-logo">
                            {connector.category === "Funding" ? (
                              <FileText size={18} />
                            ) : connector.category === "Identity" ? (
                              <Users size={18} />
                            ) : connector.category === "Datasets" ? (
                              <BarChart3 size={18} />
                            ) : (
                              <Globe2 size={18} />
                            )}
                          </span>
                          <span>
                            <strong>{connector.name}</strong>
                            <small>{connector.category}</small>
                          </span>
                          <span
                            className="connector-state"
                            data-tone={connectorStatus.tone}
                          >
                            <span />
                            {connectorStatus.label}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  {visibleConnectors.length === 0 ? (
                    <div className="empty-connector-search">
                      No connectors match this filter.
                    </div>
                  ) : null}
                </div>

                {managedConnector && connectorDraft && managedConnectorStatus ? (
                  <form
                    className="connector-manager"
                    onSubmit={(event) => {
                      event.preventDefault();
                      mutateConnector(managedConnector, connectorDraft);
                    }}
                  >
                    <div className="connector-manager-heading">
                      <div>
                        <span className="eyebrow">Configuration widget</span>
                        <h3 id="connector-manager-title">Connector manager</h3>
                      </div>
                      <label className="field connector-selector">
                        <span>Connector</span>
                        <select
                          aria-label="Connector to manage"
                          value={managedConnector.id}
                          onChange={(event) =>
                            setManagedConnectorId(event.target.value)
                          }
                        >
                          {connectors.map((connector) => (
                            <option value={connector.id} key={connector.id}>
                              {connector.name}
                            </option>
                          ))}
                        </select>
                      </label>
                    </div>

                    <div className="managed-connector-title">
                      <span className="connector-logo">
                        {managedConnector.category === "Funding" ? (
                          <FileText size={20} />
                        ) : managedConnector.category === "Identity" ? (
                          <Users size={20} />
                        ) : managedConnector.category === "Datasets" ? (
                          <BarChart3 size={20} />
                        ) : (
                          <Globe2 size={20} />
                        )}
                      </span>
                      <div>
                        <strong>{managedConnector.name}</strong>
                        <span>
                          {managedConnector.category} · {managedConnector.auth_kind}
                        </span>
                      </div>
                      <span
                        className="connector-health-badge"
                        data-tone={managedConnectorStatus.tone}
                      >
                        {managedConnectorStatus.label}
                      </span>
                    </div>

                    <div
                      className="connector-diagnostic"
                      data-tone={managedConnectorStatus.tone}
                    >
                      <div>
                        <strong>{managedConnectorStatus.label}</strong>
                        <p>{managedConnectorStatus.detail}</p>
                      </div>
                      <dl>
                        <div>
                          <dt>Credential</dt>
                          <dd>{managedConnector.secret_status}</dd>
                        </div>
                        <div>
                          <dt>Last tested</dt>
                          <dd>{formatTime(managedConnector.last_tested_at)}</dd>
                        </div>
                      </dl>
                    </div>

                    <label className="connector-enable-row">
                      <span>
                        <strong>Enable connector</strong>
                        <small>
                          {["pubmed", "grants_gov"].includes(managedConnector.id)
                            ? "Required baseline connectors cannot be disabled."
                            : "Disabled connectors are excluded from research runs."}
                        </small>
                      </span>
                      <input
                        type="checkbox"
                        aria-label={`Enable ${managedConnector.name}`}
                        checked={connectorDraft.enabled}
                        disabled={
                          busyConnector?.id === managedConnector.id ||
                          ["pubmed", "grants_gov"].includes(managedConnector.id)
                        }
                        onChange={(event) =>
                          setConnectorDrafts((current) => ({
                            ...current,
                            [managedConnector.id]: {
                              ...connectorDraft,
                              enabled: event.target.checked,
                            },
                          }))
                        }
                      />
                    </label>

                    <fieldset className="agent-assignments connector-manager-agents">
                      <legend>Assigned specialists</legend>
                      <p>
                        Only selected specialists can use this connector during
                        an opted-in public metadata run.
                      </p>
                      <div>
                        {CONNECTOR_SPECIALISTS.map((agent) => (
                          <label key={agent}>
                            <input
                              type="checkbox"
                              aria-label={`Assign ${agent} to ${managedConnector.name}`}
                              checked={connectorDraft.assigned_agents.includes(agent)}
                              disabled={busyConnector?.id === managedConnector.id}
                              onChange={(event) => {
                                const assigned = event.target.checked
                                  ? [...connectorDraft.assigned_agents, agent]
                                  : connectorDraft.assigned_agents.filter(
                                      (item) => item !== agent,
                                    );
                                setConnectorDrafts((current) => ({
                                  ...current,
                                  [managedConnector.id]: {
                                    ...connectorDraft,
                                    assigned_agents: assigned,
                                  },
                                }));
                              }}
                            />
                            <span>{agent}</span>
                          </label>
                        ))}
                      </div>
                    </fieldset>

                    <div className="connector-manager-details">
                      <div>
                        <strong>Capabilities</strong>
                        <span>{managedConnector.capabilities.join(" · ")}</span>
                      </div>
                      <div>
                        <strong>Data boundary</strong>
                        <span>{managedConnector.data_boundary}</span>
                      </div>
                    </div>

                    <div className="connector-manager-actions">
                      <PolicyGatedExternalLink url={managedConnector.terms_url}>
                        Provider terms <ArrowUpRight size={13} />
                      </PolicyGatedExternalLink>
                      <div>
                        <button
                          type="button"
                          disabled={busyConnector?.id === managedConnector.id}
                          onClick={() => runConnectorTest(managedConnector)}
                        >
                          {busyConnector?.id === managedConnector.id &&
                          busyConnector.action === "testing"
                            ? "Testing…"
                            : "Test connection"}
                        </button>
                        <button
                          className="primary-button"
                          type="submit"
                          disabled={busyConnector?.id === managedConnector.id}
                        >
                          {busyConnector?.id === managedConnector.id &&
                          busyConnector.action === "saving"
                            ? "Saving…"
                            : "Save configuration"}
                        </button>
                      </div>
                    </div>
                  </form>
                ) : (
                  <div className="empty-connector-manager">
                    Select a connector to configure it.
                  </div>
                )}
              </div>
              <article className="web-search-policy panel">
                <span className="connector-logo">
                  <Search size={18} />
                </span>
                <div>
                  <strong>Foundry Web Search</strong>
                  <p>
                    Separate from the 12 metadata connectors. Available only to
                    literature, grant, and matching runs when the user opts in
                    and the context is public.
                  </p>
                </div>
                <span className="subtle-chip">Per-run only</span>
              </article>

              <div className="settings-section-heading">
                <div>
                  <h2>Gateway & tool versions</h2>
                  <p>
                    Version promotion and rollback are unavailable until an
                    administrator registers a real gateway or tool registry.
                    Nothing here promotes a version by default.
                  </p>
                </div>
              </div>
              <div className="readiness-card-grid">
                {gatewayVersionCards.map((target) => (
                  <article className="panel readiness-status-card" key={target.id}>
                    <div>
                      <strong>{target.label}</strong>
                      <span className="subtle-chip">
                        {target.connector
                          ? statusLabel(target.connector.test_status)
                          : "Not configured"}
                      </span>
                    </div>
                    <p>
                      {target.connector
                        ? `${target.connector.name} is registered, but version promotion still requires administrator approval.`
                        : "No gateway or tool registry connection is configured for this capability yet."}
                    </p>
                    <div className="connector-actions">
                      <button
                        type="button"
                        disabled
                        title="Version promotion requires a verified gateway release and administrator approval; not available in this workspace."
                      >
                        Promote to default
                      </button>
                      <button
                        type="button"
                        disabled
                        title="Rollback requires an active promoted version."
                      >
                        Roll back
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          {tab === "Retrieval & Evidence" ? (
            <SettingsPolicySection
              title="Retrieval & evidence"
              description="Deterministic thresholds applied before model analysis."
              rows={[
                ["Retrieval mode", "Hybrid lexical + vector"],
                ["Semantic reranking", "Enabled for authorized corpora"],
                ["Claim citation coverage", "100% required"],
                ["Unresolved citation behavior", "Block verified label"],
                ["Institutional version policy", "Effective version first"],
                ["Prompt-injection treatment", "Retrieved text is untrusted data"],
              ]}
            />
          ) : null}

          {tab === "Governance" ? (
            <SettingsPolicySection
              title="Governance & approvals"
              description="Policy is enforced by server functions and durable checkpoints."
              rows={[
                ["Tenant source", "Authenticated platform identity"],
                ["Confidential web routing", "Blocked"],
                ["External export", "Named human approval"],
                ["Large-scale compute", "Estimate + approval + idempotency"],
                ["Default classification", draft?.default_classification ?? "internal"],
                ["Retention", `${draft?.retention_days ?? 2555} days`],
              ]}
            />
          ) : null}

          {tab === "Evaluation" ? (
            <section className="settings-section">
              <div className="settings-section-heading">
                <div>
                  <h2>Release evaluation</h2>
                  <p>
                    Independent quality dimensions remain visible instead of
                    collapsing into a single model score.
                  </p>
                </div>
              </div>
              <div className="evaluation-grid">
                {[
                  ["Citation resolution", 100, "Blocking"],
                  ["Claim entailment", 96, "Blocking"],
                  ["Retrieval completeness", 91, "Warning"],
                  ["Abstention behavior", 100, "Blocking"],
                  ["Policy compliance", 100, "Blocking"],
                  ["Accessibility", 100, "Blocking"],
                ].map(([label, score, gate]) => {
                  const state = evaluationState(Number(score), String(gate));
                  return (
                    <article
                      className="panel evaluation-card"
                      data-evaluation-state={state}
                      key={String(label)}
                    >
                      <div>
                        <strong>{label}</strong>
                        <span>
                          {state === "ready"
                            ? "Ready"
                            : state === "blocked"
                              ? "Blocked"
                              : "Degraded"}{" "}
                          · {gate}
                        </span>
                      </div>
                      <div className="evaluation-score">
                        <i style={{ width: `${score}%` }} />
                      </div>
                      <em>{score}%</em>
                    </article>
                  );
                })}
              </div>
            </section>
          ) : null}

          {tab === "Readiness" ? (
            <section className="settings-section">
              <div className="settings-section-heading">
                <div>
                  <h2>Platform integration readiness</h2>
                  <p>
                    Truthful status only. Ready capabilities do not grant
                    user-level permissions, and blocked capabilities remain off
                    until an administrator verifies their prerequisites.
                  </p>
                </div>
              </div>
              <div className="readiness-card-grid">
                <article className="panel readiness-status-card">
                  <div>
                    <strong>APIM / Toolbox</strong>
                    <span
                      className="subtle-chip"
                      data-readiness-state="deployment-managed"
                    >
                      Deployment managed
                    </span>
                  </div>
                  <p>
                    The accelerator provisions APIM-backed MCP connectors and
                    immutable per-studio Foundry Toolboxes. Health and versions
                    remain visible on the Connectors tab.
                  </p>
                </article>
                <article className="panel readiness-status-card">
                  <div>
                    <strong>Work IQ</strong>
                    <span
                      className="subtle-chip"
                      data-readiness-state="needs-consent"
                    >
                      Needs tenant consent
                    </span>
                  </div>
                  <p>
                    Requires tenant-level Microsoft Graph consent. Not granted
                    in this environment, so Work IQ signals stay off across
                    every studio.
                  </p>
                </article>
                <article className="panel readiness-status-card">
                  <div>
                    <strong>GitHub Copilot connector authoring</strong>
                    <span
                      className="subtle-chip"
                      data-readiness-state="blocked"
                    >
                      Repository setup required
                    </span>
                  </div>
                  <p>
                    Approved connector-request issues can invoke the scoped
                    Copilot cloud agent to create a draft PR. It cannot merge,
                    deploy, or promote APIM and Toolbox versions.
                  </p>
                </article>
                <article className="panel readiness-status-card">
                  <div>
                    <strong>Foundry Code Interpreter</strong>
                    <span
                      className="subtle-chip"
                      data-readiness-state="ready"
                    >
                      Ready: dataset toolbox
                    </span>
                  </div>
                  <p>
                    Dataset analysis runs through the existing Foundry Hosted
                    Dataset Agent and its managed Code Interpreter tool. This
                    accelerator permits public/synthetic data only because the
                    hosted Toolbox container is project-scoped, not per-user.
                  </p>
                </article>
              </div>
            </section>
          ) : null}
        </main>
      </div>
    </div>
  );
}

function SettingsPolicySection({
  title,
  description,
  rows,
}: {
  title: string;
  description: string;
  rows: string[][];
}) {
  return (
    <section className="settings-section">
      <div className="settings-section-heading">
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </div>
      <article className="panel policy-settings-list">
        {rows.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
            <CheckCircle2 size={16} />
          </div>
        ))}
      </article>
    </section>
  );
}

export const CAPABILITY_CARDS: CapabilityCard[] = [
  {
    id: "literature",
    title: "Literature review synthesis",
    shortTitle: "Literature Studio",
    description:
      "Protocol-led search, screening, extraction, synthesis, and claim audit.",
    eyebrow: "Evidence review",
    icon: BookOpen,
    accent: "sage-card",
    artifact: "Review + extraction matrix",
    stages: ["Protocol", "Screen", "Extract", "Audit"],
  },
  {
    id: "grant",
    title: "Grant application studio",
    shortTitle: "Grant Studio",
    description:
      "Requirement matrix, project facts, section drafting, compliance, and red team.",
    eyebrow: "Application lifecycle",
    icon: FileText,
    accent: "amber-card",
    artifact: "Review-ready package",
    stages: ["Notice", "Facts", "Draft", "Review"],
  },
  {
    id: "matching",
    title: "PI and resource matching",
    shortTitle: "Matching Explorer",
    description:
      "Hard filters, entity resolution, weighted evidence, and confirmed shortlist.",
    eyebrow: "Discovery",
    icon: Users,
    accent: "indigo-card",
    artifact: "Verified shortlist",
    stages: ["Criteria", "Resolve", "Score", "Confirm"],
  },
  {
    id: "dataset",
    title: "Dataset and notebook summary",
    shortTitle: "Dataset Lab",
    description:
      "Schema and quality profile, analysis plan, deterministic metrics, approved compute.",
    eyebrow: "Data analysis",
    icon: FlaskConical,
    accent: "blue-card",
    artifact: "Profile + analysis plan",
    stages: ["Validate", "Profile", "Compute", "Interpret"],
  },
  {
    id: "institutional_qa",
    title: "Institution-grounded Q&A",
    shortTitle: "Institutional Q&A",
    description:
      "Identity-scoped retrieval, version resolution, conflict detection, answer or abstain.",
    eyebrow: "Policy guidance",
    icon: Landmark,
    accent: "rose-card",
    artifact: "Version-aware answer",
    stages: ["Authorize", "Version", "Conflict", "Answer"],
  },
  {
    id: "orchestration",
    title: "Research workflow orchestration",
    shortTitle: "Workflow Automation",
    description:
      "Templates, typed DAG steps, triggers, retries, approvals, and durable history.",
    eyebrow: "Automation",
    icon: Workflow,
    accent: "slate-card",
    artifact: "Versioned workflow",
    stages: ["Trigger", "Graph", "Dry run", "Activate"],
  },
];
