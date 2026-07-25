// Standalone, reproducible proof that two *concurrently started* invocations
// of the atomic E2E coverage gate can never clear or overwrite each other's
// report/output directory -- and, for contrast, that the *previous* scheme
// (a shared `outputDir` with only a uniquely named report file inside it)
// genuinely was vulnerable to exactly that.
//
// This does not spin up two real `next build && playwright test` processes
// (prohibitively slow for a repeatable proof); instead it exercises the
// exact same filesystem operation Playwright's own test runner performs on
// `outputDir` at the start of every invocation --
// `fs.rm(outputDir, { recursive: true, force: true })`, verified against
// this repo's installed Playwright version in
// `node_modules/playwright/lib/runner/index.js`'s
// `createRemoveOutputDirsTask` -- against the real path-construction logic
// in `resolveInvocationPaths` (imported directly from
// `run-e2e-coverage-gate.mjs`, so this proof can never silently drift from
// what the gate script actually does).
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { resolveInvocationPaths, stripGateDistDirIncludes } from "./gate-invocation-paths.mjs";

/** Mirrors Playwright's own unconditional `outputDir` wipe at the start of
 * every invocation (`createRemoveOutputDirsTask`, `playwright/lib/runner/index.js`). */
function simulatePlaywrightClearOutputDir(outputDir) {
  rmSync(outputDir, { recursive: true, force: true });
}

function proveFixedSchemeIsolatesConcurrentInvocations(root) {
  const invocationA = resolveInvocationPaths(root, "invocation-A");
  const invocationB = resolveInvocationPaths(root, "invocation-B");

  assert.notStrictEqual(
    invocationA.outputDir,
    invocationB.outputDir,
    "two invocation IDs must resolve to two structurally distinct output directories",
  );

  // Invocation A starts, gets its own outputDir, and (eventually) writes its
  // real JSON report inside it.
  mkdirSync(invocationA.outputDir, { recursive: true });
  writeFileSync(invocationA.reportPath, JSON.stringify({ invocation: "A" }));

  // Invocation B starts *concurrently*. Its own Playwright process performs
  // the exact same "clear output" step Playwright always performs -- but
  // only against *its own* outputDir, since that's the only one it was ever
  // configured with.
  simulatePlaywrightClearOutputDir(invocationB.outputDir);
  mkdirSync(invocationB.outputDir, { recursive: true });
  writeFileSync(invocationB.reportPath, JSON.stringify({ invocation: "B" }));

  assert.ok(
    existsSync(invocationA.reportPath),
    "FAIL: invocation A's report was deleted by invocation B's own output-dir cleanup " +
      "-- the fixed per-invocation outputDir scheme is not actually isolating concurrent invocations",
  );
  assert.deepStrictEqual(
    JSON.parse(readFileSync(invocationA.reportPath, "utf8")),
    { invocation: "A" },
    "invocation A's report content must be exactly what A itself wrote, untouched by B",
  );
  assert.ok(existsSync(invocationB.reportPath), "invocation B's own report must still exist");
}

function proveHtmlReportAndBuildDirsAreIsolated(root) {
  const invocationA = resolveInvocationPaths(root, "invocation-A");
  const invocationB = resolveInvocationPaths(root, "invocation-B");

  // The HTML reporter clears and rewrites its `outputFolder` on every
  // invocation exactly as the test runner clears `outputDir`, so a fixed
  // `playwright-report/` is the same hazard one level over.
  assert.notStrictEqual(
    invocationA.htmlReportDir,
    invocationB.htmlReportDir,
    "two invocation IDs must resolve to two structurally distinct HTML report directories",
  );

  mkdirSync(invocationA.htmlReportDir, { recursive: true });
  const htmlA = path.join(invocationA.htmlReportDir, "index.html");
  writeFileSync(htmlA, "<html>A</html>");

  simulatePlaywrightClearOutputDir(invocationB.htmlReportDir);
  mkdirSync(invocationB.htmlReportDir, { recursive: true });
  writeFileSync(path.join(invocationB.htmlReportDir, "index.html"), "<html>B</html>");

  assert.ok(
    existsSync(htmlA),
    "FAIL: invocation A's HTML report was deleted when invocation B's HTML reporter cleared its own folder",
  );
  assert.strictEqual(
    readFileSync(htmlA, "utf8"),
    "<html>A</html>",
    "invocation A's HTML report content must be exactly what A wrote",
  );

  // Playwright refuses to start when the HTML reporter's output folder lives
  // inside the test output folder ("HTML reporter output folder clashes with
  // the tests output folder"), because the runner's own cleanup would delete
  // the report it had just written. Nesting the HTML directory inside the
  // per-invocation outputDir is the obvious-looking way to keep an
  // invocation's artifacts together, and it is exactly what tripped this --
  // so the constraint is asserted here rather than rediscovered at runtime.
  for (const invocation of [invocationA, invocationB]) {
    assert.ok(
      !path
        .resolve(invocation.htmlReportDir)
        .startsWith(path.resolve(invocation.outputDir) + path.sep),
      "the HTML report directory must not be nested inside the Playwright outputDir",
    );
    assert.notStrictEqual(
      path.resolve(invocation.htmlReportDir),
      path.resolve(invocation.outputDir),
      "the HTML report directory must not be the Playwright outputDir itself",
    );
  }

  // `npm run test:e2e` is `next build && playwright test`, so every gate
  // invocation runs a real build. Two builds into one `.next` fail with
  // "Another next build process is already running" before Playwright starts,
  // which no report-path isolation could address.
  assert.notStrictEqual(
    invocationA.distDir,
    invocationB.distDir,
    "two invocation IDs must resolve to two structurally distinct Next build directories",
  );
  // The build directory must also not sit inside the directory Playwright
  // clears, or the running server's own build would be deleted mid-run.
  assert.ok(
    !path.resolve(root, invocationA.distDir).startsWith(
      path.resolve(invocationA.outputDir) + path.sep,
    ),
    "the Next build directory must not be nested inside the Playwright outputDir that gets cleared",
  );
}

