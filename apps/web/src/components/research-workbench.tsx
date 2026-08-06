"use client";

import {
  Archive,
  Bot,
  Home,
  Library,
  Menu,
  Pencil,
  Plus,
  Settings,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { StudioForCapability } from "@/components/studio-components";
import { FoundryAgentCatalog } from "@/components/foundry-agent-studio";
import { ProjectSettingsView } from "@/components/project-settings";
import {
  CAPABILITY_CARDS,
  LibraryView,
  Overview,
  type WorkspaceViewId,
} from "@/components/workspace-views";
import {
  activateProject,
  createProject,
  getWorkspaceData,
  listProjects,
  updateProject,
  type WorkspaceData,
} from "@/lib/api";
import { useBlockingModalOpen } from "@/lib/blocking-modal";
import type { ProjectSummary } from "@/lib/types";

function viewTitle(view: WorkspaceViewId): string {
  if (view === "overview") return "Research command center";
  if (view === "library") return "Evidence Library";
  if (view === "settings") return "Project Settings";
  if (view === "agents") return "Agents";
  return (
    CAPABILITY_CARDS.find((capability) => capability.id === view)?.shortTitle ??
    "Research Assistant"
  );
}

function isWorkspaceView(candidate: string | null): candidate is WorkspaceViewId {
  return (
    candidate === "overview" ||
    candidate === "library" ||
    candidate === "settings" ||
    candidate === "agents" ||
    CAPABILITY_CARDS.some((capability) => capability.id === candidate)
  );
}

function viewFromSearch(search: string): WorkspaceViewId {
  const candidate = new URLSearchParams(search).get("view");
  return isWorkspaceView(candidate) ? candidate : "overview";
}

function projectFromSearch(search: string): string | null {
  return new URLSearchParams(search).get("project");
}

export function ResearchWorkbench() {
  const [view, setView] = useState<WorkspaceViewId>("overview");
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const blockingModalOpen = useBlockingModalOpen();
  const [projectPanelMode, setProjectPanelMode] = useState<"create" | "edit" | null>(null);
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  const [projectSubmitting, setProjectSubmitting] = useState(false);
  const [archiveConfirmationOpen, setArchiveConfirmationOpen] = useState(false);
  const mobileMenuTriggerRef = useRef<HTMLButtonElement | null>(null);
  const railCloseRef = useRef<HTMLButtonElement | null>(null);
  const wasNavOpenRef = useRef(false);
  // Several triggers can each start a workspace fetch independently and
  // concurrently: the initial mount load, the transitional-state poll, the
  // window "focus" listener, view navigation, and post-decision refreshes.
  // Network responses are not guaranteed to resolve in the order they were
  // issued, so an older in-flight request finishing after a newer one (e.g.
  // a stale poll response landing just after a fresh post-approval refresh)
  // would silently overwrite up-to-date data -- including the pending
  // approvals count the notification bell reads -- with stale data. A
  // monotonic request sequence number lets every refresh discard its own
  // result if a newer refresh has since been issued, so only the
  // most-recently-requested response is ever applied, deterministically.
  const requestSequenceRef = useRef(0);

  const loadWorkspace = useCallback(async (projectId: string | null) => {
    if (!projectId) {
      setData(null);
      setLoadError(null);
      return;
    }
    const requestId = (requestSequenceRef.current += 1);
    try {
      const next = await getWorkspaceData(projectId);
      if (requestSequenceRef.current !== requestId) return;
      setData(next);
      setLoadError(null);
    } catch (error) {
      if (requestSequenceRef.current !== requestId) return;
      setLoadError(
        error instanceof Error
          ? error.message
          : "Workspace data could not be loaded.",
      );
    }
  }, []);

  const refresh = useCallback(async () => {
    await loadWorkspace(activeProjectId);
  }, [activeProjectId, loadWorkspace]);

  const updateProjectUrl = (projectId: string | null) => {
    const url = new URL(window.location.href);
    if (projectId) {
      url.searchParams.set("project", projectId);
    } else {
      url.searchParams.delete("project");
    }
    if (url.href !== window.location.href) {
      window.history.pushState(null, "", url);
    }
  };

  const loadProjects = useCallback(async () => {
    return listProjects();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void loadProjects()
      .then(async (availableProjects) => {
        if (cancelled) return;
        const requestedProjectId = projectFromSearch(window.location.search);
        const storedProject = availableProjects.find((project) => project.is_active);
        const selectedProjectId = requestedProjectId ?? storedProject?.id ?? null;
        if (requestedProjectId && requestedProjectId !== storedProject?.id) {
          await activateProject(requestedProjectId);
        }
        if (cancelled) return;
        setProjects(
          availableProjects.map((project) => ({
            ...project,
            is_active: project.id === selectedProjectId,
          })),
        );
        setActiveProjectId(selectedProjectId);
        if (selectedProjectId) {
          await loadWorkspace(selectedProjectId);
        } else {
          setProjectPanelMode("create");
        }
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setLoadError(
          error instanceof Error
            ? error.message
            : "Projects could not be loaded.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [loadProjects, loadWorkspace]);

  useEffect(() => {
    const restoreView = () => {
      setView(viewFromSearch(window.location.search));
      setNavOpen(false);
    };
    restoreView();
    window.addEventListener("popstate", restoreView);
    return () => window.removeEventListener("popstate", restoreView);
  }, []);

  useEffect(() => {
    const restoreProject = () => {
      const requestedProjectId = projectFromSearch(window.location.search);
      if (
        !requestedProjectId ||
        requestedProjectId === activeProjectId ||
        projects.length === 0
      ) {
        return;
      }
      void activateProject(requestedProjectId)
        .then(() => {
          setActiveProjectId(requestedProjectId);
          setProjects((current) =>
            current.map((project) => ({
              ...project,
              is_active: project.id === requestedProjectId,
            })),
          );
          void loadWorkspace(requestedProjectId);
        })
        .catch((error: unknown) => {
          setProjectError(
            error instanceof Error
              ? error.message
              : "The requested project is unavailable.",
          );
        });
    };
    window.addEventListener("popstate", restoreProject);
    return () => window.removeEventListener("popstate", restoreProject);
  }, [activeProjectId, loadWorkspace, projects.length]);

  useEffect(() => {
    const hasTransitionalState =
      data?.library.some((item) => item.status === "processing") ||
      data?.runs.some((run) =>
        ["planned", "running"].includes(run.status),
      );
    if (!hasTransitionalState) return;
    // A fixed `setInterval` fires again every 3s regardless of whether the
    // previous `refresh()` call is still in flight. If `getWorkspaceData()`
    // consistently takes longer than 3s to resolve (a slow backend, a busy
    // gateway, etc.), each tick bumps `requestSequenceRef` again *before*
    // the prior response lands, so `refresh`'s own stale-response guard
    // (see above) would discard every single response forever -- the
    // transitional UI would poll indefinitely without ever applying a
    // result, even once the underlying state genuinely finished
    // processing. Scheduling the next poll only after the current one
    // settles serializes polling (never more than one in-flight poll
    // request at a time), so a slow response always gets to apply -- and
    // the next request is only issued once there is nothing left for it to
    // race against.
    let cancelled = false;
    let timeoutId: number | undefined;
    const scheduleNext = () => {
      timeoutId = window.setTimeout(() => {
        void refresh().finally(() => {
          if (!cancelled) scheduleNext();
        });
      }, 3_000);
    };
    scheduleNext();
    return () => {
      cancelled = true;
      // `timeoutId` is always assigned synchronously above before this
      // cleanup can ever run, but `clearTimeout` safely no-ops on
      // `undefined` regardless, so no extra guard is needed here.
      window.clearTimeout(timeoutId);
    };
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
      // While an application-modal dialog is open elsewhere (it is portalled
      // outside `.workbench-shell` precisely so the shell can be inerted
      // behind it), every global shell shortcut is suppressed. Ctrl/Cmd+K in
      // particular would otherwise open the command palette *on top of* that
      // dialog -- a second modal living outside the first one's focus trap
      // and outside the inert region -- and Escape would close shell surfaces
      // the user cannot even see. The dialog stops propagation of its own
      // keydowns too; this is the independent guard for events that never
      // pass through it (dispatched directly on `window`, or fired while
      // focus somehow sits outside both regions).
      if (blockingModalOpen) {
        return;
      }
      if (event.key === "Escape") {
        setNavOpen(false);
        setProjectPanelMode(null);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [blockingModalOpen]);

  useEffect(() => {
    if (navOpen) {
      wasNavOpenRef.current = true;
      railCloseRef.current?.focus();
    } else if (wasNavOpenRef.current) {
      wasNavOpenRef.current = false;
      mobileMenuTriggerRef.current?.focus();
    }
  }, [navOpen]);

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
    if (next === "library" || next === "settings") {
      void refresh();
    }
  };

  const selectProject = async (projectId: string) => {
    setProjectSubmitting(true);
    setProjectError(null);
    try {
      await activateProject(projectId);
      setProjects((current) =>
        current.map((project) => ({
          ...project,
          is_active: project.id === projectId,
        })),
      );
      setActiveProjectId(projectId);
      updateProjectUrl(projectId);
      await loadWorkspace(projectId);
    } catch (error) {
      setProjectError(
        error instanceof Error ? error.message : "Project selection failed.",
      );
    } finally {
      setProjectSubmitting(false);
    }
  };

  const openProjectPanel = (mode: "create" | "edit") => {
    const activeProject = projects.find((project) => project.id === activeProjectId);
    setProjectName(mode === "edit" ? activeProject?.name ?? "" : "");
    setProjectDescription(mode === "edit" ? activeProject?.description ?? "" : "");
    setArchiveConfirmationOpen(false);
    setProjectError(null);
    setProjectPanelMode(mode);
  };

  const submitProject = async () => {
    setProjectSubmitting(true);
    setProjectError(null);
    try {
      const project =
        projectPanelMode === "edit" && activeProjectId
          ? await updateProject(activeProjectId, {
              name: projectName,
              description: projectDescription,
              archive: false,
            })
          : await createProject({
              name: projectName,
              description: projectDescription,
            });
      const availableProjects = await loadProjects();
      setActiveProjectId(project.id);
      setProjects(
        availableProjects.map((availableProject) => ({
          ...availableProject,
          is_active: availableProject.id === project.id,
        })),
      );
      updateProjectUrl(project.id);
      setProjectPanelMode(null);
      await loadWorkspace(project.id);
    } catch (error) {
      setProjectError(
        error instanceof Error ? error.message : "Project changes could not be saved.",
      );
    } finally {
      setProjectSubmitting(false);
    }
  };

  const archiveProject = async () => {
    if (!activeProjectId) return;
    setProjectSubmitting(true);
    setProjectError(null);
    try {
      await updateProject(activeProjectId, { archive: true });
      const availableProjects = await loadProjects();
      const nextProject = availableProjects.find((project) => project.is_active) ?? null;
      setProjects(availableProjects);
      setActiveProjectId(nextProject?.id ?? null);
      setData(null);
      updateProjectUrl(nextProject?.id ?? null);
      setArchiveConfirmationOpen(false);
      setProjectPanelMode(nextProject ? null : "create");
      if (nextProject) {
        await loadWorkspace(nextProject.id);
      }
    } catch (error) {
      setProjectError(
        error instanceof Error ? error.message : "Project archive failed.",
      );
    } finally {
      setProjectSubmitting(false);
    }
  };

  const activeProject = projects.find((project) => project.id === activeProjectId);

  return (
    <div
      className="workbench-shell"
      data-workspace-ready={Boolean(data)}
      // The entire shell -- rail, main content, command palette -- is inert
      // while an application-modal dialog is open. That
      // dialog is portalled into `document.body`, outside this subtree, so it
      // is unaffected. Without this, the dialog's own focus trap is the only
      // thing keeping keyboard and assistive-technology users out of the
      // shell, and anything that moved focus programmatically (or any control
      // the trap's focusable-element query did not match) would land on
      // background content the user cannot see.
      inert={blockingModalOpen}
    >
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
            ref={railCloseRef}
            onClick={() => setNavOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        <div className="project-switcher">
          <select
            aria-label="Active project"
            disabled={projectSubmitting || projects.length === 0}
            value={activeProjectId ?? ""}
            onChange={(event) => {
              if (event.target.value) {
                void selectProject(event.target.value);
              }
            }}
          >
            {projects.length === 0 ? (
              <option value="">No personal project</option>
            ) : (
              projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))
            )}
          </select>
          <button
            aria-label="Create project"
            title="Create project"
            type="button"
            onClick={() => openProjectPanel("create")}
          >
            <Plus size={15} />
          </button>
          <button
            aria-label="Manage active project"
            disabled={!activeProject}
            title="Manage active project"
            type="button"
            onClick={() => openProjectPanel("edit")}
          >
            <Pencil size={14} />
          </button>
        </div>
        {projectError && !projectPanelMode ? (
          <p className="project-switcher-error">{projectError}</p>
        ) : null}

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
            <em>{data?.summary.library_items ?? "—"}</em>
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
        <nav className="rail-nav utility-nav" aria-label="Project utilities">
          <button
            className="rail-link"
            data-active={view === "agents"}
            aria-current={view === "agents" ? "page" : undefined}
            onClick={() => navigate("agents")}
          >
            <Bot size={17} />
            <span>Agents</span>
          </button>
          <button
            className="rail-link settings-link"
            data-active={view === "settings"}
            aria-current={view === "settings" ? "page" : undefined}
            onClick={() => navigate("settings")}
          >
            <Settings size={17} />
            <span>Project Settings</span>
          </button>
        </nav>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="mobile-menu-button"
              aria-label="Open navigation"
              aria-controls="project-navigation"
              aria-expanded={navOpen}
              ref={mobileMenuTriggerRef}
              onClick={() => setNavOpen(true)}
            >
              <Menu size={20} />
            </button>
            <div className="breadcrumbs">
              <small>Project workspace</small>
              <strong>{viewTitle(view)}</strong>
            </div>
          </div>
          <div className="topbar-actions">
            <button
              className="icon-button"
              aria-label="Open project settings"
              onClick={() => navigate("settings")}
            >
              <Settings size={18} />
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
          ) : view === "settings" ? (
            <ProjectSettingsView
              key={data?.settings.project_id ?? "settings-loading"}
              data={data}
              onRefresh={refresh}
            />
          ) : view === "agents" ? (
            <FoundryAgentCatalog />
          ) : (
            <StudioForCapability
              capability={view}
              projectId={activeProjectId}
            />
          )}
        </main>
      </div>

      {projectPanelMode ? (
        <div className="modal-backdrop" role="presentation">
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-dialog-title"
          >
            <div className="modal-heading">
              <div>
                <span className="eyebrow">Personal workspace</span>
                <h2 id="project-dialog-title">
                  {projectPanelMode === "create"
                    ? "Create project"
                    : "Manage project"}
                </h2>
              </div>
              <button
                aria-label="Close project dialog"
                onClick={() => setProjectPanelMode(null)}
              >
                <X size={19} />
              </button>
            </div>
            <p>
              Projects are private to your identity. A new project starts with
              the governed policy defaults and no sources, runs, or approvals.
            </p>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void submitProject();
              }}
            >
              <label className="field">
                <span>Project name</span>
                <input
                  autoFocus
                  required
                  disabled={projectSubmitting}
                  maxLength={120}
                  minLength={3}
                  value={projectName}
                  onChange={(event) => setProjectName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Description</span>
                <textarea
                  required
                  disabled={projectSubmitting}
                  maxLength={1000}
                  minLength={3}
                  rows={3}
                  value={projectDescription}
                  onChange={(event) => setProjectDescription(event.target.value)}
                />
              </label>
              {projectError ? (
                <p className="project-dialog-error" role="alert">
                  {projectError}
                </p>
              ) : null}
              {archiveConfirmationOpen ? (
                <div className="archive-confirmation">
                  <span>
                    Archive this project? Its records stay retained and it
                    leaves the project list.
                  </span>
                  <button
                    className="secondary-button"
                    disabled={projectSubmitting}
                    type="button"
                    onClick={() => setArchiveConfirmationOpen(false)}
                  >
                    Keep project
                  </button>
                  <button
                    className="primary-button"
                    disabled={projectSubmitting}
                    type="button"
                    onClick={() => void archiveProject()}
                  >
                    Confirm archive
                  </button>
                </div>
              ) : null}
              <div className="modal-actions">
                {projectPanelMode === "edit" ? (
                  <button
                    className="secondary-button project-archive-button"
                    disabled={projectSubmitting}
                    type="button"
                    onClick={() => setArchiveConfirmationOpen(true)}
                  >
                    <Archive size={15} />
                    Archive project
                  </button>
                ) : null}
                <button
                  className="secondary-button"
                  disabled={projectSubmitting}
                  type="button"
                  onClick={() => setProjectPanelMode(null)}
                >
                  Cancel
                </button>
                <button
                  className="primary-button"
                  disabled={projectSubmitting}
                  type="submit"
                >
                  {projectSubmitting ? "Saving" : "Save project"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}

    </div>
  );
}
