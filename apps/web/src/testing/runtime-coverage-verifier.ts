// Dynamic (runtime) counterpart to the static AST scan in
// `e2e/coverage-contract.spec.ts`.
//
// The static scan proves that every required (interaction, state) pair has a
// non-skip-guarded `[pw.<id>:<state>]` token *somewhere in source*. That is
// necessary but not sufficient: a runtime-only skip condition (an env var
// gate resolved only when the process actually runs, a platform check, a
// retry that keeps failing) can make a token look "trusted" by the static
// scan while the covering test never genuinely executes and passes in a
// given run. This module closes that gap by reading the Playwright JSON
// reporter's output for a completed run and requiring every declared
// interaction id / (interaction, state) pair to have at least one execution
// whose final result status is literally "passed". Skipped, failed,
// timed-out, or interrupted executions never satisfy coverage here, even if
// the exact same token would be considered "trusted" by the static scan.
//
// This file has zero import of `interaction-manifest.ts` on purpose: the
// manifest is passed in as a plain parameter so this module (and its logic)
// can be loaded by a standalone Node CLI script (`scripts/verify-playwright-
// runtime-coverage.mjs`) via on-the-fly `ts.transpileModule`, independently
// of the manifest's own transpile, with no relative-import resolution to
// worry about.

/** Bare `[pw.<alias>]` token: an interaction-level claim. Kept identical to
 * the pattern used by the static AST scan so both layers agree on grammar. */
export const PLAYWRIGHT_ID_PATTERN = /\[(pw\.[a-z0-9.-]+)\]/g;

/** `[pw.<interaction-id>:<state>]` token: a per-state claim. Kept identical
 * to the pattern used by the static AST scan so both layers agree on
 * grammar. */
