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
    id: "studio.run-evidence",
    surface: "Studio",
    control: "Inline run evidence",
    behavior: "Renders run provenance, resolved sources, and the hosted-agent boundary beside the artifact once a run resolves.",
    baseline: "functional-uncovered",
    milestone: "M1",
    // Purely presentational -- no focusable control, no viewport-dependent
    // DOM -- so "keyboard" and "mobile" would be unprovable claims.
    states: ["ready", "empty"],
    testIds: ["pw.run-evidence", "jest.run-evidence"],
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
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx date-window inputs are plain always-enabled
    // <input type="number"> fields with no `disabled` prop and no async read/write
    // path, so disabled/loading are not reachable. Real `dateWindowError` validation
    // (~L306-319) computes a field-level error whenever "Published from" is after
    // "Through", either field is empty/unparseable, or either bound is a future
    // year — rendered via `#literature-date-window-error` (role="alert") and wired
    // to `aria-invalid`/`aria-describedby` on both inputs, plus a disabled RunButton
    // and an early-return submit guard. "success" has no code path distinct from
    // typing (no secondary import/apply action for this control).
    states: ["ready", "keyboard", "invalid"],
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
    // Evidence: studio-components.tsx `<RunButton running={running}
    // disabled={!!dateWindowError}>` — the submit button is disabled either
    // while `running` (the "loading" state) or whenever the sibling
    // date-window control's validation is failing (a distinct, independently
    // reachable "disabled" precondition, not merely `running`).
    states: ["ready", "keyboard", "loading", "disabled", "success", "error"],
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
    behavior: "Shows a truthful pass/warning/not-verified audit outcome plus resolved/unresolved citation status and excluded-record reasons for the current run.",
    baseline: "functional-covered",
    milestone: "M3",
    // Evidence: studio-components.tsx ~L313-330, L788-800 — a computed
    // `auditStatus` is rendered as a visible outcome paragraph
    // (`.audit-outcome`, `data-audit-status`), not just the static "Claim &
    // citation audit" header: "not-verified" when `literature.insight` is
    // absent (e.g. execution_mode "mock", or a hosted run with no insight —
    // the audit has not actually checked anything), "passed" only when
    // `insight` is present AND zero unresolved source ids, and "warning"
    // when `insight` is present with 1+ unresolved source ids. No
    // per-tab loading skeleton, and no "blocked"/"error" branch distinct
    // from the studio-level StudioError.
    states: ["empty", "not-verified", "passed", "warning"],
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
    // Evidence: studio-components.tsx ~L1103-1132 — funding connector
    // checkboxes are now gated through the same
    // `src/lib/connector-availability.ts` mapping used by Matching's source
    // list: only "ready" connectors (test_status ready/ready_with_key) are
    // selectable/checked and included in the shared `sources` request key
    // (the same key Literature/Matching use, so the server-side
    // `retrieve_public_metadata` readiness gate applies uniformly) at
    // submit; a
    // "needs-connection" (configuration_required), "unavailable"
    // (unavailable), or "disabled" (enabled === false) connector renders a
    // distinct caption and is unselectable, and is filtered out of the
    // submitted payload even if present in the default selection. The
    // "empty" state is the "no connectors assigned" message; there is still
    // no async/error path tied to the toggle itself.
    states: [
      "ready",
      "selected",
      "needs-connection",
      "unavailable",
      "disabled",
      "empty",
    ],
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
    // Evidence: studio-components.tsx ~L1342-1346 — `factsConfirmed` is a
    // plain useState<boolean> bound to an always-enabled checkbox's onChange;
    // there is no disabled/loading/error prop, no async read/write path, and
    // no permission/authorization gating in the frontend or the
    // GrantStudioResult schema, so "error" (like "permission-denied") has no
    // reachable code path for this control specifically. A downstream
    // package-build request failing after the checkbox is checked exercises
    // grant.package.build's own error state, not this one -- the checkbox
    // itself never transitions to any error/disabled rendering.
    states: ["unchecked", "checked"],
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
    // Evidence: studio-components.tsx ~L1526-1660 — public source checkboxes
    // are gated through `src/lib/connector-availability.ts`'s
    // `connectorAvailability()`, which truthfully maps the real backend
    // `enabled` + `test_status` fields to 4 distinct, mutually exclusive
    // categories: "ready" (test_status ready/ready_with_key, selectable and
    // sent as a runnable source), "needs-connection" (test_status
    // configuration_required — provider never configured/authorized),
    // "unavailable" (test_status unavailable — latest probe failed), and
    // "disabled" (enabled === false, wins regardless of test_status). Only
    // "ready" connectors are selectable/checked; the other 3 render a
    // distinct caption and are `disabled` on the checkbox. `sources` is
    // filtered to `runnableSources` before submission, so a non-ready
    // connector can never reach the run payload even via a stale default
    // selection. "consent-required" is the permanently `checked={false}
    // disabled` Work IQ toggle with its Microsoft Graph consent note. There
    // is no error variable tied to source selection itself (only the
    // studio-level StudioError), so "error" is trimmed.
    states: [
      "ready",
      "selected",
      "needs-connection",
      "unavailable",
      "disabled",
      "consent-required",
    ],
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
    // Evidence: studio-components.tsx ~L1989-1991 (runDisabled formula), ~L2068
    // (RunButton running/disabled), ~L2001 (status-chip renders
    // `run.status.replaceAll("_", " ")` verbatim), ~L2288-2298
    // (compute_proposal.approval_required lock banner vs. local-compute banner).
    // "running" is the same run() in-flight moment as dataset.profile:loading;
    // "failed" is the same onRun() rejection moment as dataset.profile:error.
    // "waiting-for-approval" and "blocked" are genuinely distinct, non-overlapping
    // gates -- confirmed by the backend's own `RunStatus` union in
    // generated-api.ts, which declares "waiting_for_approval" and "blocked" as
    // two separate literal values, not synonyms:
    // "waiting-for-approval" covers TWO resolvable moments that are both
    // fixtures for this same state (neither is a hard stop): (a) the pre-submit
    // RunButton-disabled moment caused specifically by `!planApproved` with the
    // asset otherwise ready to run (v3-gap-closing.spec.ts), and (b) the later
    // "Human approval required before submit" banner for large/estimate-required
    // assets, backed by `run.status: "waiting_for_approval"` and
    // compute_proposal.approval_required === true even after planApproved was
    // checked and the run attempted (matching-dataset-state-closure.spec.ts).
    // "blocked" is a genuinely different, non-resolvable-by-approval condition:
    // the backend literally returns `run.status: "blocked"` (e.g. a
    // data-governance policy denial) with compute_proposal.approval_required
    // false -- no local checkbox or human reviewer can clear it -- and is
    // rendered verbatim in the status-chip plus the asset's profile note
    // (matching-dataset-state-closure.spec.ts).
    states: [
      "waiting-for-approval",
      "running",
      "completed",
      "failed",
      "blocked",
    ],
    testIds: ["jest.dataset-plan", "pw.dataset-upload"],
  },
  {
    id: "workflow.template",
    surface: "Workflow",
    control: "Workflow template cards",
    behavior: "Selects and loads an editable versioned graph.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx AUTOMATION_TEMPLATES — a fixed 3-item
    // array of real, distinct step graphs rendered with plain onClick
    // handlers; there is no async fetch or per-template error concept, so
    // loading/error are trimmed.
    states: ["ready", "selected"],
    testIds: ["jest.workflow-template", "pw.workflow-template"],
  },
  {
    id: "workflow.trigger",
    surface: "Workflow",
    control: "Trigger selector",
    behavior: "Configures a typed manual, schedule, webhook, GitHub, or library trigger.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx — a plain <select> with Manual, Schedule,
    // Webhook, GitHub, and Library upload options and no `disabled` prop and no
    // async read/write path tied to selection, so disabled/loading/error are
    // trimmed; native keyboard selection remains reachable and distinct from a
    // pointer click.
    states: ["ready", "selected", "keyboard"],
    testIds: ["jest.workflow-trigger", "pw.workflow-trigger"],
  },
  {
    id: "workflow.catalog",
    surface: "Workflow",
    control: "Agent, tool, and studio catalog",
    behavior: "Adds only authorized versioned capabilities to the graph.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx (buildCatalogItems) — the Studio group is
    // always sourced from the hardcoded, non-empty AUTOMATION_STUDIO_CATALOG,
    // so catalogItems can never be length 0 and "empty" is trimmed. There is no
    // error variable for the catalog itself (only the studio-level
    // StudioError), so "error" is trimmed too. "loading" is `data === null ||
    // data === undefined` and "unauthorized" is now driven by
    // `connector.enabled && test_status === "ready" &&
    // assigned_agents.includes("orchestration")` (real `data.connectors` mock
    // values) — both remain reachable and testable.
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
    // Evidence: studio-components.tsx StepDraftForm — the commit button is
    // `disabled={isNew ? !draft.label.trim() : false}`, so "invalid" (an empty
    // label blocking the Add commit) is a real, reachable state. saveEdit/addStep
    // are synchronous local setState calls with no async period and no error
    // variable, so "saving" and "error" are trimmed.
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
    behavior: "Disabled until a dry run passes with zero graph errors against the exact current draft: the pass is matched by a fingerprint derived from the server's own echoed template_id/trigger/steps (not the client-submitted values) and by draft version, so any step/trigger/template edit, or cloning into a new draft version with identical content, immediately invalidates it and requires a fresh passing dry run before an explicit confirmation dialog can record activation.",
    baseline: "functional-covered",
    milestone: "M8",
    // No "rejected" state: studio-components.tsx's activation confirmation
    // dialog only exposes Cancel (calls setActivationConfirmOpen(false),
    // simply closing the dialog with no persisted outcome) and Confirm
    // activation (rechecks canActivate before recording activation). There
    // is no code path that produces an outcome distinct from the dialog
    // never having been opened, so "rejected" was a structurally impossible
    // state and has been removed rather than credited by a Cancel-click test.
    states: ["disabled", "ready", "waiting-for-approval", "active"],
    testIds: ["jest.workflow-activation", "pw.workflow-activation"],
  },
  {
    id: "workflow.run.manage",
    surface: "Workflow",
    control: "Run status, inspect, clone, and unavailable lifecycle controls",
    behavior: "Displays API-backed run states, links inspection, clones a new draft, and keeps lifecycle actions disabled in direct-execution mode.",
    baseline: "functional-covered",
    milestone: "M8",
    // Evidence: studio-components.tsx — the Pause/Resume/Retry/Cancel buttons
    // are permanently `disabled` in direct-execution mode, and generated-api.ts
    // RunStatus has 8 real values (planned,
    // running, waiting_for_approval, partial, blocked, completed, cancelled,
    // failed), all reachable via `orchestrationRuns` (mockable through the
    // `data` prop). "paused"/"retrying" have no reachable code path and were
    // never real RunStatus values.
    states: [
      "planned",
      "running",
      "waiting-for-approval",
      "partial",
      "blocked",
      "completed",
      "cancelled",
      "failed",
    ],
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
    // Corrected from a 6-state lifecycle list to the 4 states the backend can
    // actually produce: generated-api.ts declares
    // `LibraryStatus = "ready" | "processing" | "needs_review" | "blocked"`.
    // workspace-views.tsx renders `item.status` verbatim via `statusLabel()`
    // (row ~500, detail dialog ~570) with no "failed"/"quarantined"/
    // "superseded"/"archived" concept anywhere in the type or the component --
    // declaring those would make the contract unsatisfiable by design, not a
    // real coverage gap. "needs_review" and "blocked" ARE real, distinct
    // `LibraryStatus` literals rendered identically to "ready"/"processing"
    // and are just as reachable via a mocked item; omitting them was itself
    // an under-claim.
    states: ["ready", "processing", "needs-review", "blocked"],
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
    // RunsView (workspace-views.tsx ~756-935) renders `data.runs` synchronously
    // from props with no internal fetch/loading state for the run list or the
    // detail panel itself (only the approval decision has a busy/error state,
    // tracked separately as approvals.decide). "loading" and generic "error"
    // have no reachable code path for run selection/display. `run.status`
    // (~868-888) is the backend `RunStatus` enum rendered verbatim through the
    // identical `<em className={`table-status ${run.status}`}>{statusLabel(
    // run.status)}</em>` expression for every row and for the selected-run
    // overview -- there is no status-specific branch, so all 8 real
    // generated-api.ts RunStatus literals (planned, running,
    // waiting_for_approval, partial, blocked, completed, cancelled, failed)
    // are equally reachable via a mocked run record, not just a subset.
    // "waiting-for-approval" is the kebab state-token spelling of that
    // literal (see dataset.execution for the convention); the other 7
    // literals contain no underscore and are used as-is.
    states: [
      "ready",
      "selected",
      "planned",
      "running",
      "waiting-for-approval",
      "partial",
      "blocked",
      "completed",
      "cancelled",
      "failed",
    ],
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
    control: "Approve and reject",
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
    // design, not a real coverage gap. The previously-divergent "authoritative"
    // packages/research_core/.../v3_contracts.py (ApprovalRequestV3/
    // ApprovalDecisionV3) has since been reconciled to this same
    // pending/approved/rejected/cancelled + approved/rejected shape, so every
    // Python/generated contract now agrees with this manifest instead of one
    // silently overclaiming.
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
    // workspace-views.tsx renders the submit button disabled only while the PUT
    // is in flight (the visible label is "Saving…"), so "disabled" was not an
    // independently reachable state. research-workbench.tsx surfaces initial
    // settings load failures, including an API 401, before data is available.
    states: [
      "ready",
      "keyboard",
      "loading",
      "saving",
      "success",
      "error",
      "unauthorized",
    ],
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
    // Corrected from an earlier, mistaken "no client-side guard" note: the
    // assignment checkboxes (workspace-views.tsx ~1127-1134) DO block
    // submission client-side — an enabled connector with zero assigned
    // specialists sets a status-only error and returns before calling
    // `updateConnector`, so no PUT is ever sent. "invalid" is a real,
    // independently reachable state alongside "saving" (busyConnector) and
    // "error" (catch path).
    states: ["selected", "unselected", "saving", "error", "invalid"],
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
    // workspace-views.tsx renders four static readiness cards with no request or
    // state machine: APIM/Toolbox is deployment-managed, Work IQ needs tenant
    // consent, Copilot authoring is blocked on repository setup, and the dataset
    // Toolbox is ready. The prior partial/permission-denied/unsupported/error
    // states had no production branch and therefore could never be exercised.
    states: ["deployment-managed", "needs-consent", "blocked", "ready"],
    testIds: ["jest.integration-readiness", "pw.integration-readiness"],
  },
  {
    id: "settings.evaluations.release",
    surface: "Settings",
    control: "Release evaluation gates",
    behavior:
      "Shows each independent deterministic release dimension as ready, blocked, or degraded without collapsing scores.",
    baseline: "functional-covered",
    milestone: "M9",
    states: ["ready", "blocked", "degraded"],
    testIds: ["jest.evaluation-readiness", "pw.evaluation-readiness"],
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
  // Rendered by all six studios, but only proven at the literature route --
  // claiming six routes here would assert coverage no test provides.
  Studio: "/?view=literature",
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
  Studio: ["visual.core.literature"],
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
  "needs-consent",
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
/**
 * Interactions whose DOM or interaction model genuinely differs by
 * breakpoint, and which therefore require evidence from *every* declared
 * viewport rather than desktop alone.
 *
 * Derived from this app's actual media queries in `src/app/globals.css`, not
 * from intuition. Against the configured viewport widths (desktop 1440,
 * tablet 834, mobile 390):
 *
 *  - `@media (max-width: 900px)` restyles `.project-rail`,
 *    `.mobile-menu-button`, `.mobile-scrim`, and `.topbar` -- the navigation
 *    rail becomes a drawer opened by a button that does not exist at desktop
 *    width. Applies to tablet (834) and mobile.
 *  - `@media (max-width: 680px)` further restyles `.topbar` and its
 *    controls. Applies to mobile only.
 *
 * Everything else the remaining breakpoints touch (`.workspace-main`,
 * `.institutional-grid`, `.dataset-grid`, `.capability-grid`, and similar)
 * is pure layout reflow: the same elements, same roles, same handlers,
 * rearranged. Those interactions are declared desktop-only because that is
 * where they are actually proven, and claiming more would be the exact
 * overstatement this classification exists to remove.
 *
 * This replaced a blanket `viewports: ALL_VIEWPORTS` applied to every
 * interaction, which asserted that all 77 interactions and all 298 states
 * were covered at desktop, tablet and mobile. Runtime evidence showed tablet
 * and mobile proving three states each, so that claim was not merely
 * unverified -- it was false, and it inflated what the 298 denominator
 * appeared to mean.
 */
const VIEWPORT_SENSITIVE_INTERACTION_IDS: ReadonlySet<string> = new Set([
  "shell.navigation.primary-routes",
  "shell.navigation.open-mobile",
  "shell.navigation.close-mobile",
]);

const DESKTOP_ONLY_VIEWPORTS = ["desktop"] as const;

export function viewportsForInteraction(
  interactionId: string,
): readonly CoverageViewport[] {
  return VIEWPORT_SENSITIVE_INTERACTION_IDS.has(interactionId)
    ? ALL_VIEWPORTS
    : DESKTOP_ONLY_VIEWPORTS;
}

export const UI_COVERAGE_MANIFEST: readonly UiCoverageContract[] =
  INTERACTION_MANIFEST.map((interaction) => ({
    ...interaction,
    route: SURFACE_ROUTES[interaction.surface],
    viewports: viewportsForInteraction(interaction.id),
    rtlTestIds: interaction.testIds.filter((id) => id.startsWith("jest.")),
    playwrightTestIds: interaction.testIds.filter((id) => id.startsWith("pw.")),
    screenshotIds: SURFACE_SCREENSHOTS[interaction.surface],
    classifiedStates: interaction.states.map((name) => ({
      name,
      kind: classifyState(name),
    })),
  }));
