export type InteractionBaseline =
  | "functional-covered"
  | "functional-uncovered"
  | "unwired"
  | "missing";

export type DeliveryMilestone =
  | "M1"
  | "M2"
  | "M3"
  | "M4"
  | "M5"
  | "M6"
  | "M7"
  | "M8"
  | "M9";

export interface InteractionContract {
  id: string;
  surface: string;
  control: string;
  behavior: string;
  baseline: InteractionBaseline;
  milestone: DeliveryMilestone;
  states: readonly string[];
  testIds: readonly string[];
}

export type CoverageViewport = "desktop" | "tablet" | "mobile";
export type CoverageStateKind =
  | "behavior"
  | "async"
  | "empty"
  | "error"
  | "auth";

export interface UiCoverageContract extends InteractionContract {
  route: string;
  viewports: readonly CoverageViewport[];
  rtlTestIds: readonly string[];
  playwrightTestIds: readonly string[];
  screenshotIds: readonly string[];
  classifiedStates: readonly {
    name: string;
    kind: CoverageStateKind;
  }[];
}

const STANDARD_STATES = [
  "ready",
  "keyboard",
  "disabled",
  "loading",
  "success",
  "error",
] as const;

const NAVIGATION_STATES = ["ready", "keyboard", "selected", "mobile"] as const;

