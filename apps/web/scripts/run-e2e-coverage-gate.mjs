// Atomic release-gate wrapper: makes "run the E2E suite" and "verify its
// coverage" a single, indivisible operation, closing the reviewer-identified
// gap where a stale, minimal, or fabricated `test-results/report.json` left
// over from an earlier/unrelated/partially-failed invocation could
// otherwise be picked up by a standalone verify step and made to look like
// this run passed -- including the narrower race where a *different*,
// concurrently-running gate invocation could overwrite the same fixed path
// with its own fresh-mtimed report in between this run finishing and this
// gate reading the file back, which an mtime-only freshness check cannot
// distinguish from this invocation's own report.
//
// What it does, in order, and why each step matters:
//
//   1. Generates an invocation-unique *directory*
//      (`test-results/gate-<uuid>/`, via `resolveInvocationPaths`) and
//      passes it to Playwright as `outputDir` (via `PLAYWRIGHT_OUTPUT_DIR`,
//      read by `playwright.config.ts`), with the JSON report nested inside
//      it (`PLAYWRIGHT_JSON_REPORT_PATH`). A unique *filename* alone is not
//      enough: Playwright's own test runner unconditionally deletes its
//      entire configured `outputDir` recursively as the first step of
//      every invocation (`createRemoveOutputDirsTask` in
//      `playwright/lib/runner/index.js`), so a second, concurrently
//      started invocation sharing the same parent `outputDir` would
//      wholesale delete it -- including a first invocation's
//      already-written, uniquely *named* report sitting inside that same
//      shared folder -- before the first invocation's gate ever read it
//      back. Giving each invocation its own, structurally distinct
//      `outputDir` means each one only ever clears its own folder; two
//      concurrent invocations can never observe or clear each other's.
//   2. Records a start timestamp *before* spawning anything (kept as
//      defense-in-depth alongside the unique path).
//   3. Spawns the exact same `npm run test:e2e` command used for manual
//      verification (unchanged: `next build && playwright test`, governed
//      entirely by `playwright.config.ts` -- no `--workers` or other CLI
//      flags are added here), inheriting stdio so its full output is
//      visible, with `PLAYWRIGHT_JSON_REPORT_PATH`/`PLAYWRIGHT_OUTPUT_DIR`
//      set in its environment.
//   4. If that process exits non-zero, fails immediately -- a failed
//      Playwright run must never be handed to the coverage verifier at all.
//   5. Otherwise, calls into `verify-playwright-runtime-coverage.mjs`'s
//      `verifyReport`, passing both the unique report path (so it reads
//      back only the exact artifact this invocation's spawned run produced)
//      and the recorded start timestamp (so the file's mtime is additionally
//      required to be at or after that instant), in addition to full schema
//      validation (non-empty suites/stats/errors, every configured project
//      genuinely represented) and the existing coverage checks.
//
// This script is the intended CI/release-gate command
// (`npm run test:e2e:gate`). The plain `npm run test:e2e` command remains
// unchanged (no PLAYWRIGHT_JSON_REPORT_PATH/PLAYWRIGHT_OUTPUT_DIR set, so it
// keeps writing to the fixed `test-results/report.json` path/`test-results/`
// outputDir) and continues to serve as the separate "exact command, run
// twice" manual-determinism proof. See
// `scripts/prove-concurrent-gate-report-isolation.mjs` for a standalone,
// reproducible proof that two concurrent invocations of this script's path
// scheme cannot clear or overwrite each other's report/output directory.
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";

import { verifyReport } from "./verify-playwright-runtime-coverage.mjs";
import {
  resolveInvocationPaths,
  stripGateDistDirIncludes,
} from "./gate-invocation-paths.mjs";

const repoRoot = path.resolve(import.meta.dirname, "..");

/** Undo `next build`'s in-place `tsconfig.json` rewrite for this invocation's
 * throwaway build directory (see `stripGateDistDirIncludes`). Called whether
 * the run passed or failed, so a failing gate never leaves the tree dirty
 * either. */
function cleanGateEntriesFromTsconfig() {
  const tsconfigPath = path.join(repoRoot, "tsconfig.json");
  let raw;
  try {
    raw = readFileSync(tsconfigPath, "utf8");
  } catch {
    return;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // Never rewrite a file we could not parse.
    return;
  }
  const { config, changed } = stripGateDistDirIncludes(parsed);
  if (!changed) {
    return;
  }
  writeFileSync(tsconfigPath, `${JSON.stringify(config, null, 2)}\n`);
}

