"use client";

import {
  Button,
} from "@fluentui/react-components";
import {
  CheckCircle2,
  CircleDashed,
  Clock3,
  Lock,
  Pencil,
  Play,
  Plus,
  ShieldCheck,
  Sparkles,
  Trash2,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import type { WorkspaceData } from "@/lib/api";
import { AgentChat, isChatCapability } from "@/components/agent-chat";
import { openBlockingModal } from "@/lib/blocking-modal";
import { isConnectorRunnable } from "@/lib/connector-availability";
import type {
  AutomationStep,
  AutomationStudioResult,
  CapabilityId,
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
  projectId?: string | null;
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
      <div className="agent-boundary-card">
        <p>
          Model text is supplemental analysis. It cannot grant permissions,
          calculate scores, approve actions, or promote unresolved claims to
          verified evidence.
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
      </div>
    </article>
  );
}

const INLINE_EVIDENCE_SOURCE_LIMIT = 5;

/**
 * Run provenance for a resolved studio run: which durable instance produced
 * the artifact, how far it got, and which stored passages it actually
 * resolved. Rendered by every studio so the artifact and the evidence that
 * backs it stay in the same place.
 */
function RunEvidence({ result }: { result: StudioResult }) {
  const citations = result.citations;
  return (
    <section className="run-evidence" aria-label="Evidence and lineage">
      <div className="evidence-section-heading">
        <span>Evidence &amp; lineage</span>
        <em>Run resolved</em>
      </div>
      <div className="evidence-run-card">
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
      </div>
      <div className="evidence-section">
        <div className="evidence-section-heading">
          <span>Resolved sources</span>
          <em>{citations.length}</em>
        </div>
        {citations.length ? (
          <div className="evidence-source-list">
            {citations
              .slice(0, INLINE_EVIDENCE_SOURCE_LIMIT)
              .map((citation, index) => (
                <article key={citation.id}>
                  <span>{index + 1}</span>
                  <div>
                    <strong>{citation.title}</strong>
                    <small>
                      {citation.section}
                      {citation.page_start ? ` · p. ${citation.page_start}` : ""}
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
      </div>
    </section>
  );
}

export const InstitutionalStudio: (props: StudioProps) => ReactNode = () => (
  <div className="studio-page institutional-studio institutional-coming-soon">
    <section
      className="work-iq-coming-soon"
      aria-labelledby="work-iq-coming-soon-title"
    >
      <div className="work-iq-coming-soon-mark" aria-hidden="true">
        <Sparkles size={28} />
      </div>
      <span className="eyebrow">Microsoft 365 integration</span>
      <h1 id="work-iq-coming-soon-title">Work IQ</h1>
      <p>Plugin coming soon</p>
    </section>
  </div>
);

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
    authorized:
      isConnectorRunnable(connector) &&
      connector.assigned_agents.includes("orchestration"),
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

const AUTOMATION_TEMPLATES: readonly {
  id: string;
  title: string;
  description: string;
  steps: readonly AutomationStep[];
}[] = [
  {
    id: "evidence-review-v2",
    title: "Evidence review",
    description: "Ingest → screen → synthesize",
    steps: defaultAutomationSteps(),
  },
  {
    id: "grant-review-v2",
    title: "Grant red team",
    description: "Draft → compliance → approval",
    steps: [
      {
        id: "parse-notice",
        label: "Parse notice",
        kind: "activity",
        depends_on: [],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "draft-response",
        label: "Draft response",
        kind: "agent",
        depends_on: ["parse-notice"],
        retry_limit: 1,
        approval_required: false,
      },
      {
        id: "compliance-review",
        label: "Compliance review",
        kind: "activity",
        depends_on: ["draft-response"],
        retry_limit: 1,
        approval_required: false,
      },
      {
        id: "approve-submission",
        label: "Approve submission",
        kind: "approval",
        depends_on: ["compliance-review"],
        retry_limit: 0,
        approval_required: true,
      },
    ],
  },
  {
    id: "dataset-profile-v2",
    title: "Dataset profile",
    description: "Validate → compute → interpret",
    steps: [
      {
        id: "validate-schema",
        label: "Validate schema",
        kind: "activity",
        depends_on: [],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "compute-profile",
        label: "Compute profile",
        kind: "fan_out",
        depends_on: ["validate-schema"],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "interpret-results",
        label: "Interpret results",
        kind: "agent",
        depends_on: ["compute-profile"],
        retry_limit: 1,
        approval_required: false,
      },
    ],
  },
];

function cloneAutomationSteps(steps: readonly AutomationStep[]): AutomationStep[] {
  return steps.map((step) => ({
    ...step,
    depends_on: [...step.depends_on],
  }));
}

function workflowConfigurationFingerprint(
  template: string,
  trigger: string,
  steps: readonly AutomationStep[],
) {
  return JSON.stringify({ template, trigger, steps });
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
  const initialTemplate = AUTOMATION_TEMPLATES[0].id;
  const initialTrigger = "Manual";
  const initialSteps = AUTOMATION_TEMPLATES[0].steps;
  const [template, setTemplate] = useState(initialTemplate);
  const [trigger, setTrigger] = useState(initialTrigger);
  const [zoom, setZoom] = useState(100);
  const [steps, setSteps] = useState<AutomationStep[]>(
    cloneAutomationSteps(initialSteps),
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
  // Modal focus lifecycle for the activation confirmation dialog: move
  // focus into the dialog when it opens, and restore it to the trigger
  // that opened it when it closes. `wasActivationOpenRef` distinguishes a
  // genuine close (open -> closed) from the initial mount (never opened),
  // so we don't yank focus to the trigger button on first render.
  const activateTriggerRef = useRef<HTMLButtonElement | null>(null);
  const activationCloseButtonRef = useRef<HTMLButtonElement | null>(null);
  const activationStatusRef = useRef<HTMLParagraphElement | null>(null);
  const wasActivationOpenRef = useRef(false);
  useEffect(() => {
    if (activationConfirmOpen) {
      wasActivationOpenRef.current = true;
      activationCloseButtonRef.current?.focus();
    } else if (wasActivationOpenRef.current) {
      wasActivationOpenRef.current = false;
      // Restore focus to the control that opened the dialog -- but only if it
      // can still actually receive focus. A *successful* activation disables
      // that trigger (`disabled={!canActivate || activated}`), and browsers
      // silently refuse to focus a disabled element, so calling `.focus()` on
      // it would strand focus on `document.body` and drop a keyboard user
      // back to the top of the page with no announcement. In that case move
      // focus to the activation status message instead, which is both
      // programmatically focusable and the most relevant thing to read at
      // that moment.
      const trigger = activateTriggerRef.current;
      if (trigger && !trigger.disabled) {
        trigger.focus();
      } else {
        activationStatusRef.current?.focus();
      }
    }
  }, [activationConfirmOpen]);
  // Suppress the surrounding shell (global shortcuts such as Ctrl/Cmd+K, and
  // the shell's own focusable content) for as long as this dialog is open.
  // See src/lib/blocking-modal.ts for why this cannot simply be passed down
  // as a prop.
  useEffect(() => {
    if (!activationConfirmOpen) {
      return;
    }
    return openBlockingModal();
  }, [activationConfirmOpen]);
  const [catalogPreviewKey, setCatalogPreviewKey] = useState<string | null>(
    null,
  );
  const [graphDraftVersion, setGraphDraftVersion] = useState(1);
  const [cloneStatus, setCloneStatus] = useState<string | null>(null);
  const [validationPending, setValidationPending] = useState(false);
  // The version (graphDraftVersion) that was actually submitted the last
  // time a dry run passed. Content equality alone is not enough: "Clone
  // into a new draft" (see below) intentionally bumps graphDraftVersion
  // without changing template/trigger/steps content, and that new draft
  // version must require its own fresh validate+dry-run before it can be
  // activated even though it is byte-for-byte identical to the version
  // that already passed. Tracking the submitted version alongside content
  // closes that gap.
  const [validatedAtVersion, setValidatedAtVersion] = useState<number | null>(
    null,
  );
  const automation =
    result && "template_id" in result
      ? (result as AutomationStudioResult)
      : null;
  // Derive the "last known-good configuration" fingerprint directly from
  // the server's own dry-run result (its echoed template_id/trigger/steps)
  // instead of from client-local initial state. Seeding this from
  // hardcoded defaults (the prior behavior) meant that mounting the studio
  // with any previously passed `result` for this capability -- regardless
  // of which graph actually produced it -- could authorize activation of
  // whatever the default template happened to be, without a matching dry
  // run ever having run against the currently displayed configuration.
  const validatedConfiguration = useMemo(
    () =>
      automation
        ? workflowConfigurationFingerprint(
            automation.template_id,
            automation.trigger,
            automation.steps,
          )
        : null,
    [automation],
  );
  const catalogItems = buildCatalogItems(data);
  const catalogLoading = data === null || data === undefined;
  const orchestrationRuns = (data?.runs ?? []).filter(
    (run) => run.capability === "orchestration",
  );

  const dependedOnIds = new Set(steps.flatMap((step) => step.depends_on));
  const currentConfiguration = workflowConfigurationFingerprint(
    template,
    trigger,
    steps,
  );
  const canActivate = automation
    ? automation.dry_run_status === "passed" &&
      automation.validation_errors.length === 0 &&
      !error &&
      !running &&
      !validationPending &&
      validatedConfiguration === currentConfiguration &&
      validatedAtVersion === graphDraftVersion
    : false;
  const resetActivation = () => {
    setActivated(false);
    // Any edit must invalidate the activation fingerprint gate itself, not
    // just the transient `activated` flag: without this, editing a step
    // away and then back to byte-identical content leaves
    // `currentConfiguration` re-equal to the stale `validatedConfiguration`,
    // and since `graphDraftVersion` never otherwise changes on ordinary
    // edits, `validatedAtVersion === graphDraftVersion` stays trivially
    // true -- so `canActivate` can flip back to true without a fresh dry
    // run ever having run against the edited draft. Bumping
    // graphDraftVersion here (mirroring the existing "Clone into a new
    // draft" pattern below) guarantees `validatedAtVersion` can never
    // coincidentally match again until a new dry run explicitly records it.
    setGraphDraftVersion((current) => current + 1);
  };

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
    resetActivation();
  };

  const removeStep = (id: string) => {
    setSteps((current) => current.filter((step) => step.id !== id));
    resetActivation();
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
    resetActivation();
  };

  return (
    <>
      <div
        className="studio-page automation-studio"
        // While the activation confirmation dialog is open, the rest of this
        // page is marked inert: the dialog is rendered through a portal (see
        // below) so it lives outside this subtree and is unaffected. Without
        // this, the full-viewport `.modal-backdrop` blocks pointer clicks on
        // the background but does not stop keyboard Tab navigation or
        // assistive-tech focus from reaching background controls (there is
        // no separate focus trap), so a keyboard user could still reach and
        // change the trigger/template/steps behind the "modal" dialog. This
        // is defense-in-depth alongside (not a replacement for) the
        // `canActivate` recheck in the Confirm handler below, which remains
        // the actual authorization boundary.
        inert={activationConfirmOpen}
      >
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
        onSubmit={async (event) => {
          event.preventDefault();
          const submittedVersion = graphDraftVersion;
          setValidationPending(true);
          try {
            await onRun(
              "orchestration",
              "Validate and dry run the configured evidence workflow.",
              { inputs: { template_id: template, trigger, steps } },
            );
            // Record which draft version this dry run actually validated.
            // Whether the *content* now matches is derived from the
            // server's own echoed result above (validatedConfiguration),
            // not from what we optimistically submitted.
            setValidatedAtVersion(submittedVersion);
          } finally {
            setValidationPending(false);
          }
        }}
      >
        <section className="template-strip">
          {AUTOMATION_TEMPLATES.map((templateOption) => (
            <button
              type="button"
              data-active={template === templateOption.id}
              aria-pressed={template === templateOption.id}
              key={templateOption.id}
              onClick={() => {
                setTemplate(templateOption.id);
                setSteps(cloneAutomationSteps(templateOption.steps));
                setEditingId(null);
                setDraft(null);
                setAddingStep(false);
                resetActivation();
              }}
            >
              <span className="template-icon">
                <Workflow size={18} />
              </span>
              <span>
                <strong>{templateOption.title}</strong>
                <small>{templateOption.description}</small>
              </span>
              {template === templateOption.id ? <CheckCircle2 size={17} /> : null}
            </button>
          ))}
          <label className="field trigger-field">
            <span>Trigger</span>
            <select
              value={trigger}
              onChange={(event) => {
                setTrigger(event.target.value);
                resetActivation();
              }}
            >
              <option>Manual</option>
              <option>Schedule</option>
              <option>Webhook</option>
              <option>GitHub</option>
              <option>Library upload</option>
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
                <output aria-label="Workflow zoom" aria-live="polite">
                  {zoom}%
                </output>
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
            <RunButton running={running || validationPending}>
              Validate & dry run
            </RunButton>
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
              ref={activateTriggerRef}
            >
              {activated ? "Activated (draft workspace)" : "Activate after approval"}
            </button>
            {activated ? (
              <p
                className="activation-status"
                role="status"
                tabIndex={-1}
                ref={activationStatusRef}
                data-testid="workflow-activation-status"
              >
                Workflow activated for this draft workspace. Edit the graph to
                require a new passing dry run before activating again.
              </p>
            ) : null}
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
                      // Bumping graphDraftVersion (below) is sufficient to
                      // invalidate canActivate: validatedAtVersion will no
                      // longer match the new version even though the
                      // content-based fingerprint is unchanged, so this new
                      // draft version requires its own fresh dry run.
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
                    title="Pausing is unavailable in direct-execution mode."
                  >
                    Pause
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Resuming is unavailable in direct-execution mode."
                  >
                    Resume
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Retrying is unavailable in direct-execution mode."
                  >
                    Retry
                  </button>
                  <button
                    type="button"
                    disabled
                    title="Cancelling is unavailable in direct-execution mode."
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
      {automation ? (
        <>
          <RunEvidence result={automation} />
          <InsightCard result={automation} />
        </>
      ) : null}
      </div>
      {activationConfirmOpen
        ? createPortal(
            <div className="modal-backdrop" role="presentation">
              <div
                className="modal-card"
                role="dialog"
                aria-modal="true"
                aria-labelledby="activate-workflow-title"
                onKeyDown={(event) => {
                  // Every keydown that happens inside this dialog stops here.
                  // The dialog is portalled into `document.body`, so without
                  // this its native events keep bubbling to the `window`
                  // keydown listener in research-workbench.tsx, where
                  // Ctrl/Cmd+K would open the command palette *on top of*
                  // this dialog -- a second modal outside this focus trap and
                  // outside the shell's `inert` region. Stopping propagation
                  // unconditionally (rather than per-shortcut) means a new
                  // global shortcut added to the shell later cannot silently
                  // reintroduce that escape hatch.
                  event.stopPropagation();
                  if (event.key === "Escape") {
                    // Escape behaves exactly like Cancel/the close button: it
                    // never activates, regardless of `canActivate`.
                    setActivationConfirmOpen(false);
                    return;
                  }
                  if (event.key !== "Tab") return;
                  // `currentTarget` is exactly this dialog element (the one
                  // this handler is bound to), so unlike a separately-read
                  // ref it is never null here -- no defensive guard needed.
                  const container = event.currentTarget;
                  const focusable = Array.from(
                    container.querySelectorAll<HTMLElement>(
                      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
                    ),
                  );
                  // The dialog always renders its close button and a Cancel
                  // button (neither is ever disabled), so `focusable` can
                  // never actually be empty; no length guard is needed
                  // before indexing into it.
                  const first = focusable[0];
                  const last = focusable[focusable.length - 1];
                  // Keep keyboard focus contained within the dialog while it
                  // is open: wrap Tab past the last focusable element back to
                  // the first, and Shift+Tab past the first back to the
                  // last, so a keyboard user (or screen reader) can never
                  // Tab out into the `inert`-marked background page.
                  if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                  } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                  }
                }}
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
                    ref={activationCloseButtonRef}
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
                    disabled={!canActivate}
                    onClick={() => {
                      // Recheck `canActivate` at confirm time, not just when the
                      // dialog was opened: the dialog can stay open across an
                      // edit (e.g. a step save in another panel, or a stale
                      // background revalidation) that invalidates the
                      // fingerprint gate while the confirmation is pending.
                      // Without this guard, a stale-open confirm dialog could
                      // activate a draft that no longer matches its last
                      // passing dry run.
                      if (!canActivate) return;
                      setActivated(true);
                      setActivationConfirmOpen(false);
                    }}
                  >
                    Confirm activation
                  </button>
                </div>
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
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
  const commitOnEnter = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (isNew && !draft.label.trim()) return;
    onCommit();
  };
  return (
    <div className="step-editor-form">
      <label className="field">
        <span>Step label</span>
        <input
          value={draft.label}
          onKeyDown={commitOnEnter}
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
          onKeyDown={commitOnEnter}
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
  // The four evidence/analysis capabilities share one chat surface: their
  // agents already carry the instructions, tools, and boundary that the old
  // multi-step forms re-stated in the browser. Institutional Q&A keeps its
  // version-and-abstain workflow and orchestration keeps its DAG editor,
  // because neither is a conversation.
  if (isChatCapability(capability)) {
    return <AgentChat capability={capability} projectId={props.projectId} />;
  }
  switch (capability) {
    case "institutional_qa":
      return <InstitutionalStudio {...props} />;
    case "orchestration":
      return <AutomationStudio {...props} />;
    default:
      return null;
  }
}
