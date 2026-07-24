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
  };
}