function main() {
  const {
    outputDir: invocationOutputDir,
    reportPath: invocationReportPath,
    htmlReportDir: invocationHtmlReportDir,
    distDir: invocationDistDir,
  } = resolveInvocationPaths(repoRoot, randomUUID());
  // Defensive only: a UUID collision with a leftover directory is not
  // expected, but if this exact path somehow already existed, remove it so
  // stale contents at this unique path can never be mistaken for this
  // run's output.
  if (existsSync(invocationOutputDir)) {
    rmSync(invocationOutputDir, { recursive: true, force: true });
  }

  const startedAt = Date.now();
  console.log(
    `Starting atomic E2E coverage gate at ${new Date(startedAt).toISOString()}: ` +
      `spawning \`npm run test:e2e\` (unmodified: next build && playwright test) ` +
      `with invocation-unique output directory ${invocationOutputDir}, ` +
      `report path ${invocationReportPath}, HTML report directory ` +
      `${invocationHtmlReportDir}, and Next build directory ${invocationDistDir}...`,
  );

  const run = spawnSync("npm run test:e2e", {
    cwd: repoRoot,
    stdio: "inherit",
    shell: true,
    env: {
      ...process.env,
      PLAYWRIGHT_JSON_REPORT_PATH: invocationReportPath,
      PLAYWRIGHT_OUTPUT_DIR: invocationOutputDir,
      PLAYWRIGHT_HTML_REPORT_DIR: invocationHtmlReportDir,
      NEXT_DIST_DIR: invocationDistDir,
    },
  });

  if (run.status !== 0) {
    cleanGateEntriesFromTsconfig();
    console.error(
      `\nAtomic E2E coverage gate FAILED: \`npm run test:e2e\` itself exited ` +
        `with status ${run.status ?? "unknown"} (or signal ${run.signal ?? "none"}). ` +
        "A failed Playwright run can never satisfy the coverage gate, " +
        "regardless of what report.json ends up on disk.",
    );
    process.exitCode = 1;
    return Promise.resolve();
  }

  return verifyReport({
    reportPath: invocationReportPath,
    requireFreshSince: startedAt,
  }).then((result) => {
    cleanGateEntriesFromTsconfig();
    if (result.schemaProblems.length > 0) {
      console.error(
        "\nAtomic E2E coverage gate FAILED (fail-closed): the report produced " +
          "by this exact invocation did not pass structural/freshness " +
          "validation:\n" +
          result.schemaProblems.map((problem) => `  - ${problem}`).join("\n"),
      );
      process.exitCode = 1;
      return;
    }

    const coverage = result.coverage;
    console.log(
      JSON.stringify(
        {
          ok: result.ok,
          // Three DISTINCT denominators, reported separately on purpose so
          // none can be mistaken for another:
          //   - interactionCount: number of manifest *interaction entries*
          //     (e.g. 77) -- the actual number of distinct UI
          //     interactions the manifest declares.
          //   - requiredIdCount/passedIdCount: number of unique
          //     `playwrightTestId` *aliases* (e.g. 64) -- several
          //     interaction entries can share one alias (one spec covering
          //     more than one interaction), so this is always
          //     <= interactionCount and must never be reported as if it
          //     were the interaction-entry count.
          //   - requiredStateCount/passedStateCount: number of
          //     (interaction, state) pairs (e.g. 299).
          interactionCount: coverage.interactionCount,
          requiredIdCount: coverage.requiredIdCount,
          passedIdCount: coverage.passedIdCount,
          requiredStateCount: coverage.requiredStateCount,
          passedStateCount: coverage.passedStateCount,
          missingIds: coverage.missingIds,
          missingStates: coverage.missingStates,
          idsPresentButNeverPassed: coverage.idsPresentButNeverPassed,
          statesPresentButNeverPassed: coverage.statesPresentButNeverPassed,
          // Per-viewport/project attribution: which project actually proved
          // each token. The global counts above deliberately cannot show
          // that, and a project can execute tests while proving no coverage.
          perProject: coverage.perProject,
          projectsWithoutEvidence: coverage.projectsWithoutEvidence,
        },
        null,
        2,
      ),
    );

    if (!result.ok) {
      console.error(
        coverage.projectsWithoutEvidence.length > 0
          ? "\nAtomic E2E coverage gate FAILED: required Playwright " +
              `project(s) ${coverage.projectsWithoutEvidence.join(", ")} ` +
              "contributed no genuinely passed coverage token in this " +
              "invocation's report."
          : "\nAtomic E2E coverage gate FAILED: at least one required " +
              "(interaction, state) pair never had a genuinely, expectedly " +
              "passed execution in this exact invocation's report.",
      );
      process.exitCode = 1;
      return;
    }

    console.log(
      `\nAtomic E2E coverage gate PASSED: \`npm run test:e2e\` exited 0, the ` +
        "report it produced passed schema/freshness/project-completeness " +
        `validation, and ${coverage.passedStateCount}/${coverage.requiredStateCount} ` +
        `required (interaction, state) pairs + ${coverage.passedIdCount}/${coverage.requiredIdCount} ` +
        "required playwrightTestId aliases (spanning " +
        `${coverage.interactionCount} manifest interaction entries) each had ` +
        "a genuinely, expectedly passed execution, with every required " +
        "project contributing genuine evidence (" +
        `${coverage.perProject
          .map(
            (entry) =>
              `${entry.projectName}: ${entry.passedIdCount} ids / ${entry.passedStateCount} states`,
          )
          .join("; ")}).`,
    );
  });
}

await main();