export const PLAYWRIGHT_STATE_TOKEN_PATTERN =
  /\[pw\.([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*):([a-z][a-z0-9-]*)\]/g;

export interface ExtractedTitleTokens {
  bareIds: string[];
  statePairs: string[];
}

/** Extract every bare id and (interaction, state) pair token from a single
 * test title string. Pure string parsing, no I/O. */
export function extractTokensFromTitle(title: string): ExtractedTitleTokens {
  const bareIds: string[] = [];
  for (const match of title.matchAll(PLAYWRIGHT_ID_PATTERN)) {
    bareIds.push(match[1]);
  }
  const statePairs: string[] = [];
  for (const match of title.matchAll(PLAYWRIGHT_STATE_TOKEN_PATTERN)) {
    statePairs.push(`${match[1]}::${match[2]}`);
  }
  return { bareIds, statePairs };
}

// --- Minimal typed subset of the Playwright JSON reporter schema. ---
// (https://playwright.dev/docs/test-reporters#json-reporter) Only the
// fields this module actually reads are declared.

export interface PlaywrightJsonResult {
  status?: string;
}

export interface PlaywrightJsonTest {
  /** The run's *outcome* classification for this test entry, comparing
   * actual vs. expected: `'skipped' | 'expected' | 'unexpected' | 'flaky'`.
   * An `'unexpected'` outcome means the actual final status did NOT match
   * `expectedStatus` (e.g. a `test.fail()`-marked test that unexpectedly
   * passed, or a normal test that unexpectedly failed) and must never be
   * treated as coverage regardless of any individual attempt's status. */
  status?: string;
  /** What Playwright expected this test to resolve to, derived from any
   * `test.fail`/`test.fixme`/`test.skip` annotations:
   * `'passed' | 'failed' | 'timedOut' | 'skipped' | 'interrupted'`. Only
   * `'passed'` represents a test that was expected to (and must) genuinely
   * succeed; a `test.fail()`-marked test has `expectedStatus: 'failed'` even
   * if it happens to pass, and must not satisfy required coverage. */
  expectedStatus?: string;
  /** The Playwright project this specific execution ran under (e.g.
   * `"chromium"`, `"tablet-chromium"`, `"mobile-chromium"`). Used by
   * `validateReportSchema` to prove every configured project genuinely
   * executed at least one test in a given report, not just that the
   * config declares the project. */
  projectName?: string;
  results?: readonly PlaywrightJsonResult[];
}

export interface PlaywrightJsonSpec {
  title: string;
  tests?: readonly PlaywrightJsonTest[];
}

export interface PlaywrightJsonSuite {
  title?: string;
  specs?: readonly PlaywrightJsonSpec[];
  suites?: readonly PlaywrightJsonSuite[];
}

export interface PlaywrightJsonReportStats {
  startTime?: string;
  duration?: number;
  expected?: number;
  unexpected?: number;
  flaky?: number;
  skipped?: number;
}

export interface PlaywrightJsonReportError {
  message?: string;
}

export interface PlaywrightJsonReportProject {
  name?: string;
}

export interface PlaywrightJsonReport {
  suites?: readonly PlaywrightJsonSuite[];
  /** Global (setup/teardown) errors, not per-test failures -- those live
   * inside each test's own `results[].errors`. A non-empty array here means
   * the run itself broke down before/around the tests, and the rest of the
   * report cannot be trusted. */
  errors?: readonly PlaywrightJsonReportError[];
  stats?: PlaywrightJsonReportStats;
  config?: {
    projects?: readonly PlaywrightJsonReportProject[];
  };
}

/** The only two top-level *outcome* values (`PlaywrightJsonTest.status`) that
 * can ever legitimately represent a genuinely-passing execution: `"expected"`
 * (ran, and its final status matched `expectedStatus`) or `"flaky"` (failed
 * on an earlier attempt but the final retry matched `expectedStatus`).
 * `"unexpected"` and `"skipped"` can never mean a genuine pass. Declared as
 * an allowlist -- mirroring `GENUINE_EXECUTION_STATUSES` below, for the same
 * reason -- rather than as a single `!== "unexpected"` exclusion: a bogus,
 * missing, or fabricated `status` value (e.g. a hand-crafted/corrupted
 * report engineered to slip a fake "coverage" past this check) is not
 * literally `"unexpected"` either, so an exclusion-only check would fail
 * open and count it as a pass whenever `expectedStatus`/the last result
 * happened to also look right. Reproduced this exact gap before fixing it:
 * `{ expectedStatus: "passed", status: "some-bogus-value", results: [{
 * status: "passed" }] }` returned `true` under the old exclusion-only logic. */
const PASSING_OUTCOME_STATUSES = new Set(["expected", "flaky"]);

/**
 * Reimplementation of Playwright's own outcome-computation algorithm
 * (`computeTestCaseOutcome` in `playwright/lib/common/index.js`, compiled
 * from `packages/playwright/src/common/test.ts`), operating directly on a
 * test entry's `expectedStatus` and its actual per-attempt `results[]`
 * history:
 *
 * ```
 * for each result:
 *   if result.status === "interrupted": (tracked, does not affect outcome)
 *   else if result.status === "skipped" && expectedStatus === "skipped": skipped += 1
 *   else if result.status === "skipped": (did-not-run, does not affect outcome)
 *   else if result.status === expectedStatus: expected += 1
 *   else: unexpected += 1
 * if expected === 0 && unexpected === 0: "skipped"
 * else if unexpected === 0: "expected"
 * else if expected === 0 && skipped === 0: "unexpected"
 * else: "flaky"
 * ```
 *
 * Used by `outcomeIsInternallyConsistent` below to prove a report's own
 * claimed top-level `status` is exactly what this algorithm would produce
 * from its own `expectedStatus`/`results` -- not merely trusted verbatim.
 * A malformed/missing per-result `status` is deliberately never treated as
 * a match for `expectedStatus` (fail-closed, same rationale as the
 * allowlists above), so it can only ever land in the "unexpected" bucket.
 */
export function computeExpectedOutcome(
  expectedStatus: string | undefined,
  results: readonly PlaywrightJsonResult[],
): "skipped" | "expected" | "unexpected" | "flaky" {
  let skipped = 0;
  let expected = 0;
  let unexpected = 0;
  for (const result of results) {
    const status = result.status;
    if (status === "interrupted") {
      continue;
    } else if (status === "skipped" && expectedStatus === "skipped") {
      skipped += 1;
    } else if (status === "skipped") {
      continue; // "did not run" in upstream terms; never affects the outcome branches.
    } else if (status !== undefined && status === expectedStatus) {
      expected += 1;
    } else {
      unexpected += 1;
    }
  }
  if (expected === 0 && unexpected === 0) return "skipped";
  if (unexpected === 0) return "expected";
  if (expected === 0 && skipped === 0) return "unexpected";
  return "flaky";
}

/**
 * True iff a test entry's own claimed top-level `status` is exactly what
 * `computeExpectedOutcome` would independently derive from its
 * `expectedStatus` and its actual `results[]` history. Closes the gap
 * where a hand-crafted, corrupted, or partially-edited report could claim
 * an outcome its own attempt history could never legitimately produce --
 * reproduced both exact gaps before fixing them:
 *
 *  - `{ expectedStatus: "passed", status: "flaky", results: [{ status:
 *    "passed" }] }` -- a single, already-passing attempt can never
 *    genuinely produce "flaky" (that requires at least one *unexpected*
 *    attempt per the real algorithm above); the real outcome for this
 *    history is "expected".
 *  - `{ expectedStatus: "passed", status: "expected", results: [{ status:
 *    "failed" }, { status: "passed" }] }` -- a genuine fail-then-pass
 *    history always computes to "flaky", never "expected", under the real
 *    algorithm.
 *
 * Also implicitly requires at least one real attempt: `results: []`
 * recomputes to `"skipped"` regardless of any claimed `status`, so a
 * non-`"skipped"` claim backed by zero attempts is rejected here too --
 * closing the "project execution with no attempts" gap in
 * `collectProjectNames`.
 */
/** Normalize a test entry's `results` field to a concrete array, treating a
 * missing/undefined field as "no attempts" (`[]`). Shared by every reader of
 * `testEntry.results` so this fallback is expressed -- and covered -- in
 * exactly one place rather than re-derived redundantly at each call site. */
export function resolveTestResults(
  testEntry: PlaywrightJsonTest,
): readonly PlaywrightJsonResult[] {
  return testEntry.results ?? [];
}

export function outcomeIsInternallyConsistent(
  testEntry: PlaywrightJsonTest,
): boolean {
  const results = resolveTestResults(testEntry);
  return computeExpectedOutcome(testEntry.expectedStatus, results) === testEntry.status;
}

/** A spec "genuinely passed" iff at least one of its per-project test
 * executions:
 *
 * 1. was *expected* to pass (`expectedStatus === "passed"` — excludes any
 *    test annotated `test.fail`/`test.fixme`/`test.skip`, whose expected
 *    status is `"failed"`/`"skipped"` respectively, regardless of what
 *    actually happened at runtime);
 * 2. has a top-level outcome of literally `"expected"` or `"flaky"` (see
 *    `PASSING_OUTCOME_STATUSES` above) — rejects `"unexpected"` (an
 *    "unexpected pass" of a `test.fail()`-marked test — actual `passed` but
 *    expected `failed`, which is not evidence the behavior genuinely
 *    works), `"skipped"`, and any bogus/missing/fabricated status string;
 * 3. has a claimed `status` that is internally consistent with its own
 *    `expectedStatus`/`results` history (see `outcomeIsInternallyConsistent`
 *    above) — rejects a report entry whose claimed outcome its own attempt
 *    history could never legitimately produce (e.g. "flaky" backed by a
 *    single already-passing attempt); and
 * 4. has a final (last-retry) result whose status is literally "passed".
 *
 * A test that is skipped everywhere has no "passed" result at all; a test
 * that failed on every attempt (including exhausted retries) likewise has
 * no "passed" final result. A flaky test that failed then passed on retry
 * still counts (outcome `"flaky"`), since its *final* attempt is what the
 * run actually reports as the outcome. */
export function specPassed(spec: PlaywrightJsonSpec): boolean {
  return (spec.tests ?? []).some((testEntry) => {
    if (testEntry.expectedStatus !== "passed") return false;
    if (!PASSING_OUTCOME_STATUSES.has(testEntry.status ?? "")) return false;
    if (!outcomeIsInternallyConsistent(testEntry)) return false;
    const results = resolveTestResults(testEntry);
    const last = results[results.length - 1];
    return last?.status === "passed";
  });
}

export interface ManifestInteractionShape {
  id: string;
  states: readonly string[];
  playwrightTestIds: readonly string[];
}

export interface RuntimeCoverageResult {
  /** `manifest.length` -- the number of distinct *interaction entries*
   * declared in the manifest (e.g. 77). This is NOT the same number as
   * `requiredIdCount` below: several interaction entries can share one
   * `playwrightTestId` alias (a single spec covering more than one
   * interaction), so `requiredIdCount` (the count of *unique aliases*,
   * e.g. 64) is always <= `interactionCount`. Reporting only
   * `requiredIdCount`/`passedIdCount` without this field risks a reader
   * mistaking "64 aliases all passed" for "64 (of 77) interaction entries
   * covered" -- the two are different denominators and both are disclosed
   * here explicitly so they can never be conflated. */
  interactionCount: number;
  /** Count of unique `playwrightTestId` aliases across all interaction
   * entries (`new Set(manifest.flatMap(i => i.playwrightTestIds)).size`).
   * An alias, not an interaction-entry, denominator -- see `interactionCount`
   * above for why these are reported separately rather than as one number. */
  requiredIdCount: number;
  passedIdCount: number;
  requiredStateCount: number;
  passedStateCount: number;
  missingIds: string[];
  missingStates: string[];
  idsPresentButNeverPassed: string[];
  statesPresentButNeverPassed: string[];
}

/** Recursively collect every spec across a suite tree, tracking whether each
 * one genuinely passed, and fold its title tokens into the running sets. */
function walkSuite(
  suite: PlaywrightJsonSuite,
  allBareIds: Set<string>,
  allStatePairs: Set<string>,
  passedBareIds: Set<string>,
  passedStatePairs: Set<string>,
): void {
  for (const spec of suite.specs ?? []) {
    const passed = specPassed(spec);
    const { bareIds, statePairs } = extractTokensFromTitle(spec.title ?? "");
    for (const id of bareIds) {
      allBareIds.add(id);
      if (passed) passedBareIds.add(id);
    }
    for (const pair of statePairs) {
      allStatePairs.add(pair);
      if (passed) passedStatePairs.add(pair);
    }
  }
  for (const child of suite.suites ?? []) {
    walkSuite(child, allBareIds, allStatePairs, passedBareIds, passedStatePairs);
  }
}

/** Compare a completed Playwright JSON report against the manifest's
 * required ids/states, counting a required token as covered only when it
 * has at least one genuinely passed execution in the report. */
export function computeRuntimeCoverage(
  report: PlaywrightJsonReport,
  manifest: readonly ManifestInteractionShape[],
): RuntimeCoverageResult {
  const requiredIds = new Set(
    manifest.flatMap((interaction) => interaction.playwrightTestIds),
  );
  const requiredStatePairs = new Set(
    manifest.flatMap((interaction) =>
      interaction.states.map((state) => `${interaction.id}::${state}`),
    ),
  );

  const allBareIds = new Set<string>();
  const allStatePairs = new Set<string>();
  const passedBareIds = new Set<string>();
  const passedStatePairs = new Set<string>();

  for (const suite of report.suites ?? []) {
    walkSuite(suite, allBareIds, allStatePairs, passedBareIds, passedStatePairs);
  }

  const missingIds = [...requiredIds]
    .filter((id) => !passedBareIds.has(id))
    .sort();
  const missingStates = [...requiredStatePairs]
    .filter((pair) => !passedStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  // Diagnostic: required tokens that DID appear somewhere in this run's
  // report (so the static scan would call them trusted) but that never
  // actually reached a "passed" final result -- exactly the runtime-only
  // loophole this module exists to catch. Already folded into
  // missingIds/missingStates above; surfaced separately for diagnosability.
  const idsPresentButNeverPassed = [...requiredIds]
    .filter((id) => allBareIds.has(id) && !passedBareIds.has(id))
    .sort();
  const statesPresentButNeverPassed = [...requiredStatePairs]
    .filter((pair) => allStatePairs.has(pair) && !passedStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  return {
    interactionCount: manifest.length,
    requiredIdCount: requiredIds.size,
    passedIdCount: [...requiredIds].filter((id) => passedBareIds.has(id))
      .length,
    requiredStateCount: requiredStatePairs.size,
    passedStateCount: [...requiredStatePairs].filter((pair) =>
      passedStatePairs.has(pair),
    ).length,
    missingIds,
    missingStates,
    idsPresentButNeverPassed,
    statesPresentButNeverPassed,
  };
}

/** The complete set of *outcome* values Playwright's JSON reporter can
 * genuinely produce for a test entry's top-level `status` field
 * (https://playwright.dev/docs/test-reporters#json-reporter): `"skipped"`
 * for a test that never ran, and `"expected"` / `"unexpected"` / `"flaky"`
 * for one that did. Anything else -- `undefined`, an empty string, or any
 * value not in this set -- is not a status this run's reporter could ever
 * legitimately produce, and must never be treated as evidence a project
 * genuinely executed. */
const GENUINE_EXECUTION_STATUSES = new Set(["expected", "unexpected", "flaky"]);

/** Collect every distinct `projectName` that appears on any test entry
 * anywhere in a suite subtree whose *outcome* is one of the known
 * genuinely-executed values (`"expected"` / `"unexpected"` / `"flaky"`) --
 * i.e. at least one execution that genuinely ran (passed, failed, timed
 * out, or was flaky), not merely present in the report as a skipped
 * placeholder. Used by `validateReportSchema` to prove a project genuinely
 * *executed* at least one test in this specific report -- not merely that
 * the report's `config.projects` declares the project, nor that a
 * project's every test happened to be skipped (e.g. a tablet/mobile
 * project that is configured but whose entire suite is runtime-skipped
 * would previously have counted as "executed" here). Deliberately an
 * allowlist rather than `!== "skipped"`: a test entry with a missing,
 * empty, or malformed `status` field (e.g. a hand-crafted/fabricated
 * report, or a truncated write) is not literally `"skipped"` either, so a
 * bare exclusion-based check would have silently counted it as genuine
 * execution -- failing open on exactly the kind of corrupted/fabricated
 * report this schema validation exists to catch. Only a recognized,
 * genuinely-executed status can count. */
function collectProjectNames(suite: PlaywrightJsonSuite, into: Set<string>): void {
  for (const spec of suite.specs ?? []) {
    for (const testEntry of spec.tests ?? []) {
      if (!testEntry.projectName) continue;
      if (!GENUINE_EXECUTION_STATUSES.has(testEntry.status ?? "")) continue;
      // A claimed "expected"/"unexpected"/"flaky" outcome is not enough on
      // its own: also require that outcome to be exactly what Playwright's
      // own algorithm would derive from this entry's actual
      // `expectedStatus`/`results[]` history (see
      // `outcomeIsInternallyConsistent`). Closes the gap where a
      // hand-crafted entry claiming e.g. `status: "expected"` with zero
      // attempts (`results: []`) -- which can only ever genuinely
      // recompute to `"skipped"` -- would otherwise count as this project
      // having "genuinely executed" a test despite there being no actual
      // attempt anywhere in the report.
      if (!outcomeIsInternallyConsistent(testEntry)) continue;
      into.add(testEntry.projectName);
    }
  }
  for (const child of suite.suites ?? []) {
    collectProjectNames(child, into);
  }
}

/** Per-outcome counts of individual `PlaywrightJsonTest` entries (each
 * entry representing one project's execution of one spec), recomputed by
 * walking every suite/spec in a report -- for cross-checking against the
 * report's own top-level `stats` block in `validateReportSchema`. A
 * malformed/missing/unrecognized per-entry `status` deliberately falls
 * into none of the four buckets (rather than being coerced into one),
 * since it can never genuinely match any of them; any such entry
 * necessarily makes the recomputed total lower than an internally
 * consistent report's own `stats` total, which the mismatch check below
 * surfaces regardless of which specific bucket disagrees. */
interface OutcomeTally {
  expected: number;
  unexpected: number;
  flaky: number;
  skipped: number;
}

function tallyReportedOutcomes(suite: PlaywrightJsonSuite, tally: OutcomeTally): void {
  for (const spec of suite.specs ?? []) {
    for (const testEntry of spec.tests ?? []) {
      switch (testEntry.status) {
        case "expected":
          tally.expected += 1;
          break;
        case "unexpected":
          tally.unexpected += 1;
          break;
        case "flaky":
          tally.flaky += 1;
          break;
        case "skipped":
          tally.skipped += 1;
          break;
        default:
          break;
      }
    }
  }
  for (const child of suite.suites ?? []) {
    tallyReportedOutcomes(child, tally);
  }
}

/** Validate that a Playwright JSON report is structurally complete enough
 * to trust for coverage verification at all, closing the gap where a
 * stale, minimal, or fabricated report (e.g. hand-crafted with just enough
 * shape to make `computeRuntimeCoverage` report success, or a genuine but
 * partial Chromium-only run) could otherwise satisfy the coverage check.
 * Returns an array of human-readable problem descriptions; an empty array
 * means the report passed schema validation. Deliberately strict and
 * fails closed: anything ambiguous or missing is reported as a problem
 * rather than silently ignored. */
export function validateReportSchema(
  report: PlaywrightJsonReport,
  requiredProjectNames: readonly string[],
): string[] {
  const problems: string[] = [];

  if (typeof report !== "object" || report === null) {
    return ["Report is not a JSON object."];
  }

  const stats = report.stats;
  if (
    !stats ||
    typeof stats.expected !== "number" ||
    typeof stats.unexpected !== "number" ||
    typeof stats.flaky !== "number" ||
    typeof stats.skipped !== "number"
  ) {
    problems.push(
      "Report is missing a complete `stats` block (numeric expected/unexpected/flaky/skipped counts) -- looks stale, minimal, or fabricated.",
    );
  } else {
    // Reconcile the report's own top-level aggregate counts against an
    // independent recount of every individual test entry's status across
    // the whole suite tree. A genuine, untampered report always agrees
    // with itself here; a stale report spliced with new suites/specs, a
    // partially hand-edited report, or one with a corrupted/truncated
    // suite tree will not.
    const tally: OutcomeTally = { expected: 0, unexpected: 0, flaky: 0, skipped: 0 };
    for (const suite of report.suites ?? []) {
      tallyReportedOutcomes(suite, tally);
    }
    const mismatches: string[] = [];
    if (tally.expected !== stats.expected) {
      mismatches.push(`expected: header ${stats.expected} vs recounted ${tally.expected}`);
    }
    if (tally.unexpected !== stats.unexpected) {
      mismatches.push(`unexpected: header ${stats.unexpected} vs recounted ${tally.unexpected}`);
    }
    if (tally.flaky !== stats.flaky) {
      mismatches.push(`flaky: header ${stats.flaky} vs recounted ${tally.flaky}`);
    }
    if (tally.skipped !== stats.skipped) {
      mismatches.push(`skipped: header ${stats.skipped} vs recounted ${tally.skipped}`);
    }
    if (mismatches.length > 0) {
      problems.push(
        "Report's top-level `stats` block does not match an independent recount of every " +
          `individual test entry's status across all suites (${mismatches.join("; ")}) -- ` +
          "the report is internally inconsistent (stale, partially edited, or fabricated) " +
          "and cannot be trusted.",
      );
    }
  }

  if (!Array.isArray(report.suites) || report.suites.length === 0) {
    problems.push(
      "Report has no top-level suites -- no tests appear to have run at all.",
    );
  }

  if (!Array.isArray(report.errors)) {
    problems.push("Report is missing the top-level `errors` array.");
  } else if (report.errors.length > 0) {
    problems.push(
      `Report contains ${report.errors.length} global setup/teardown error(s), so the rest of the run cannot be trusted: ` +
        report.errors.map((error) => error.message ?? "(no message)").join("; "),
    );
  }

  const configuredProjectNames = new Set(
    (report.config?.projects ?? [])
      .map((project) => project.name)
      .filter((name): name is string => Boolean(name)),
  );
  const executedProjectNames = new Set<string>();
  for (const suite of report.suites ?? []) {
    collectProjectNames(suite, executedProjectNames);
  }

  for (const required of requiredProjectNames) {
    if (!configuredProjectNames.has(required)) {
      problems.push(
        `Report's config.projects does not include the required project "${required}" -- report may be stale or from a different/reduced config.`,
      );
    }
    if (!executedProjectNames.has(required)) {
      problems.push(
        `No test in the report actually executed (with a genuinely non-skipped outcome) under the required project "${required}" -- a partial (e.g. Chromium-only) run, or a project whose entire suite was runtime-skipped, cannot satisfy the full release gate.`,
      );
    }
  }

  return problems;
}