function proveLegacySharedOutputDirSchemeWasVulnerable(root) {
  // Reproduces the *previous* scheme this gate script used: one shared
  // `outputDir` (here, a stand-in for the old fixed `test-results/`) with
  // only a uniquely named report file inside it -- to prove the bug the
  // fixed scheme above closes was real, not hypothetical.
  const sharedOutputDir = path.join(root, "legacy-shared-test-results");
  const reportA = path.join(sharedOutputDir, "report.legacy-invocation-A.json");
  const reportB = path.join(sharedOutputDir, "report.legacy-invocation-B.json");

  mkdirSync(sharedOutputDir, { recursive: true });
  writeFileSync(reportA, JSON.stringify({ invocation: "legacy-A" }));

  // Invocation B's Playwright process clears *the shared* outputDir -- the
  // only one either invocation was ever configured with under the old
  // scheme -- exactly reproducing the reviewer-identified defect.
  simulatePlaywrightClearOutputDir(sharedOutputDir);
  mkdirSync(sharedOutputDir, { recursive: true });
  writeFileSync(reportB, JSON.stringify({ invocation: "legacy-B" }));

  assert.ok(
    !existsSync(reportA),
    "expected the legacy shared-outputDir scheme to have deleted invocation A's report " +
      "when invocation B started -- if this assertion fails, the reproduction of the " +
      "original defect is itself wrong, and the fix above cannot be trusted as a real fix",
  );
  assert.ok(existsSync(reportB), "invocation B's own report must still exist under the legacy scheme");
}

function proveGateDistDirEntriesAreStrippedFromTsconfig() {
  // `next build` rewrites tsconfig.json in place, appending an include entry
  // per distDir it sees. With a per-invocation distDir that silently turns a
  // tracked file into unbounded generated churn -- two more lines per gate
  // run, each naming a directory that no longer exists.
  const polluted = {
    include: [
      "**/*.ts",
      ".next/types/**/*.ts",
      ".next/dev/types/**/*.ts",
      ".next-gate/gate-aaaa/types/**/*.ts",
      ".next-gate/gate-aaaa/dev/types/**/*.ts",
      ".next-gate/gate-bbbb/types/**/*.ts",
    ],
    exclude: ["node_modules"],
  };

  const first = stripGateDistDirIncludes(polluted);
  assert.strictEqual(first.changed, true);
  assert.deepStrictEqual(
    first.config.include,
    ["**/*.ts", ".next/types/**/*.ts", ".next/dev/types/**/*.ts"],
    "every .next-gate entry must be removed and the committed .next entries kept",
  );
  assert.deepStrictEqual(
    first.config.exclude,
    ["node_modules"],
    "unrelated config keys must be preserved verbatim",
  );

  // Idempotent, and reports no change when there is nothing to strip -- this
  // is what makes it safe for two concurrent gates to run it in either order
  // without one undoing or duplicating the other's work.
  const second = stripGateDistDirIncludes(first.config);
  assert.strictEqual(second.changed, false);
  assert.deepStrictEqual(second.config.include, first.config.include);

  // A config with no include list at all must not throw or invent one.
  const withoutInclude = stripGateDistDirIncludes({ compilerOptions: {} });
  assert.strictEqual(withoutInclude.changed, false);
  assert.deepStrictEqual(withoutInclude.config, { compilerOptions: {} });
}

function main() {
  const root = mkdtempSync(path.join(tmpdir(), "gate-isolation-proof-"));
  try {
    proveFixedSchemeIsolatesConcurrentInvocations(root);
    proveHtmlReportAndBuildDirsAreIsolated(root);
    proveGateDistDirEntriesAreStrippedFromTsconfig();
    proveLegacySharedOutputDirSchemeWasVulnerable(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }

  console.log(
    "PASSED: resolveInvocationPaths' per-invocation scheme isolates concurrent " +
      "gate invocations across all four shared artifacts -- Playwright outputDir, " +
      "JSON report, HTML report directory, and Next build directory -- and the " +
      "legacy shared-outputDir scheme was confirmed genuinely vulnerable to the " +
      "exact defect this fix closes.",
  );
}

main();