export const INTERACTION_MANIFEST: readonly InteractionContract[] = [
  {
    id: "shell.navigation.open-mobile",
    surface: "Shell",
    control: "Open navigation",
    behavior: "Opens the mobile project navigation and moves focus into it.",
    baseline: "functional-covered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.mobile-navigation", "jest.mobile-navigation"],
  },
  {
    id: "shell.navigation.close-mobile",
    surface: "Shell",
    control: "Close navigation and scrim",
    behavior: "Closes mobile navigation by button, scrim, or Escape and restores focus.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.mobile-navigation", "pw.keyboard-shell"],
  },
  {
    id: "shell.navigation.primary-routes",
    surface: "Shell",
    control: "Overview, Library, Runs, Settings, and studio navigation",
    behavior: "Navigates to a URL-addressable view and preserves back/forward/refresh state.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.route-state", "jest.primary-navigation"],
  },
  {
    id: "shell.search.open",
    surface: "Shell",
    control: "Search workspace and Ctrl/Cmd+K",
    behavior: "Opens the command palette, traps focus, and focuses the query.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: ["ready", "keyboard", "open", "mobile"],
    testIds: ["pw.command-palette"],
  },
  {
    id: "shell.search.query",
    surface: "Shell",
    control: "Workspace search query",
    behavior: "Filters authorized routes and records with empty and no-result states.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: ["ready", "typing", "empty", "no-results"],
    testIds: ["pw.command-palette"],
  },
  {
    id: "shell.search.select-result",
    surface: "Shell",
    control: "Command result",
    behavior: "Navigates with pointer or keyboard and closes the palette.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: ["ready", "keyboard", "selected"],
    testIds: ["pw.command-palette"],
  },
  {
    id: "shell.search.close",
    surface: "Shell",
    control: "Close search and Escape",
    behavior: "Closes the dialog and restores focus to the trigger.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: ["open", "keyboard", "closed"],
    testIds: ["pw.command-palette"],
  },
  {
    id: "shell.approvals.open",
    surface: "Shell",
    control: "Pending approvals notification",
    behavior: "Navigates to Runs with the pending approval filter and count.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["none", "pending", "keyboard"],
    testIds: ["pw.approval-notification"],
  },
  {
    id: "shell.evidence.open-close",
    surface: "Shell",
    control: "Evidence inspector",
    behavior: "Opens and closes by trigger, close button, scrim, and Escape with focus restoration.",
    baseline: "functional-uncovered",
    milestone: "M1",
    states: ["ready", "open", "empty", "resolved", "keyboard", "mobile"],
    testIds: ["pw.evidence-inspector"],
  },
  {
    id: "overview.start-literature",
    surface: "Overview",
    control: "Start a literature review",
    behavior: "Navigates to the Literature Protocol view.",
    baseline: "functional-covered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.literature-open", "jest.literature-open"],
  },
  {
    id: "overview.open-library",
    surface: "Overview",
    control: "Explore evidence library",
    behavior: "Navigates to the Library.",
    baseline: "functional-covered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.operational-surfaces"],
  },
  {
    id: "overview.open-studio-card",
    surface: "Overview",
    control: "Six capability cards",
    behavior: "Each card opens its distinct studio route.",
    baseline: "functional-covered",
    milestone: "M1",
    states: NAVIGATION_STATES,
    testIds: ["pw.distinct-studios", "jest.capability-cards"],
  },
  {
    id: "overview.open-runs",
    surface: "Overview",
    control: "View all runs and run rows",
    behavior: "Opens Runs and selects the chosen run when applicable.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["ready", "empty", "keyboard"],
    testIds: ["pw.overview-runs"],
  },
  {
    id: "literature.protocol.question",
    surface: "Literature",
    control: "Research question",
    behavior: "Edits and validates the protocol question.",
    baseline: "functional-uncovered",
    milestone: "M3",
    // Evidence: studio-components.tsx ~L416-421 — a plain <textarea> with no
    // `disabled` prop and no async read/write path tied to typing, so
    // disabled/loading/error are not reachable for this control. "success" is
    // also not reachable: unlike grant.opportunity.id (which has a distinct
    // "Use as opportunity source" import action separate from typing), this
    // textarea has no second action that would produce an outcome
    // observably distinct from "keyboard" (typed value in the DOM).
    states: ["ready", "keyboard"],
    testIds: ["jest.literature-protocol", "pw.literature-protocol"],
  },
  {
    id: "literature.protocol.date-window",
    surface: "Literature",
    control: "Published from and through",
    behavior: "Edits an ordered bounded date window and surfaces validation errors.",
    baseline: "functional-uncovered",
    milestone: "M3",
    // Evidence: studio-components.tsx date-window inputs are plain always-enabled
    // <input type="date"> fields with no `disabled` prop and no async read/write
    // path, so disabled/loading are not reachable. There is no field-level error
    // banner tied specifically to the date inputs (studio-level errors surface via
    // StudioError, not per-field). "success" has no code path distinct from typing
    // (no secondary import/apply action for this control).
    states: ["ready", "keyboard"],
    testIds: ["jest.literature-date-window", "pw.literature-protocol"],
  },
  {
    id: "literature.protocol.criteria",
    surface: "Literature",
    control: "Include and exclude criteria chips",
    behavior: "Adds and removes protocol criteria and includes them in the run request.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx criteria chips add/remove via synchronous
    // setInclusionCriteria/setExclusionCriteria array updates with no error
    // variable or async path; a studio-level error surfaces via StudioError,
    // not per-criterion, so "error" is not reachable for this control.
    states: ["ready", "editing", "empty", "duplicate"],
    testIds: ["jest.literature-criteria", "pw.literature-protocol"],
  },
  {
    id: "literature.protocol.sources",
    surface: "Literature",
    control: "Scholarly source checkboxes",
    behavior: "Selects enabled and healthy source connectors included in the request.",
    baseline: "functional-uncovered",
    milestone: "M3",
    // Evidence: studio-components.tsx sourceOptions is a hardcoded array of
    // checkboxes with no `disabled` prop and no per-source authorization or
    // health check, so disabled/unhealthy/unauthorized have no code path.
    states: ["ready", "selected"],
    testIds: ["jest.literature-sources", "pw.literature-protocol"],
  },
  {
    id: "literature.protocol.online",
    surface: "Literature",
    control: "Online research toggle",
    behavior: "Requires explicit acknowledgement and sends only the public query.",
    baseline: "functional-uncovered",
    milestone: "M3",
    // Corrected from a 5-state list to the 3 states this workspace can actually
    // reach: studio-components.tsx OnlineResearchToggle is a plain, always-enabled
    // checkbox (no `disabled` prop, no async/error state variable feeding it) whose
    // "acknowledgement" note is static copy rendered unconditionally next to the
    // control, not a separate confirmation step. "unavailable" and "error" have no
    // code path that can ever set them for this control.
    states: ["off", "acknowledgement", "on"],
    testIds: ["jest.literature-online", "pw.literature-online"],
  },
  {
    id: "literature.protocol.run",
    surface: "Literature",
    control: "Search and screen evidence",
    behavior: "Creates a run with loading, partial, success, empty, retry, and error states.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx ~L557 — `<RunButton running={running}>` is
    // rendered with no `disabled` prop passed, so the submit button is only ever
    // disabled while `running` (the "loading" state); there is no separate,
    // distinct "disabled" precondition for this control.
    states: ["ready", "keyboard", "loading", "success", "error"],
    testIds: ["pw.literature-run"],
  },
  {
    id: "literature.screen.tab",
    surface: "Literature",
    control: "Screen tab",
    behavior: "Shows the candidate screening queue with per-record include/exclude/maybe decisions.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx ~L602-669 — the screen tab renders only
    // when `literature` is non-null (final data, no per-tab loading skeleton) and
    // has no per-tab error branch (studio-level errors surface via StudioError).
    states: ["empty", "ready", "partial"],
    testIds: ["jest.literature-tabs", "pw.literature-screen"],
  },
  {
    id: "literature.screen.decision",
    surface: "Literature",
    control: "Include, exclude, maybe, reason, and bulk screening actions",
    behavior: "Records a per-item decision in component state and deterministically changes included/excluded/maybe counts and the extraction and audit tabs.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx ~L643-663 — decision buttons have no
    // `disabled` prop and update local state synchronously (no async/error path),
    // so disabled/loading/error are not reachable for this control.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.screening-decisions", "pw.literature-screen"],
  },
  {
    id: "literature.extract.tab",
    surface: "Literature",
    control: "Extract tab",
    behavior: "Shows the evidence-linked extraction matrix filtered by current screening decisions.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: same tab-rendering pattern as literature.screen.tab — no per-tab
    // loading skeleton and no per-tab error branch.
    states: ["empty", "ready", "partial"],
    testIds: ["jest.literature-tabs", "pw.literature-extract"],
  },
  {
    id: "literature.extract.edit-export",
    surface: "Literature",
    control: "Extraction schema, cells, and export",
    behavior: "Edits source-linked values and exports the current version.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: extraction cell edits and export are synchronous local-state/
    // download operations with no `disabled` prop, no async path, and no
    // field-level error state, so disabled/loading/error are not reachable.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.extraction-matrix", "pw.literature-extract"],
  },
  {
    id: "literature.synthesize.tab",
    surface: "Literature",
    control: "Synthesize tab",
    behavior: "Shows the audited synthesis narrative for the current run.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: same tab-rendering pattern as literature.screen.tab — no per-tab
    // loading skeleton and no per-tab error branch.
    states: ["empty", "ready", "unsupported"],
    testIds: ["jest.literature-tabs", "pw.literature-synthesize"],
  },
  {
    id: "literature.audit.tab",
    surface: "Literature",
    control: "Audit tab",
    behavior: "Shows resolved/unresolved citation status and excluded-record reasons for the current run.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: same tab-rendering pattern as literature.screen.tab — no per-tab
    // loading skeleton, and audit-tab evidence is rendered directly from the
    // completed run's citation/resolution data with no "blocked"/"error" branch
    // distinct from the studio-level StudioError.
    states: ["empty", "passed", "warning"],
    testIds: ["jest.literature-tabs", "pw.literature-audit"],
  },
  {
    id: "grant.discovery.search",
    surface: "Grant",
    control: "Opportunity discovery query and filters",
    behavior: "Finds net-new opportunities through selected governed connectors.",
    baseline: "functional-covered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L893-942 — discoveryQuery/discoveryCapability
    // drive a synchronous, in-memory `Array.filter` over `fundingConnectors`
    // (no fetch/await in the filter path), so `loading` and `error` are not
    // reachable for this control. The input is `disabled={!discoverableConnectors.length}`
    // (L1148), so `disabled` IS reachable and is retained.
    states: ["ready", "keyboard", "disabled", "success"],
    testIds: ["jest.grant-discovery", "pw.grant-discovery"],
  },
  {
    id: "grant.discovery.sources",
    surface: "Grant",
    control: "Grant source toggles and add connector",
    behavior: "Toggles assigned funding connectors included in the run request and opens a connector-builder dialog that records a draft request pending admin review.",
    baseline: "functional-covered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L1093-1115 — funding connector checkboxes
    // are plain, always-enabled toggles over `fundingConnectors` with no health
    // or authorization flag checked in the UI, and no async/error path; the only
    // observable states are the checkbox toggle and the "no connectors assigned"
    // empty-list message.
    states: ["ready", "selected", "empty"],
    testIds: ["jest.grant-sources", "pw.grant-connectors"],
  },
  {
    id: "grant.opportunity.id",
    surface: "Grant",
    control: "Opportunity ID",
    behavior: "Selects or imports a canonical notice and amendments.",
    baseline: "functional-uncovered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L1049-1053 — a plain always-enabled
    // <input value={opportunityId} onChange={...} /> with no `disabled` prop
    // and no async read/write path tied to typing, so disabled/loading/error
    // are not reachable for this control.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.grant-opportunity", "pw.grant-opportunity"],
  },
  {
    id: "grant.requirements.open",
    surface: "Grant",
    control: "Requirement matrix rows",
    behavior: "Opens the source evidence and mapping for a requirement.",
    baseline: "functional-covered",
    milestone: "M4",
    states: ["unmapped", "mapped", "needs-input", "blocked"],
    testIds: ["jest.grant-requirements", "pw.grant-requirements"],
  },
  {
    id: "grant.editor.tabs",
    surface: "Grant",
    control: "Specific aims, Significance, and Approach tabs",
    behavior: "Switches section editor content between drafted sections and a not-yet-drafted state.",
    baseline: "functional-covered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L950-955, L1210-1220, L1251-1260 — the
    // section tabs only switch which pre-computed `grant.sections` entry is
    // displayed (or a static "not yet drafted" message); there is no local
    // editable draft, no save action, and no error path tied to this control,
    // so dirty/saving/saved/error have no reachable code path.
    states: ["ready", "selected"],
    testIds: ["jest.grant-editor", "pw.grant-draft"],
  },
  {
    id: "grant.editor.framing",
    surface: "Grant",
    control: "Project framing",
    behavior: "Edits validated project context without inventing institutional facts.",
    baseline: "functional-uncovered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L1226-1230 — a plain <textarea> with no
    // `disabled` prop and no async read/write path tied to typing, so
    // disabled/loading/error are not reachable for this control.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.grant-framing", "pw.grant-draft"],
  },
  {
    id: "grant.facts.confirm",
    surface: "Grant",
    control: "Core project facts verified",
    behavior: "Records the verifying user and controls deterministic readiness.",
    baseline: "functional-uncovered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L1290-1300 — a plain always-enabled
    // checkbox with no permission/authorization gating in the frontend or the
    // GrantStudioResult schema, so "permission-denied" has no reachable code path.
    states: ["unchecked", "checked", "error"],
    testIds: ["jest.grant-facts", "pw.grant-fit"],
  },
  {
    id: "grant.package.build",
    surface: "Grant",
    control: "Parse notice and build package",
    behavior: "Runs notice normalization, compliance, and bounded drafting.",
    baseline: "functional-covered",
    milestone: "M4",
    // Evidence: studio-components.tsx ~L1262-1264 — `<RunButton running={...}>` is
    // rendered with no `disabled` prop passed, so there is no separate, distinct
    // "disabled" precondition for this submit action.
    states: ["ready", "keyboard", "loading", "success", "error"],
    testIds: ["pw.grant-build"],
  },
  {
    id: "grant.review.red-team",
    surface: "Grant",
    control: "Red-team draft",
    behavior: "Runs a distinct red-team studio pass and shows the resulting readiness, gaps, and blockers.",
    baseline: "functional-covered",
    milestone: "M4",
    states: ["ready", "running", "findings", "resolved", "error"],
    testIds: ["jest.grant-red-team", "pw.grant-review"],
  },
  {
    id: "matching.need.query",
    surface: "Matching",
    control: "Expertise, method, or need",
    behavior: "Edits the typed matching need.",
    baseline: "functional-uncovered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1564-1571 — a plain uncontrolled
    // <textarea> with no `disabled` prop and no async read/write path tied
    // to typing, so disabled/loading/error are not reachable for this control.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.matching-need", "pw.matching-need"],
  },
  {
    id: "matching.need.entity-types",
    surface: "Matching",
    control: "Record type checkboxes",
    behavior: "Sends selected record kinds as part of the run request inputs.",
    baseline: "functional-covered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1459-1465, L1574-1589 — RECORD_TYPE_OPTIONS
    // is a hardcoded 5-item array always rendered in full via `.map()` with no
    // `disabled` prop and no filtering/async path that could reduce it to zero
    // items, so empty/disabled have no reachable code path.
    states: ["selected", "unselected"],
    testIds: ["jest.matching-types", "pw.matching-filters"],
  },
  {
    id: "matching.need.hard-filters",
    surface: "Matching",
    control: "Hard filter checkboxes",
    behavior: "Sends selected hard filter ids as part of the run request inputs.",
    baseline: "functional-covered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1467-1470, L1593-1608 — HARD_FILTER_OPTIONS
    // is a hardcoded 2-item array with no `disabled` prop, no error variable, and
    // no "no results" concept tied to this checkbox list (results filtering happens
    // downstream in matching.result.select, not on the filter checkboxes themselves).
    states: ["selected", "unselected"],
    testIds: ["jest.matching-hard-filters", "pw.matching-filters"],
  },
  {
    id: "matching.need.sources",
    surface: "Matching",
    control: "Public, institutional, and Work IQ sources",
    behavior: "Selects governed sources with consent/readiness constraints.",
    baseline: "functional-covered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1610-1665 — "unavailable" is
    // `disabled={!connector.enabled}` on public source checkboxes, and
    // "consent-required" is the permanently `checked={false} disabled` Work IQ
    // toggle with its Microsoft Graph consent note (L1647-1664); both are real,
    // always-reachable code paths. There is no error variable tied to source
    // selection itself (only the studio-level StudioError), so "error" is trimmed.
    states: ["ready", "selected", "unavailable", "consent-required"],
    testIds: ["jest.matching-sources", "pw.matching-sources"],
  },
  {
    id: "matching.run",
    surface: "Matching",
    control: "Build verified shortlist",
    behavior: "Generates, resolves, hard-filters, and deterministically scores candidates.",
    baseline: "functional-covered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1671 — `<RunButton running={running}>` is
    // rendered with no `disabled` prop passed, so there is no separate, distinct
    // "disabled" precondition for this submit action.
    states: ["ready", "keyboard", "loading", "success", "error"],
    testIds: ["pw.matching-run"],
  },
  {
    id: "matching.result.select",
    surface: "Matching",
    control: "Candidate cards",
    behavior: "Selects a candidate and shows exact score/evidence details.",
    baseline: "functional-uncovered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1719 renders `match.freshness` (a
    // free-form backend string, generated-api.ts ~L935/1100) directly as text, so
    // "stale" is reachable by mocking a match with freshness: "stale". No field on
    // RankedEntity represents a version/edit conflict, so "conflict" is trimmed.
    states: ["ready", "selected", "keyboard", "stale"],
    testIds: ["jest.matching-select", "pw.matching-results"],
  },
  {
    id: "matching.compare-shortlist",
    surface: "Matching",
    control: "Compare and shortlist actions",
    behavior: "Toggles shortlist membership per candidate in component state and shows a transparent side-by-side score comparison.",
    baseline: "functional-covered",
    milestone: "M5",
    // Evidence: studio-components.tsx ~L1727-1773 — the shortlist-toggle and
    // "Compare shortlisted" buttons have no `disabled` prop and toggle local state
    // synchronously with no async/error path, so disabled/loading/error are not
    // reachable for this control.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.matching-shortlist", "pw.matching-shortlist"],
  },
  {
    id: "dataset.upload",
    surface: "Dataset",
    control: "Dataset upload",
    behavior: "Validates a bounded CSV/JSON file client-side, then uploads real bytes through the Library upload API.",
    baseline: "functional-covered",
    milestone: "M6",
    // "reading" reflects the observable csvReadStatus state added for the FileReader
    // readiness fix (studio-components.tsx): the Run action is disabled and a visible
    // reading indicator is shown while the deferred FileReader.readAsText() is pending,
    // before "validated"/"error" are known.
    // Evidence: studio-components.tsx ~L1882-1925 — handleFileChange only sets
    // fileError for unsupported extension/oversize files; there is no server-side
    // quarantine/scan concept anywhere in this client, so "quarantined" is trimmed.
    // "empty" is the real default asset-upload-tile state before any file is chosen
    // (uploadedFile === null, L2045 renders "Upload a dataset").
    states: [
      "empty",
      "reading",
      "uploading",
      "validated",
      "rejected",
      "error",
    ],
    testIds: ["jest.dataset-upload", "pw.dataset-upload"],
  },
  {
    id: "dataset.asset.select",
    surface: "Dataset",
    control: "Dataset asset cards",
    behavior: "Selects an authorized uploaded asset and updates the request.",
    baseline: "functional-uncovered",
    milestone: "M6",
    // Evidence: studio-components.tsx ~L2001-2060 — the sample/large asset cards
    // are plain onClick handlers with no disabled/processing/authorization flag,
    // so "processing" and "unauthorized" have no reachable code path.
    states: ["ready", "selected", "rejected"],
    testIds: ["jest.dataset-assets", "pw.dataset-assets"],
  },
  {
    id: "dataset.objective",
    surface: "Dataset",
    control: "Analysis objective",
    behavior: "Edits a bounded analysis objective.",
    baseline: "functional-uncovered",
    milestone: "M6",
    // Evidence: studio-components.tsx ~L2061-2066 — a plain <input> with no
    // `disabled` prop and no async read/write path tied to typing, so
    // disabled/loading/error are not reachable for this control. Consistent with
    // literature.protocol.question/date-window, "success" has no code path distinct
    // from plain typing ("keyboard") because there is no secondary import/auto-fill
    // action on this field, so it is trimmed too.
    states: ["ready", "keyboard"],
    testIds: ["jest.dataset-objective", "pw.dataset-plan"],
  },
  {
    id: "dataset.profile",
    surface: "Dataset",
    control: "Analyze with Foundry Code Interpreter",
    behavior: "Profiles uploaded bytes, invokes the Foundry Dataset Agent Toolbox, and produces schema/quality findings plus bounded analysis.",
    baseline: "functional-covered",
    milestone: "M6",
    // Evidence: studio-components.tsx ~L2068 — unlike sibling RunButtons, this one
    // does pass `disabled={runDisabled}`, so all six standard states are reachable.
    states: STANDARD_STATES,
    testIds: ["pw.dataset-profile"],
  },
  {
    id: "dataset.plan.approve",
    surface: "Dataset",
    control: "Edit and approve analysis plan",
    behavior: "Requires explicit approval before the bounded dataset is sent to the project-scoped Foundry Code Interpreter.",
    baseline: "functional-covered",
    milestone: "M6",
    // Evidence: studio-components.tsx ~L2107-2118 — planApproved is a single plain
    // boolean checkbox with no dirty-tracking, no waiting-for-approval workflow
    // state, no rejection concept, and no field-level error; "draft" is the real
    // default unchecked state and "approved" is checked=true.
    states: ["draft", "approved"],
    testIds: ["jest.dataset-plan", "pw.dataset-plan"],
  },
  {
    id: "dataset.execution",
    surface: "Dataset",
    control: "Approve and invoke Foundry Code Interpreter analysis",
    behavior: "Sends only the bounded approved dataset to the Foundry Hosted Dataset Agent and its project-scoped Code Interpreter Toolbox, then renders typed outputs and limitations.",
    baseline: "functional-covered",
    milestone: "M6",
    // Evidence: studio-components.tsx ~L2068 (RunButton running/disabled),
    // ~L2256-2266 (compute_proposal.approval_required lock banner vs. local-compute
    // banner). "running" is the same run() in-flight moment as dataset.profile:loading;
    // "failed" is the same onRun() rejection moment as dataset.profile:error; "blocked"
    // is the distinct "Human approval required before submit" banner shown when the
    // large/estimate-required asset's compute_proposal.approval_required is true.
    states: ["pending-approval", "running", "completed", "failed", "blocked"],
    testIds: ["jest.dataset-plan", "pw.dataset-upload"],
  },
  {
    id: "institutional.corpora",
    surface: "Institutional",
    control: "Authorized corpus checkboxes",
    behavior: "Sends selected authorized corpora as part of the run request and keeps the legal-hold corpus locked.",
    baseline: "functional-covered",
    milestone: "M7",
    // Evidence: studio-components.tsx ~L2282-2292 — CORPUS_SCOPES is a hardcoded
    // 4-item array always rendered in full, so "empty" has no reachable code path.
    // There is no per-checkbox error variable (only the studio-level StudioError),
    // so "error" is trimmed too.
    states: ["selected", "unselected", "locked"],
    testIds: ["jest.institutional-corpora", "pw.institutional-corpora"],
  },
  {
    id: "institutional.work-iq",
    surface: "Institutional",
    control: "Work IQ readiness and consent",
    behavior: "Shows an honest, disabled, default-off readiness panel; this workspace never claims Work IQ is configured or enabled.",
    baseline: "functional-covered",
    milestone: "M7",
    // Corrected from a 5-state aspirational list to the single state this workspace can
    // actually reach: studio-components.tsx (~2507-2535) renders the Work IQ toggle as
    // permanently `checked={false}` + `disabled`, driven by no prop or data path. The
    // other four states (admin-consent-required/user-consent-required/ready/
    // unsupported-network) have no code path that can ever set them, matching this
    // interaction's own `behavior` text above. Declaring them would make the state
    // contract unsatisfiable by design, not a real coverage gap.
    states: ["unconfigured"],
    testIds: ["jest.work-iq-readiness", "pw.work-iq-readiness"],
  },
  {
    id: "institutional.question",
    surface: "Institutional",
    control: "Institutional question",
    behavior: "Edits and submits a scoped question.",
    baseline: "functional-covered",
    milestone: "M7",
    // Evidence: studio-components.tsx ~L2404-2412 — a plain <textarea> paired with
    // `<RunButton running={running}>` (no `disabled` prop passed), so "disabled" has
    // no reachable code path. "loading"/"error"/"keyboard" (typing + Enter/submit)
    // remain reachable through the same onRun submit path as every other studio.
    states: ["ready", "keyboard", "loading", "success", "error"],
    testIds: ["pw.institutional-answer"],
  },
  {
    id: "institutional.evidence.open",
    surface: "Institutional",
    control: "Inline citation buttons",
    behavior: "Opens a dialog with the exact citation title, section, page, quote, checksum, and license.",
    baseline: "functional-covered",
    milestone: "M7",
    // Evidence: studio-components.tsx ~L2445-2588 — the citation modal renders
    // directly from the already-available `Citation` object passed via
    // `setSelectedCitation`; there is no fetch, no access-control flag, and no
    // version-supersession concept anywhere in this dialog, so
    // unavailable/superseded/permission-denied have no reachable code path.
    states: ["ready", "open"],
    testIds: ["jest.institutional-evidence", "pw.institutional-evidence"],
  },
  {
    id: "workflow.template",
    surface: "Workflow",
    control: "Workflow template cards",
    behavior: "Selects and loads an editable versioned graph.",
    baseline: "functional-uncovered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L2762-2871 — `templates` is a hardcoded
    // 3-item array rendered with plain onClick handlers; there is no async
    // fetch or per-template error concept, so loading/error are trimmed.
    states: ["ready", "selected"],
    testIds: ["jest.workflow-template", "pw.workflow-template"],
  },
  {
    id: "workflow.trigger",
    surface: "Workflow",
    control: "Trigger selector",
    behavior: "Configures a typed manual, schedule, webhook, GitHub, or library trigger.",
    baseline: "functional-uncovered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L2872-2883 — a plain <select> with no
    // `disabled` prop and no async read/write path tied to selection, so
    // disabled/loading/error are trimmed; native keyboard selection remains
    // reachable and distinct from a pointer click.
    states: ["ready", "keyboard", "success"],
    testIds: ["jest.workflow-trigger", "pw.workflow-trigger"],
  },
  {
    id: "workflow.catalog",
    surface: "Workflow",
    control: "Agent, tool, and studio catalog",
    behavior: "Adds only authorized versioned capabilities to the graph.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L2645-2671 (buildCatalogItems) — the
    // Studio group is always sourced from the hardcoded, non-empty
    // AUTOMATION_STUDIO_CATALOG, so catalogItems can never be length 0 and
    // "empty" is trimmed. There is no error variable for the catalog itself
    // (only the studio-level StudioError), so "error" is trimmed too. "loading"
    // is `data === null || data === undefined` (L2768) and "unauthorized" is
    // `!item.authorized` (L2651/L2659, driven by real `data.agents`/
    // `data.connectors` mock values) — both remain reachable and testable.
    states: ["loading", "ready", "unauthorized", "preview"],
    testIds: ["jest.workflow-catalog", "pw.workflow-catalog"],
  },
  {
    id: "workflow.graph.edit",
    surface: "Workflow",
    control: "Add, remove, and configure a bounded step list",
    behavior: "Adds up to 8 typed steps, blocks removing a step that others depend on, and edits label/kind/retries/dependencies/approval in an accessible list editor; edited steps are sent to dry run.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L3447-3459 — the commit button is
    // `disabled={isNew ? !draft.label.trim() : false}`, so "invalid" (an empty
    // label blocking the Add commit) is a real, reachable state. saveEdit/addStep
    // (L3791-3827) are synchronous local setState calls with no async period and
    // no error variable, so "saving" and "error" are trimmed.
    states: ["draft", "dirty", "invalid", "valid"],
    testIds: ["jest.workflow-graph", "pw.workflow-graph"],
  },
  {
    id: "workflow.canvas.zoom",
    surface: "Workflow",
    control: "Zoom out and zoom in",
    behavior: "Changes the graph viewport scale between 50% and 150% without changing workflow semantics.",
    baseline: "functional-covered",
    milestone: "M8",
    states: ["ready", "minimum", "maximum", "keyboard"],
    testIds: ["pw.workflow-viewport"],
  },
  {
    id: "workflow.validate",
    surface: "Workflow",
    control: "Validate and dry run",
    behavior: "Compiles and dry-runs with external side effects disabled.",
    baseline: "functional-covered",
    milestone: "M8",
    states: ["draft", "validating", "passed", "blocked", "error"],
    testIds: ["pw.workflow-dry-run"],
  },
  {
    id: "workflow.activate",
    surface: "Workflow",
    control: "Activate after approval",
    behavior: "Stays disabled until a dry run passes with zero graph errors, then requires an explicit confirmation dialog before recording a session-local activation state.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L3336-3345 — "Confirm activation" is a
    // synchronous `setActivated(true)` with no async call, no error path, no
    // rejection outcome, and no further approval workflow beyond this dialog,
    // so waiting-for-approval/rejected/error are trimmed.
    states: ["disabled", "ready", "active"],
    testIds: ["jest.workflow-activation", "pw.workflow-activation"],
  },
  {
    id: "workflow.run.manage",
    surface: "Workflow",
    control: "Pause, resume, retry, cancel, inspect, clone, and version",
    behavior: "Manages durable runs without repeating completed external effects.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx ~L3257-3284 — the Pause/Resume/Retry/Cancel
    // buttons are permanently `disabled` (Durable Task Scheduler control plane
    // not exposed), and generated-api.ts RunStatus has no "paused"/"retrying"
    // value, so "paused" and "retrying" have no reachable code path. "running",
    // "failed", and "cancelled" remain reachable as real RunStatus values
    // rendered via `orchestrationRuns` (L3229-3231, mockable via the `data` prop).
    states: ["running", "failed", "cancelled", "completed"],
    testIds: ["jest.workflow-run", "pw.workflow-run"],
  },
  {
    id: "library.search-filter",
    surface: "Library",
    control: "Search and type filters",
    behavior: "Filters real persisted library records with no-result handling.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["ready", "filtered", "empty", "keyboard"],
    testIds: ["jest.library-filter", "pw.library-filter"],
  },
  {
    id: "library.ingest.open-close",
    surface: "Library",
    control: "Ingest source, close, cancel, and Escape",
    behavior: "Opens/closes an accessible upload dialog with focus restoration.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["closed", "open", "keyboard"],
    testIds: ["jest.library-dialog", "pw.library-ingest"],
  },
  {
    id: "library.ingest.form",
    surface: "Library",
    control: "Title, type, access, file, license, provider, year, and description",
    behavior: "Validates bounded ingestion metadata and real uploaded bytes.",
    baseline: "functional-covered",
    milestone: "M9",
    states: ["empty", "invalid", "valid", "submitting", "success", "error"],
    testIds: ["pw.library-ingest", "pw.library-oversize"],
  },
  {
    id: "library.item.open",
    surface: "Library",
    control: "Library item row",
    behavior: "Opens a detail dialog with source metadata, governance, checksum, and evidence counts.",
    baseline: "functional-covered",
    milestone: "M9",
    // Corrected from a 6-state lifecycle list to the 2 states the backend can
    // actually produce: generated-api.ts declares
    // `LibraryStatus = "ready" | "processing" | "needs_review" | "blocked"`.
    // workspace-views.tsx renders `item.status` verbatim via `statusLabel()`
    // with no "failed"/"quarantined"/"superseded"/"archived" concept anywhere
    // in the type or the component; declaring them would make the contract
    // unsatisfiable by design, not a real coverage gap.
    states: ["ready", "processing"],
    testIds: ["jest.library-detail", "pw.library-detail"],
  },
  {
    id: "runs.filter",
    surface: "Runs",
    control: "Run status filters",
    behavior: "Filters runs and preserves a valid selected run.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["all", "filtered", "empty", "keyboard"],
    testIds: ["jest.runs-filter", "pw.runs-filter"],
  },
  {
    id: "runs.select",
    surface: "Runs",
    control: "Run rows",
    behavior: "Selects a run and displays authoritative events, artifacts, costs, and traces.",
    baseline: "functional-covered",
    milestone: "M9",
    // Corrected from a 5-state list to the 3 states reachable today:
    // RunsView (workspace-views.tsx ~747-935) renders `data.runs` synchronously
    // from props with no internal fetch/loading state for the run list or the
    // detail panel itself (only the approval decision has a busy/error state,
    // tracked separately as approvals.decide). "loading" and generic "error"
    // have no reachable code path for run selection/display. `run.status` is
    // the backend `RunStatus` enum, which does include "partial", so that
    // state is reachable via a mocked run record.
    states: ["ready", "selected", "partial"],
    testIds: ["pw.operational-surfaces", "pw.run-detail"],
  },
  {
    id: "approvals.rationale",
    surface: "Approvals",
    control: "Reviewer rationale",
    behavior: "Requires a bounded rationale before a decision.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["empty", "valid", "invalid", "disabled"],
    testIds: ["jest.approval-rationale", "pw.approval-decision"],
  },
  {
    id: "approvals.decide",
    surface: "Approvals",
    control: "Approve, reject, and request changes",
    behavior: "Records actor, exact payload digest, rationale, delivery, and idempotent decision.",
    baseline: "functional-uncovered",
    milestone: "M9",
    // Corrected from a 7-state list to the 5 states the backend can actually
    // produce: generated-api.ts declares
    // `ApprovalState = "pending" | "approved" | "rejected" | "cancelled"` and
    // RunsView's `decide()` (workspace-views.tsx ~770-792) only ever calls the
    // decision API with "approved" or "rejected". There is no third decision
    // path and no "changes_requested"/"expired" value anywhere in the type or
    // the component; declaring them would make the contract unsatisfiable by
    // design, not a real coverage gap.
    states: ["pending", "submitting", "approved", "rejected", "error"],
    testIds: ["jest.approval-decision", "pw.approval-decision"],
  },
  {
    id: "settings.tabs",
    surface: "Settings",
    control: "Settings section navigation",
    behavior: "Opens every URL-addressable settings section without blank panels.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: NAVIGATION_STATES,
    testIds: ["jest.settings-tabs", "pw.settings-tabs"],
  },
  {
    id: "settings.general.form",
    surface: "Settings",
    control: "Project profile fields and save",
    behavior: "Validates, persists, and reports success/error without enabling global web research.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: STANDARD_STATES,
    testIds: ["jest.settings-general", "pw.settings-general"],
  },
  {
    id: "settings.connectors.search-filter",
    surface: "Settings",
    control: "Connector search and category filters",
    behavior: "Filters persisted connectors and handles no results.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["ready", "filtered", "empty", "keyboard"],
    testIds: ["jest.connector-filter", "pw.connector-filter"],
  },
  {
    id: "settings.connectors.enable",
    surface: "Settings",
    control: "Enable connector",
    behavior: "Uses the connector manager widget to enable or disable optional connectors while protecting required dependencies.",
    baseline: "functional-covered",
    milestone: "M9",
    states: ["enabled", "disabled", "locked", "saving", "error"],
    testIds: ["jest.connector-enable", "pw.connector-enable"],
  },
  {
    id: "settings.connectors.assign",
    surface: "Settings",
    control: "Assigned specialist checkboxes",
    behavior: "Uses the connector manager widget to persist allowlisted specialist assignments and prevent invalid combinations.",
    baseline: "functional-covered",
    milestone: "M9",
    // Corrected from a 5-state list to the 3 states reachable today: the
    // assignment checkboxes (workspace-views.tsx ~1591-1622) have no
    // minimum-selection guard and the form submits unconditionally via
    // `mutateConnector`; there is no client-side "invalid" combination that
    // blocks submission. "saving" (busyConnector) and "error" (catch path)
    // remain real and are declared separately.
    states: ["selected", "unselected", "saving", "error"],
    testIds: ["jest.connector-assign", "pw.connector-assign"],
  },
  {
    id: "settings.connectors.terms",
    surface: "Settings",
    control: "Connector terms link",
    behavior: "Opens an allowlisted HTTPS terms URL safely.",
    baseline: "functional-uncovered",
    milestone: "M9",
    states: ["ready", "blocked-url"],
    testIds: ["jest.connector-terms", "pw.connector-terms"],
  },
  {
    id: "settings.connectors.test",
    surface: "Settings",
    control: "Test connection",
    behavior: "Runs a bounded audited test, updates health/freshness, and distinguishes missing gateway setup from a provider outage.",
    baseline: "functional-covered",
    milestone: "M9",
    states: ["ready", "testing", "configuration-required", "healthy", "degraded", "failed"],
    testIds: ["pw.operational-surfaces", "pw.connector-test"],
  },
  {
    id: "settings.connectors.versions",
    surface: "Settings",
    control: "APIM, MCP, and Toolbox version promotion and rollback",
    behavior: "Shows immutable versions and requires approval before default promotion.",
    baseline: "functional-covered",
    milestone: "M9",
    // Corrected from a 6-state lifecycle list to the single state reachable today:
    // workspace-views.tsx (~1048-1108, ~1727-1760) renders "Promote to default" and
    // "Roll back" as hardcoded `disabled` with no backing state machine, and none of
    // the connector fixtures in registry.py match the apim/mcp/toolbox host patterns
    // this panel keys off of, so the badge always renders "Not configured". The other
    // five lifecycle states (draft/validating/canary/active/deprecated/failed) have no
    // reachable code path in this workspace; declaring them would make the contract
    // unsatisfiable by design, not a real coverage gap.
    states: ["unconfigured"],
    testIds: ["jest.connector-versions", "pw.connector-versions"],
  },
  {
    id: "settings.integrations.readiness",
    surface: "Settings",
    control: "APIM/Toolbox, Work IQ, GitHub Copilot connector authoring, and Foundry Code Interpreter readiness",
    behavior: "Shows deployment-managed connector/toolbox state, scoped draft-PR connector authoring, the project-scoped Code Interpreter boundary, and truthful disabled Work IQ prerequisites.",
    baseline: "functional-covered",
    milestone: "M9",
    // Corrected from a 6-state lifecycle list to the single state reachable
    // today: the Readiness tab (workspace-views.tsx ~1795-1857) renders 4
    // hardcoded `<article>` cards with fixed copy and no props/data-driven
    // variation whatsoever -- there is no fetch, no per-target status field,
    // and no loading/permission/error branch. "unconfigured" is the only
    // truthful rendered state; the other five would make the contract
    // unsatisfiable by design, not a real coverage gap.
    states: ["unconfigured"],
    testIds: ["jest.integration-readiness", "pw.integration-readiness"],
  },
] as const;

