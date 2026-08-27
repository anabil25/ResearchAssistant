"use client";

import {
  ArrowUpRight,
  BarChart3,
  BookOpen,
  ClipboardCheck,
  Clock3,
  FileSearch2,
  FileText,
  FlaskConical,
  Globe2,
  Landmark,
  Library,
  Lock,
  Search,
  ShieldCheck,
  Sparkles,
  Upload,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  uploadLibraryItem,
  type WorkspaceData,
} from "@/lib/api";
import type {
  CapabilityId,
  LibraryItem,
} from "@/lib/types";

export type ReachableCapabilityId = Exclude<CapabilityId, "orchestration">;

export type WorkspaceViewId =
  | "overview"
  | "library"
  | "settings"
  | "agents"
  | ReachableCapabilityId;

export interface CapabilityCard {
  id: ReachableCapabilityId;
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
              <strong>
                {data
                  ? `${Math.round(data.settings.citation_coverage_threshold * 100)}%`
                  : "—"}
              </strong>
              claim coverage gate
            </span>
            <span>
              <strong>
                {data ? `${data.summary.connector_ready}/${data.summary.connector_total}` : "—"}
              </strong>
              research sources ready
            </span>
          </div>
        </div>
      </section>

      <section className="trust-principle">
        <span className="trust-mark">
          <ShieldCheck size={21} />
        </span>
        <div>
          <strong>Proof before prose</strong>
          <p>
            Claims become verified only after their source IDs resolve to
            admitted source records.
          </p>
        </div>
      </section>

      <section className="overview-metrics" aria-label="Workspace metrics">
        <article>
          <span className="metric-icon sage">
            <Library size={18} />
          </span>
          <div>
            <strong>{summary?.library_items ?? "—"}</strong>
            <span>Governed library items</span>
          </div>
          <small>Checksum, license & ACL</small>
        </article>
        <article>
          <span className="metric-icon amber">
            <Clock3 size={18} />
          </span>
          <div>
            <strong>{summary?.active_runs ?? "—"}</strong>
            <span>Active durable runs</span>
          </div>
          <small>Resumable & auditable</small>
        </article>
        <article>
          <span className="metric-icon rose">
            <Lock size={18} />
          </span>
          <div>
            <strong>{summary?.pending_approvals ?? "—"}</strong>
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
              {summary
                ? `${summary.connector_ready}/${summary.connector_total}`
                : "—"}
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
          </div>
          {runs.length ? (
            <div className="run-list">
              {runs.map((run) => (
                <div key={run.id}>
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
                </div>
              ))}
            </div>
          ) : data ? (
            <div className="loading-block">No work is currently in motion.</div>
          ) : (
            <div className="loading-block">Loading workspace activity...</div>
          )}
        </article>

        <article className="panel governance-board">
          <span className="eyebrow">Project policy</span>
          <h2>Current governed defaults</h2>
          {data ? (
            <>
              <div className="governance-row">
                <span>Default classification</span>
                <strong>{data.settings.default_classification}</strong>
              </div>
              <div className="governance-row">
                <span>Citation coverage</span>
                <strong>
                  {Math.round(
                    data.settings.citation_coverage_threshold * 100,
                  )}%
                </strong>
              </div>
              <div className="governance-row">
                <span>Human approval</span>
                <strong>
                  {data.settings.require_human_approval ? "Required" : "Not required"}
                </strong>
              </div>
              <div className="governance-row">
                <span>Retention</span>
                <strong>{data.settings.retention_days} days</strong>
              </div>
            </>
          ) : (
            <div className="loading-block">Loading project policy...</div>
          )}
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
    id: "screening",
    title: "Systematic review screening",
    shortTitle: "Screening Studio",
    description:
      "Criteria-led screening with one recorded decision per paper and honest unclears.",
    eyebrow: "Evidence review",
    icon: ClipboardCheck,
    accent: "sage-card",
    artifact: "Screening decisions",
    stages: ["Criteria", "Screen", "Adjudicate", "Report"],
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
];
