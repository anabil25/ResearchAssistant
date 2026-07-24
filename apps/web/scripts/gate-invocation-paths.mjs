// Pure, dependency-free path-construction logic shared between
// `run-e2e-coverage-gate.mjs` (the real atomic release-gate wrapper) and
// `prove-concurrent-gate-report-isolation.mjs` (a standalone proof that
// concurrent invocations cannot clear or overwrite each other's
// report/output directory). Deliberately kept in its own module with no
// other imports and no top-level side effects, so it can be imported by the
// proof script without triggering `run-e2e-coverage-gate.mjs`'s own
// `main()` (which spawns a real `npm run test:e2e`).
import path from "node:path";

/**
 * Compute an invocation's own, exclusive artifacts directory and JSON
 * report path from an injected invocation ID (pure/deterministic given the
 * ID, so it can be exercised directly by a standalone concurrency proof
 * without depending on `crypto.randomUUID()`'s real randomness).
 *
 * The report path previously lived at a fixed *parent* directory
 * (`test-results/report.<uuid>.json`) that was itself the Playwright
 * `outputDir` every invocation shared. A unique filename alone did not
 * protect it: Playwright's own test-runner unconditionally removes its
 * entire configured `outputDir` (recursively, `fs.rm(outputDir, {
 * recursive: true, force: true }))`) as its first "clear output" step of
 * *every* invocation (see `playwright/lib/runner/index.js`'s
 * `createRemoveOutputDirsTask`), unless `preserveOutputDir` is set. A
 * second, concurrently-started invocation's own startup would therefore
 * wholesale delete the shared directory -- including a first invocation's
 * already-written, uniquely-named report file sitting inside it -- well
 * before that first invocation's gate ever got a chance to read it back.
 *
 * The fix gives each invocation its own *directory* (not just its own
 * filename) nested under `test-results/`, and that directory -- not the
 * shared `test-results/` parent -- is what gets passed to Playwright as
 * `outputDir` (via `PLAYWRIGHT_OUTPUT_DIR`, read by `playwright.config.ts`).
 * Two concurrent invocations then each remove only their own,
 * structurally distinct, never-shared directory; neither can ever observe
 * or clear the other's.
 */
export function resolveInvocationPaths(rootDir, invocationId) {
  const outputDir = path.join(rootDir, "test-results", `gate-${invocationId}`);
  return {
    outputDir,
    reportPath: path.join(outputDir, "report.json"),
    // Playwright's HTML reporter clears and rewrites its `outputFolder` on
    // every invocation exactly as the test runner does for `outputDir`, so a
    // fixed `playwright-report/` is the same hazard one level over: two
    // concurrent invocations would each wipe the other's HTML report, and the
    // surviving one would be an unattributable mix. Deliberately a *sibling*
    // tree rather than nested inside `outputDir`: Playwright rejects an HTML
    // output folder that lives inside the test output folder ("HTML reporter
    // output folder clashes with the tests output folder"), because the
    // runner's own cleanup would delete the report it just wrote.
    htmlReportDir: path.join(rootDir, "playwright-report", `gate-${invocationId}`),
    // `npm run test:e2e` is `next build && playwright test`, so the build is
    // part of every gate invocation. Two simultaneous invocations in one
    // checkout both run `next build` into the shared `.next` and the second
    // dies with "Another next build process is already running" -- a failure
    // no amount of report-path isolation could address, because it happens
    // before Playwright starts. Kept outside `test-results/` (which the test
    // runner clears) and under a stable `.next-gate/` parent so the build
    // cache directories are easy to find and prune.
    distDir: path.join(".next-gate", `gate-${invocationId}`),
  };
}

/** Prefix every gate-created Next build directory shares. */
export const GATE_DIST_DIR_PREFIX = ".next-gate/";

/**
 * Remove gate-created build directories from a `tsconfig.json`'s `include`
 * list, returning the cleaned config plus whether anything changed.
 *
 * `next build` rewrites `tsconfig.json` in place, appending
 * `<distDir>/types/**\/*.ts` and `<distDir>/dev/types/**\/*.ts` whenever it
 * does not already find them. That is harmless with a single fixed `.next`
 * (the entries are committed once and stay), but the per-invocation `distDir`
 * that makes concurrent gates possible turns it into unbounded pollution of a
 * *tracked* file: every gate invocation permanently appends two more entries
 * naming a directory that no longer exists, so a clean checkout is dirty
 * after one gate run and grows by two lines per run thereafter.
 *
 * Stripping by prefix rather than restoring a snapshot is deliberate. It is
 * idempotent and purely content-based, so two concurrent gates finishing at
 * the same moment converge on the same result no matter who writes last,
 * whereas a snapshot taken by the second gate would already contain the
 * first's additions and restore them. The entries are only ever consumed by
 * `tsc`, and Next re-adds whatever a later build needs, so removing them
 * after the run costs nothing. The committed `.next/types/**\/*.ts` entry is
 * untouched.
 */
export function stripGateDistDirIncludes(config) {
  const include = config?.include;
  if (!Array.isArray(include)) {
    return { config, changed: false };
  }
  const cleaned = include.filter(
    (entry) =>
      typeof entry !== "string" ||
      !entry.split(path.sep).join("/").startsWith(GATE_DIST_DIR_PREFIX),
  );
  if (cleaned.length === include.length) {
    return { config, changed: false };
  }
  return { config: { ...config, include: cleaned }, changed: true };
}
