// Standalone dynamic post-run verifier: proves the Playwright JSON report
// from an actual completed run genuinely satisfies the (interaction, state)
// coverage contract, closing the gap left by the purely static AST scan in
// `e2e/coverage-contract.spec.ts`. A required token can be "trusted" by the
// static scan (right token text, not skip-guarded in source) yet still never
// pass at runtime -- a flaky/always-failing test, a runtime-only env-gated
// skip, an interrupted worker, an "unexpected pass" of a `test.fail()`, etc.
// This script requires every declared (interaction, state) pair to have at
// least one execution that was both *expected* to pass and *actually*
// reached a "passed" final result in the report that was actually produced
// by this run -- and it validates the report itself is structurally
// complete (not stale, minimal, or fabricated) before trusting it at all.
//
// This module is also imported by `scripts/run-e2e-coverage-gate.mjs`, the
// atomic release-gate wrapper that generates its own invocation-unique
// report path, spawns `npm run test:e2e` with that path (via the
// `PLAYWRIGHT_JSON_REPORT_PATH` env var the config reads), and only then
// invokes `verifyReport` bound to that exact path plus that invocation's
// start time -- so a leftover or *concurrently written* report from an
// earlier, unrelated, or partially-failed run can never be reused (or
// raced) to silently satisfy the gate.
//
// Deliberately dependency-free beyond the already-installed `typescript`
// package: `src/testing/interaction-manifest.ts`,
// `src/testing/runtime-coverage-verifier.ts`, and
// `src/testing/playwright-projects.ts` are all zero-import TS modules (the
// manifest/required-project-names are passed in as plain parameters by
// design), so each is transpiled on the fly via `ts.transpileModule` and
// loaded independently -- no bundler, no ts-node/tsx, no relative-import
// resolution to worry about.
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

import ts from "typescript";

const repoRoot = path.resolve(import.meta.dirname, "..");
export const reportPath = path.join(repoRoot, "test-results", "report.json");
const manifestSourcePath = path.join(
  repoRoot,
  "src",
  "testing",
  "interaction-manifest.ts",
);
const verifierSourcePath = path.join(
  repoRoot,
  "src",
  "testing",
  "runtime-coverage-verifier.ts",
);
const projectsSourcePath = path.join(
  repoRoot,
  "src",
  "testing",
  "playwright-projects.ts",
);

/** Transpile a self-contained (zero-import) TS module to ESM and load it via
 * a temporary file + dynamic `import()`. The temp file is always removed,
 * even if the transpiled module throws while loading. */
async function loadStandaloneModule(sourcePath) {
  const source = readFileSync(sourcePath, "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
    fileName: sourcePath,
  });

  const tempDir = mkdtempSync(
    path.join(tmpdir(), "e2e-runtime-coverage-verifier-"),
  );
  const tempFile = path.join(
    tempDir,
    `${path.basename(sourcePath, ".ts")}.mjs`,
  );
  writeFileSync(tempFile, outputText, "utf8");
  try {
    return await import(pathToFileURL(tempFile).href);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
}

/** Read and parse the Playwright JSON report from disk. Throws a
 * descriptive error (never returns a partial/garbage value) if the file is
 * missing or is not valid JSON. */
function loadReport(atPath) {
  let raw;
  try {
    raw = readFileSync(atPath, "utf8");
  } catch (error) {
    throw new Error(
      `Could not read the Playwright JSON report at ${atPath}. ` +
        "Run `npm run test:e2e` first (the config's json reporter writes this file); " +
        `original error: ${error.message}`,
    );
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(
      `The Playwright JSON report at ${atPath} is not valid JSON ` +
        `(truncated/corrupted write?); original error: ${error.message}`,
    );
  }
}

/** Run the full verification: load the required modules, load and validate
 * the report's structural completeness, and compute + report coverage.
 * Fails closed (returns `ok: false`) on any structural problem, before ever
 * looking at coverage counts.
 *
 * @param {object} [options]
 * @param {string} [options.reportPath] Path to the Playwright JSON report to
 *   verify. Defaults to the fixed `test-results/report.json` path (what the
 *   plain, unwrapped `npm run test:e2e` command always writes to). The
 *   atomic release-gate wrapper instead passes its own invocation-unique
 *   path (matching the `PLAYWRIGHT_JSON_REPORT_PATH` it set before spawning
 *   `npm run test:e2e`), so it can only ever read back the exact artifact
 *   its own spawned run produced -- never a fixed, shared path a
 *   concurrent, unrelated invocation could also be writing to.
 * @param {number} [options.requireFreshSince] Optional epoch-ms timestamp;
 *   if provided, the report file's mtime must be at or after this instant,
 *   proving the report was written by an invocation that started at or
 *   after that time rather than being a stale leftover from an earlier,
 *   unrelated run. Kept as defense-in-depth alongside the invocation-unique
 *   path above (e.g. it would still catch a reporter misconfiguration that
 *   accidentally wrote to the wrong path). Used by the atomic release-gate
 *   wrapper, which records this timestamp immediately before it spawns its
 *   own `npm run test:e2e` run.
 */