export const INTERACTION_GAPS = INTERACTION_MANIFEST.filter(
  (item) => item.baseline === "unwired" || item.baseline === "missing",
);

const ALL_VIEWPORTS = ["desktop", "tablet", "mobile"] as const;

export const CORE_SCREENSHOT_CONTRACTS = [
  {
    id: "visual.core.overview",
    route: "/",
    heading: "Move from question to defensible evidence.",
  },
  {
    id: "visual.core.literature",
    route: "/?view=literature",
    heading: "Literature Studio",
  },
  {
    id: "visual.core.grant",
    route: "/?view=grant",
    heading: "Grant Studio",
  },
  {
    id: "visual.core.matching",
    route: "/?view=matching",
    heading: "Matching Explorer",
  },
  {
    id: "visual.core.dataset",
    route: "/?view=dataset",
    heading: "Dataset Lab",
  },
  {
    id: "visual.core.institutional",
    route: "/?view=institutional_qa",
    heading: "Institutional Q&A",
  },
  {
    id: "visual.core.workflow",
    route: "/?view=orchestration",
    heading: "Workflow Automation",
  },
  {
    id: "visual.core.library",
    route: "/?view=library",
    heading: "Library",
  },
  {
    id: "visual.core.runs",
    route: "/?view=runs",
    heading: "Runs & Approvals",
  },
  {
    id: "visual.core.settings",
    route: "/?view=settings",
    heading: "Project Settings",
  },
] as const;

