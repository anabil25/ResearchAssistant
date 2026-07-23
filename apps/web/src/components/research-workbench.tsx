"use client";

import {
  Bell,
  BookOpen,
  FileText,
  History,
  Home,
  Landmark,
  Library,
  Menu,
  PanelRight,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
  Workflow,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  StudioForCapability,
  type StudioRunOptions,
} from "@/components/studio-components";
import {
  CAPABILITY_CARDS,
  LibraryView,
  Overview,
  RunsView,
  SettingsView,
  type WorkspaceViewId,
} from "@/components/workspace-views";
import {
  getWorkspaceData,
  runStudio,
  type WorkspaceData,
} from "@/lib/api";
import type {
  CapabilityId,
  Citation,
  StudioResult,
} from "@/lib/types";

function isCapability(view: WorkspaceViewId): view is CapabilityId {
  return CAPABILITY_CARDS.some((capability) => capability.id === view);
}

function viewTitle(view: WorkspaceViewId): string {
  if (view === "overview") return "Research command center";
  if (view === "library") return "Evidence Library";
  if (view === "runs") return "Runs & Approvals";
  if (view === "settings") return "Project Settings";
  return (
    CAPABILITY_CARDS.find((capability) => capability.id === view)?.shortTitle ??
    "Research Assistant"
  );
}

function isWorkspaceView(candidate: string | null): candidate is WorkspaceViewId {
  return (
    candidate === "overview" ||
    candidate === "library" ||
    candidate === "runs" ||
    candidate === "settings" ||
    CAPABILITY_CARDS.some((capability) => capability.id === candidate)
  );
}

function viewFromSearch(search: string): WorkspaceViewId {
  const candidate = new URLSearchParams(search).get("view");
  return isWorkspaceView(candidate) ? candidate : "overview";
}

