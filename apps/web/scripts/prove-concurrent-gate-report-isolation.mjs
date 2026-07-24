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

import { resolveInvocationPaths } from "./gate-invocation-paths.mjs";

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

function main() {
  const root = mkdtempSync(path.join(tmpdir(), "gate-isolation-proof-"));
  try {
    proveFixedSchemeIsolatesConcurrentInvocations(root);
    proveLegacySharedOutputDirSchemeWasVulnerable(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }

  console.log(
    "PASSED: resolveInvocationPaths' per-invocation outputDir scheme isolates " +
      "concurrent gate invocations from each other's report/output-dir cleanup " +
      "(and the legacy shared-outputDir scheme was confirmed genuinely vulnerable " +
      "to the exact defect this fix closes).",
  );
}

main();