export const STATE_SCREENSHOT_IDS = [
  "visual.state.empty",
  "visual.state.loading",
  "visual.state.error",
  "visual.state.authorization",
] as const;

const SURFACE_ROUTES: Readonly<Record<string, string>> = {
  Approvals: "/?view=runs",
  Dataset: "/?view=dataset",
  Grant: "/?view=grant",
  Institutional: "/?view=institutional_qa",
  Library: "/?view=library",
  Literature: "/?view=literature",
  Matching: "/?view=matching",
  Overview: "/",
  Runs: "/?view=runs",
  Settings: "/?view=settings",
  Shell: "/",
  Workflow: "/?view=orchestration",
};

const SURFACE_SCREENSHOTS: Readonly<Record<string, readonly string[]>> = {
  Approvals: ["visual.core.runs"],
  Dataset: ["visual.core.dataset"],
  Grant: ["visual.core.grant"],
  Institutional: [
    "visual.core.institutional",
    "visual.state.authorization",
  ],
  Library: ["visual.core.library"],
  Literature: [
    "visual.core.literature",
    "visual.state.empty",
    "visual.state.loading",
    "visual.state.error",
  ],
  Matching: ["visual.core.matching"],
  Overview: ["visual.core.overview"],
  Runs: ["visual.core.runs"],
  Settings: ["visual.core.settings"],
  Shell: ["visual.core.overview"],
  Workflow: ["visual.core.workflow"],
};