function ResultEvidence({
  result,
  data,
}: {
  result: StudioResult | null;
  data: WorkspaceData | null;
}) {
  const citations: Citation[] = result?.citations ?? [];
  return (
    <>
      <div className="evidence-header">
        <div>
          <span className="eyebrow">Trust inspector</span>
          <h2>Evidence & lineage</h2>
        </div>
        <span className="evidence-health">
          <span />
          {result ? "Run resolved" : "Ready"}
        </span>
      </div>

      {result ? (
        <>
          <section className="evidence-run-card">
            <div>
              <span className="evidence-run-icon">
                <ShieldCheck size={18} />
              </span>
              <span>
                <strong>{result.run.title}</strong>
                <small>{result.run.durable_instance_id}</small>
              </span>
            </div>
            <div className="evidence-progress">
              <span>
                <strong>{result.run.progress}%</strong>
                {result.run.current_stage}
              </span>
              <div>
                <i style={{ width: `${result.run.progress}%` }} />
              </div>
            </div>
          </section>
          <section className="evidence-section">
            <div className="evidence-section-heading">
              <span>Resolved sources</span>
              <em>{citations.length}</em>
            </div>
            {citations.length ? (
              <div className="evidence-source-list">
                {citations.slice(0, 5).map((citation, index) => (
                  <article key={citation.id}>
                    <span>{index + 1}</span>
                    <div>
                      <strong>{citation.title}</strong>
                      <small>
                        {citation.section}
                        {citation.page_start
                          ? ` · p. ${citation.page_start}`
                          : ""}
                      </small>
                      <p>{citation.quote}</p>
                      <code>{citation.source_id}</code>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className="evidence-empty">
                No stored citations were used by this artifact.
              </div>
            )}
          </section>
          {result.insight ? (
            <section className="evidence-section agent-boundary-card">
              <div className="evidence-section-heading">
                <span>Hosted Agent boundary</span>
                <em>{result.insight.evidence_state.replaceAll("_", " ")}</em>
              </div>
              <p>
                Model text is supplemental analysis. It cannot grant
                permissions, calculate scores, approve actions, or promote
                unresolved claims to verified evidence.
              </p>
              <dl>
                <div>
                  <dt>Resolved IDs</dt>
                  <dd>{(result.insight.referenced_source_ids ?? []).length}</dd>
                </div>
                <div>
                  <dt>Unresolved IDs</dt>
                  <dd>{(result.insight.unresolved_source_ids ?? []).length}</dd>
                </div>
              </dl>
            </section>
          ) : null}
        </>
      ) : (
        <>
          <section className="trust-principle">
            <span className="trust-mark">
              <ShieldCheck size={21} />
            </span>
            <div>
              <strong>Proof before prose</strong>
              <p>
                Claims become verified only after their source IDs resolve to
                authorized stored passages.
              </p>
            </div>
          </section>
          <section className="evidence-section">
            <div className="evidence-section-heading">
              <span>Active controls</span>
              <em>6</em>
            </div>
            <div className="control-list">
              {[
                ["Identity-bound tenant", "demo"],
                ["Public web default", "Off"],
                ["Citation coverage", "100%"],
                ["Approval policy", "Required"],
                ["Connector boundary", "Public metadata"],
                ["Agent runtime", "Foundry hosted"],
                [
                  "Operational state",
                  data?.summary.persistence ?? "Loading",
                ],
              ].map(([label, value]) => (
                <div key={label}>
                  <span>
                    <i />
                    {label}
                  </span>
                  <strong>{value}</strong>
                </div>
              ))}
            </div>
          </section>
          <section className="evidence-section">
            <div className="evidence-section-heading">
              <span>Workspace readiness</span>
              <em>
                {data?.summary.connector_ready ?? 12}/
                {data?.summary.connector_total ?? 12}
              </em>
            </div>
            <div className="readiness-bars">
              <div>
                <span>Connectors</span>
                <i>
                  <b style={{ width: "100%" }} />
                </i>
              </div>
              <div>
                <span>Citation policy</span>
                <i>
                  <b style={{ width: "100%" }} />
                </i>
              </div>
              <div>
                <span>Evaluations</span>
                <i>
                  <b style={{ width: "96%" }} />
                </i>
              </div>
            </div>
          </section>
        </>
      )}
    </>
  );
}

export function ResearchWorkbench() {
  const [view, setView] = useState<WorkspaceViewId>("overview");
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [studioResult, setStudioResult] = useState<StudioResult | null>(null);
  const [studioRunning, setStudioRunning] = useState(false);
  const [studioError, setStudioError] = useState<string | null>(null);
  const [focusRunId, setFocusRunId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await getWorkspaceData();
      setData(next);
      setLoadError(null);
    } catch (error) {
      setLoadError(
        error instanceof Error
          ? error.message
          : "Workspace data could not be loaded.",
      );
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void getWorkspaceData()
      .then((next) => {
        if (!cancelled) {
          setData(next);
          setLoadError(null);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(
            error instanceof Error
              ? error.message
              : "Workspace data could not be loaded.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const restoreView = () => {
      setView(viewFromSearch(window.location.search));
      setNavOpen(false);
      setEvidenceOpen(false);
      setSearchOpen(false);
      setStudioResult(null);
      setStudioError(null);
    };
    restoreView();
    window.addEventListener("popstate", restoreView);
    return () => window.removeEventListener("popstate", restoreView);
  }, []);

  useEffect(() => {
    const hasTransitionalState =
      data?.library.some((item) => item.status === "processing") ||
      data?.runs.some((run) =>
        ["planned", "running"].includes(run.status),
      );
    if (!hasTransitionalState) return;
    const interval = window.setInterval(() => {
      void refresh();
    }, 3_000);
    return () => window.clearInterval(interval);
  }, [data, refresh]);

  useEffect(() => {
    const onFocus = () => {
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setNavOpen(false);
        setEvidenceOpen(false);
        setSearchOpen(false);
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const navigate = (next: WorkspaceViewId) => {
    const url = new URL(window.location.href);
    if (next === "overview") {
      url.searchParams.delete("view");
    } else {
      url.searchParams.set("view", next);
    }
    if (url.href !== window.location.href) {
      window.history.pushState(null, "", url);
    }
    setView(next);
    setNavOpen(false);
    setSearchOpen(false);
    setStudioResult(null);
    setStudioError(null);
    if (next !== "runs") {
      setFocusRunId(null);
    }
    if (next === "library" || next === "runs" || next === "settings") {
      void refresh();
    }
  };

  const navigateToRun = (runId: string) => {
    navigate("runs");
    setFocusRunId(runId);
  };

  const executeStudio = async (
    capability: CapabilityId,
    objective: string,
    options: StudioRunOptions = {},
  ) => {
    setStudioRunning(true);
    setStudioError(null);
    try {
      const result = await runStudio(capability, objective, options);
      setStudioResult(result);
      void refresh();
    } catch (error) {
      setStudioError(
        error instanceof Error ? error.message : "The studio run failed.",
      );
    } finally {
      setStudioRunning(false);
    }
  };

  const workflow = isCapability(view)
    ? data?.workflows.find((item) => item.capability === view)
    : undefined;
  const pendingApprovals =
    data?.approvals.filter((approval) => approval.state === "pending").length ??
    0;
  const searchItems = useMemo(() => {
    const items: { id: WorkspaceViewId; title: string; subtitle: string }[] = [
      {
        id: "overview",
        title: "Research command center",
        subtitle: "Workspace overview",
      },
      {
        id: "library",
        title: "Evidence Library",
        subtitle: "Sources, versions, licenses, and ACLs",
      },
      {
        id: "runs",
        title: "Runs & Approvals",
        subtitle: "Durable execution and review gates",
      },
      {
        id: "settings",
        title: "Project Settings",
        subtitle: "Agents, connectors, evidence, and governance",
      },
      ...CAPABILITY_CARDS.map((capability) => ({
        id: capability.id,
        title: capability.title,
        subtitle: capability.artifact,
      })),
    ];
    return items.filter((item) =>
      `${item.title} ${item.subtitle}`
        .toLowerCase()
        .includes(searchQuery.toLowerCase()),
    );
  }, [searchQuery]);

  return (
    <div className="workbench-shell" data-workspace-ready={Boolean(data)}>
      {navOpen ? (
        <button
          className="mobile-scrim"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
      ) : null}
      <aside
        id="project-navigation"
        className="project-rail"
        data-open={navOpen}
        aria-label="Project navigation"
      >
        <div className="brand-row">
          <span className="brand-mark">
            <Sparkles size={18} />
          </span>
          <span className="brand-copy">
            <strong>Research Assistant</strong>
            <span>Microsoft Foundry workspace</span>
          </span>
          <button
            className="rail-close"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        <div className="project-switcher">
          <span>
            <small>Active project</small>
            <strong>
              {data?.summary.project.name ??
                "AI for equitable clinical research"}
            </strong>
          </span>
        </div>

        <span className="rail-section-label">Workspace</span>
        <nav className="rail-nav" aria-label="Workspace navigation">
          <button
            className="rail-link"
            data-active={view === "overview"}
            aria-current={view === "overview" ? "page" : undefined}
            onClick={() => navigate("overview")}
          >
            <Home size={17} />
            <span>Overview</span>
          </button>
          <button
            className="rail-link"
            data-active={view === "library"}
            aria-current={view === "library" ? "page" : undefined}
            onClick={() => navigate("library")}
          >
            <Library size={17} />
            <span>Library</span>
            <em>{data?.summary.library_items ?? 9}</em>
          </button>
          <button
            className="rail-link"
            data-active={view === "runs"}
            aria-current={view === "runs" ? "page" : undefined}
            onClick={() => navigate("runs")}
          >
            <History size={17} />
            <span>Runs & approvals</span>
            {pendingApprovals ? <em>{pendingApprovals}</em> : null}
          </button>
        </nav>

        <span className="rail-section-label">Studios</span>
        <nav className="rail-nav studio-nav" aria-label="Research studios">
          {CAPABILITY_CARDS.map((capability) => {
            const Icon = capability.icon;
            return (
              <button
                className="rail-link"
                data-active={view === capability.id}
                aria-current={view === capability.id ? "page" : undefined}
                key={capability.id}
                onClick={() => navigate(capability.id)}
              >
                <Icon size={17} />
                <span>{capability.shortTitle}</span>
              </button>
            );
          })}
        </nav>

        <div className="rail-spacer" />
        <button
          className="rail-link settings-link"
          data-active={view === "settings"}
          aria-current={view === "settings" ? "page" : undefined}
          onClick={() => navigate("settings")}
        >
          <Settings size={17} />
          <span>Project Settings</span>
        </button>
        <div className="rail-footer">
          <span className="avatar">MC</span>
          <span>
            <strong>Dr. Maya Chen</strong>
            <small>Researcher · demo tenant</small>
          </span>
          <ShieldCheck size={15} />
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="mobile-menu-button"
              aria-label="Open navigation"
              aria-controls="project-navigation"
              aria-expanded={navOpen}
              onClick={() => setNavOpen(true)}
            >
              <Menu size={20} />
            </button>
            <div className="breadcrumbs">
              <small>AI for equitable clinical research</small>
              <strong>{viewTitle(view)}</strong>
            </div>
          </div>
          <div className="topbar-actions">
            <button
              className="search-button"
              aria-label="Search workspace"
              onClick={() => setSearchOpen(true)}
            >
              <Search size={16} />
              <span>Search workspace</span>
              <kbd>Ctrl K</kbd>
            </button>
            <button
              className="icon-button"
              aria-label={`${pendingApprovals} pending approvals`}
              onClick={() => navigate("runs")}
            >
              <Bell size={18} />
              {pendingApprovals ? <span>{pendingApprovals}</span> : null}
            </button>
            <button
              className="icon-button"
              aria-label="Open project settings"
              onClick={() => navigate("settings")}
            >
              <Settings size={18} />
            </button>
            <button
              className="icon-button evidence-toggle"
              aria-label="Open evidence inspector"
              onClick={() => setEvidenceOpen(true)}
            >
              <PanelRight size={18} />
            </button>
          </div>
        </header>

        {loadError ? (
          <div className="connection-banner" role="status">
            <ShieldCheck size={16} />
            Live workspace data is unavailable: {loadError}. The product shell
            remains usable.
          </div>
        ) : null}
        <div className="view-announcement sr-only" aria-live="polite">
          Opened {viewTitle(view)}
        </div>
        <main className="workspace-main">
          {view === "overview" ? (
            <Overview
              data={data}
              capabilities={CAPABILITY_CARDS}
              onNavigate={navigate}
            />
          ) : view === "library" ? (
            <LibraryView data={data} onRefresh={refresh} />
          ) : view === "runs" ? (
            <RunsView
              key={focusRunId ?? "runs-default"}
              data={data}
              onRefresh={refresh}
              focusRunId={focusRunId}
            />
          ) : view === "settings" ? (
            <SettingsView
              key={data?.settings ? "settings-loaded" : "settings-loading"}
              data={data}
              onRefresh={refresh}
            />
          ) : (
            <StudioForCapability
              capability={view}
              result={studioResult}
              running={studioRunning}
              error={studioError}
              workflow={workflow}
              onRun={executeStudio}
              data={data}
              onRefresh={refresh}
              onNavigateToRun={navigateToRun}
            />
          )}
        </main>
      </div>

      <aside
        className="evidence-panel"
        data-open={evidenceOpen}
        aria-label="Evidence and lineage inspector"
      >
        <button
          className="evidence-close"
          aria-label="Close evidence inspector"
          onClick={() => setEvidenceOpen(false)}
        >
          <X size={18} />
        </button>
        <ResultEvidence result={studioResult} data={data} />
      </aside>

      {evidenceOpen ? (
        <button
          className="evidence-scrim"
          aria-label="Close evidence inspector"
          onClick={() => setEvidenceOpen(false)}
        />
      ) : null}

      {searchOpen ? (
        <div className="command-backdrop" role="presentation">
          <div
            className="command-palette"
            role="dialog"
            aria-modal="true"
            aria-label="Search workspace"
          >
            <label>
              <Search size={19} />
              <span className="sr-only">Search workspace</span>
              <input
                autoFocus
                placeholder="Search studios, Library, runs, or settings"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
              />
              <button
                aria-label="Close search"
                onClick={() => setSearchOpen(false)}
              >
                <X size={17} />
              </button>
            </label>
            <div className="command-results">
              {searchItems.map((item) => (
                <button key={item.id} onClick={() => navigate(item.id)}>
                  <span>
                    {item.id === "library" ? (
                      <Library size={17} />
                    ) : item.id === "runs" ? (
                      <History size={17} />
                    ) : item.id === "settings" ? (
                      <Settings size={17} />
                    ) : item.id === "literature" ? (
                      <BookOpen size={17} />
                    ) : item.id === "grant" ? (
                      <FileText size={17} />
                    ) : item.id === "matching" ? (
                      <Users size={17} />
                    ) : item.id === "institutional_qa" ? (
                      <Landmark size={17} />
                    ) : item.id === "orchestration" ? (
                      <Workflow size={17} />
                    ) : (
                      <Home size={17} />
                    )}
                  </span>
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.subtitle}</small>
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