export async function verifyReport({
  reportPath: reportPathOverride,
  requireFreshSince,
} = {}) {
  const targetReportPath = reportPathOverride ?? reportPath;
  const [{ UI_COVERAGE_MANIFEST }, verifierModule, { REQUIRED_PLAYWRIGHT_PROJECT_NAMES }] =
    await Promise.all([
      loadStandaloneModule(manifestSourcePath),
      loadStandaloneModule(verifierSourcePath),
      loadStandaloneModule(projectsSourcePath),
    ]);
  const { computeRuntimeCoverage, validateReportSchema } = verifierModule;

  if (requireFreshSince !== undefined) {
    let stat;
    try {
      stat = statSync(targetReportPath);
    } catch (error) {
      return {
        ok: false,
        schemaProblems: [
          `Could not stat the report at ${targetReportPath} for freshness verification: ${error.message}`,
        ],
        coverage: null,
      };
    }
    if (stat.mtimeMs < requireFreshSince) {
      return {
        ok: false,
        schemaProblems: [
          `Report at ${targetReportPath} was last written at ${new Date(stat.mtimeMs).toISOString()}, ` +
            `before this invocation started at ${new Date(requireFreshSince).toISOString()} -- ` +
            "it is a stale leftover, not evidence this invocation's run produced a passing report.",
        ],
        coverage: null,
      };
    }
  }

  const report = loadReport(targetReportPath);
  const schemaProblems = validateReportSchema(
    report,
    REQUIRED_PLAYWRIGHT_PROJECT_NAMES,
  );
  if (schemaProblems.length > 0) {
    return { ok: false, schemaProblems, coverage: null };
  }

  const coverage = computeRuntimeCoverage(report, UI_COVERAGE_MANIFEST);
  const ok =
    coverage.missingIds.length === 0 &&
    coverage.missingStates.length === 0 &&
    coverage.idsPresentButNeverPassed.length === 0 &&
    coverage.statesPresentButNeverPassed.length === 0;

  return { ok, schemaProblems: [], coverage };
}

function printResult(result) {
  if (result.schemaProblems.length > 0) {
    console.error(
      JSON.stringify({ ok: false, schemaProblems: result.schemaProblems }, null, 2),
    );
    console.error(
      "\nRuntime coverage verification FAILED (fail-closed): the Playwright " +
        "JSON report did not pass structural validation, so it cannot be " +
        "trusted to prove coverage at all:\n" +
        result.schemaProblems.map((problem) => `  - ${problem}`).join("\n"),
    );
    return;
  }

  const coverage = result.coverage;
  console.log(
    JSON.stringify(
      {
        ok: result.ok,
        // See run-e2e-coverage-gate.mjs for why these three counts are
        // distinct denominators reported separately: interactionCount is
        // the number of manifest interaction entries; requiredIdCount is
        // the (generally smaller) number of unique playwrightTestId
        // aliases those entries share; requiredStateCount is the number of
        // (interaction, state) pairs.
        interactionCount: coverage.interactionCount,
        requiredIdCount: coverage.requiredIdCount,
        passedIdCount: coverage.passedIdCount,
        requiredStateCount: coverage.requiredStateCount,
        passedStateCount: coverage.passedStateCount,
        missingIds: coverage.missingIds,
        missingStates: coverage.missingStates,
        idsPresentButNeverPassed: coverage.idsPresentButNeverPassed,
        statesPresentButNeverPassed: coverage.statesPresentButNeverPassed,
      },
      null,
      2,
    ),
  );

  if (!result.ok) {
    console.error(
      "\nRuntime coverage verification FAILED: at least one required " +
        "(interaction, state) pair has no execution that was both expected " +
        'to pass ("expectedStatus": "passed") and genuinely reached ' +
        '"passed" as its final result in this run\'s Playwright JSON ' +
        "report. A token being present in source (trusted by the static " +
        "AST scan) is not sufficient -- it must also have actually, " +
        "expectedly passed.",
    );
    return;
  }

  console.log(
    `\nRuntime coverage verification PASSED: ${coverage.passedStateCount}/` +
      `${coverage.requiredStateCount} required (interaction, state) pairs and ` +
      `${coverage.passedIdCount}/${coverage.requiredIdCount} required ` +
      `playwrightTestId aliases (spanning ${coverage.interactionCount} manifest ` +
      "interaction entries) each had a genuinely passed execution in this run.",
  );
}

async function main() {
  const result = await verifyReport();
  printResult(result);
  if (!result.ok) {
    process.exitCode = 1;
  }
}

const isMainModule =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMainModule) {
  await main();
}