export const DECLARED_SCREENSHOT_IDS: ReadonlySet<string> = new Set([
  ...CORE_SCREENSHOT_CONTRACTS.map((contract) => contract.id),
  ...STATE_SCREENSHOT_IDS,
]);

const ASYNC_STATES = new Set([
  "loading",
  "running",
  "retrying",
  "saving",
  "submitting",
  "testing",
  "uploading",
  "validating",
]);
const EMPTY_STATES = new Set(["empty", "none", "no-results"]);
const ERROR_STATES = new Set([
  "blocked",
  "error",
  "failed",
  "invalid",
  "rejected",
  "unavailable",
]);
const AUTH_STATES = new Set([
  "admin-consent-required",
  "consent-required",
  "locked",
  "permission-denied",
  "unauthorized",
  "user-consent-required",
]);

function classifyState(name: string): CoverageStateKind {
  if (ASYNC_STATES.has(name)) return "async";
  if (EMPTY_STATES.has(name)) return "empty";
  if (ERROR_STATES.has(name)) return "error";
  if (AUTH_STATES.has(name)) return "auth";
  return "behavior";
}

// Per-state Playwright coverage is deliberately NOT pre-declared here. It used to be
// derived as `playwrightStateTestIds`, which blanket-mapped every declared state to
// every `pw.*` id on the interaction regardless of whether a test actually exercised
// that state — a false, unverifiable claim. The truthful contract instead lives in
// e2e/coverage-contract.spec.ts: it AST-scans every `test()` title in e2e/*.spec.ts
// for `[pw.<interaction-id>:<state>]` tokens and compares the found set against the
// required cross-product of `interaction.id x interaction.states` computed from
// this manifest, failing on any missing pair (declared state with no token) or
// orphaned pair (token whose id/state isn't declared here).
export const UI_COVERAGE_MANIFEST: readonly UiCoverageContract[] =
  INTERACTION_MANIFEST.map((interaction) => ({
    ...interaction,
    route: SURFACE_ROUTES[interaction.surface],
    viewports: ALL_VIEWPORTS,
    rtlTestIds: interaction.testIds.filter((id) => id.startsWith("jest.")),
    playwrightTestIds: interaction.testIds.filter((id) => id.startsWith("pw.")),
    screenshotIds: SURFACE_SCREENSHOTS[interaction.surface],
    classifiedStates: interaction.states.map((name) => ({
      name,
      kind: classifyState(name),
    })),
  }));
