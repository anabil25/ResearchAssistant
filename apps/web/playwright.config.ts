import { execFileSync } from "node:child_process";

import { defineConfig, devices } from "@playwright/test";

import {
  defaultPortLockDeps,
  releasePortLock,
  tryClaimPortLock,
} from "./src/testing/port-lock";
import { REQUIRED_PLAYWRIGHT_PROJECT_NAMES } from "./src/testing/playwright-projects";

const deployedBaseUrl = process.env.PLAYWRIGHT_BASE_URL;

// Named from the single source of truth shared with the atomic release-gate
// script (`scripts/run-e2e-coverage-gate.mjs`) so the two can never drift:
// the gate requires every one of these names to have genuinely executed at
// least one test in the report it validates.
const [chromiumProjectName, tabletProjectName, mobileProjectName] =
  REQUIRED_PLAYWRIGHT_PROJECT_NAMES;

// Playwright loads this config file synchronously (as CommonJS, regardless
// of this repo's own ESM scripts elsewhere) -- a top-level `await` here
// throws `SyntaxError: await is only valid in async functions`. Port
// resolution below is therefore done synchronously via a short-lived child
// process instead of an in-process async `net.createServer().listen(0)`.

/**
 * Ask the OS for a currently-free ephemeral port by spawning a tiny,
 * self-contained Node child process that binds `listen(0)`, prints the
 * OS-assigned port, and exits -- run synchronously via `execFileSync` so
 * this config file never needs top-level `await`. Each call spawns its own
 * child and queries the OS independently, so three calls in this process
 * return three distinct ports from the live ephemeral pool; two *separate*
 * Playwright invocations (e.g. two worktrees/sessions on the same shared
 * machine) each get their own OS-assigned triple instead of racing for the
 * same fixed ports.
 *
 * Freeing the OS-level port immediately after the probe (via `s.close()`)
 * and this invocation's own webServer later binding it leaves a TOCTOU
 * window during which some *other* process could grab the same port --
 * this is true of any "find a free port" utility and cannot be closed
 * without OS-level socket handoff. What CAN be closed, and is: the
 * concrete, reproducible case where a second, concurrent invocation of
 * this very config file (e.g. another worktree/session running
 * `test:e2e`/`test:e2e:gate` on this shared machine at the same moment)
 * probes the OS at nearly the same time and is handed the *same* freed
 * port before this invocation's server binds it. `tryClaimPortLock` closes
 * exactly that window with a cross-process file lock (see
 * src/testing/port-lock.ts): if the OS-returned candidate is already
 * claimed by another live invocation, this retries with a fresh candidate
 * instead of both invocations proceeding with a doomed shared port.
 */
function findFreePortSync(): number {
  const lockDeps = defaultPortLockDeps();
  const maxAttempts = 25;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const output = execFileSync(
      process.execPath,
      [
        "-e",
        "const net=require('node:net');" +
          "const s=net.createServer();" +
          "s.on('error',(e)=>{process.stderr.write(String(e));process.exit(1);});" +
          "s.listen(0,'127.0.0.1',()=>{" +
          "const p=s.address().port;" +
          "s.close(()=>{process.stdout.write(String(p));});" +
          "});",
      ],
      { encoding: "utf8" },
    );
    const port = Number(output.trim());
    if (!Number.isInteger(port) || port <= 0) {
      throw new Error(
        `Could not determine an assigned ephemeral port (got: ${JSON.stringify(output)}).`,
      );
    }
    if (tryClaimPortLock(lockDeps, port)) {
      registerPortLockRelease(lockDeps, port);
      return port;
    }
    // A concurrent invocation on this shared machine already claimed this
    // exact port in the race window between the OS freeing it and either
    // invocation's server binding it -- ask the OS for a different one.
  }
  throw new Error(
    `Could not claim a free ephemeral port after ${maxAttempts} attempts (every candidate was claimed by a concurrent invocation).`,
  );
}

let portLockCleanupRegistered = false;
const claimedPorts: number[] = [];

/**
 * Release this invocation's port-lock claims when its process exits (normal
 * exit or SIGINT/SIGTERM), so a later invocation doesn't need to wait for
 * the (unnecessary, since we're exiting cleanly) stale-lock/liveness check
 * to reclaim the same port. Registered once per process, not once per port.
 */
function registerPortLockRelease(
  lockDeps: ReturnType<typeof defaultPortLockDeps>,
  port: number,
): void {
  claimedPorts.push(port);
  if (portLockCleanupRegistered) {
    return;
  }
  portLockCleanupRegistered = true;
  const releaseAll = () => {
    for (const claimedPort of claimedPorts) {
      releasePortLock(lockDeps, claimedPort);
    }
  };
  process.on("exit", releaseAll);
  process.on("SIGINT", () => {
    releaseAll();
    process.exit(130);
  });
  process.on("SIGTERM", () => {
    releaseAll();
    process.exit(143);
  });
}

/**
 * Resolve one port, memoized into `process.env[envVarName]`.
 *
 * Playwright reloads this config file in more than one process per
 * invocation: the top-level orchestrator (which actually starts the
 * `webServer` entries) evaluates it once, and each parallel test worker it
 * spawns evaluates it again independently to rebuild its own config/project
 * view. If port resolution called `findFreePortSync()` fresh every time the
 * config module runs, the orchestrator and its workers would each land on a
 * *different* OS-assigned ephemeral port -- the orchestrator's servers would
 * come up correctly on ports A/B/C, but a worker re-evaluating the config
 * would compute new ports A'/B'/C', point `baseURL`/env at those instead, and
 * every `page.goto` would hit `ERR_CONNECTION_REFUSED` against a port nothing
 * is listening on (reproduced while debugging this fix).
 *
 * Writing the freshly-resolved port back into `process.env` immediately
 * after resolving it fixes this: worker processes spawned by the
 * orchestrator inherit its environment at spawn time, so they see the
 * already-resolved value via the same "env var already set" branch below
 * instead of calling `findFreePortSync()` again -- one OS-allocated port per
 * invocation, shared by every process that loads this config, not one per
 * config-module evaluation.
 */
function resolvePort(envVarName: string): number {
  const existing = process.env[envVarName];
  if (existing) {
    return Number(existing);
  }
  const port = findFreePortSync();
  process.env[envVarName] = String(port);
  return port;
}

/**
 * Resolve the three local ports this config's `webServer` entries and
 * `baseURL` use for the connector adapter, the API, and the web app.
 * Overridable per-port via env var (to reproduce or pin a specific run, or
 * to share one already-resolved value across the processes described in
 * `resolvePort` above); otherwise each defaults to a freshly OS-allocated
 * ephemeral port for this invocation. Replaces the previous fixed
 * 3000/8100/8200, which made any two concurrent
 * `npm run test:e2e`/`test:e2e:gate` invocations on this shared machine
 * mutually exclusive (the second would fail with EADDRINUSE against
 * servers -- possibly belonging to a different worktree/branch entirely --
 * already bound to those fixed ports, rather than starting its own isolated
 * set). Skipped entirely when `PLAYWRIGHT_BASE_URL` is set: that mode targets
 * an already-deployed environment and never starts a local `webServer`.
 */
function resolveLocalPorts(): {
  gatewayPort: number;
  apiPort: number;
  webPort: number;
} {
  return {
    gatewayPort: resolvePort("PLAYWRIGHT_GATEWAY_PORT"),
    apiPort: resolvePort("PLAYWRIGHT_API_PORT"),
    webPort: resolvePort("PLAYWRIGHT_WEB_PORT"),
  };
}

const { gatewayPort, apiPort, webPort } = deployedBaseUrl
  ? { gatewayPort: 0, apiPort: 0, webPort: 0 }
  : resolveLocalPorts();

export default defineConfig({
  testDir: "./e2e",
  // Overridable via PLAYWRIGHT_OUTPUT_DIR so the atomic release-gate
  // wrapper (scripts/run-e2e-coverage-gate.mjs) can give each of its
  // invocations its own invocation-unique directory instead of always
  // using the shared `./test-results`. This matters because Playwright's
  // own test runner unconditionally deletes its entire configured
  // `outputDir` recursively as the very first step of every invocation
  // (`createRemoveOutputDirsTask`); a fixed, shared `outputDir` meant a
  // second, concurrently started invocation's startup would wholesale
  // delete the first invocation's already-written artifacts/report --
  // even one written under a uniquely named file -- since the *directory*
  // itself, not just the filename, was shared. See
  // resolveInvocationPaths in scripts/gate-invocation-paths.mjs and
  // scripts/prove-concurrent-gate-report-isolation.mjs. The plain,
  // unwrapped `npm run test:e2e` command (used for the manual "run it
  // twice" determinism proof) leaves this env var unset and keeps using
  // the fixed `./test-results` directory unchanged.
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "./test-results",
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report" }],
    // Machine-readable output consumed by
    // scripts/verify-playwright-runtime-coverage.mjs: proves every required
    // (interaction, state) pair has at least one execution whose *final*
    // result genuinely reached "passed" in this actual run, closing the
    // static-scan-only loophole (a runtime-only skip/failure can't satisfy
    // coverage just because its title carries the right token).
    //
    // The output path is overridable via PLAYWRIGHT_JSON_REPORT_PATH so the
    // atomic release-gate wrapper (scripts/run-e2e-coverage-gate.mjs) can
    // give each of its invocations its own invocation-unique path instead
    // of always writing to the same fixed `test-results/report.json` --
    // that fixed path plus an mtime-freshness check left a narrow window
    // where a *different*, concurrently-running `npm run test:e2e`
    // invocation could overwrite the file with its own fresh-mtimed report
    // in between this run finishing and the gate reading it back, letting
    // one invocation's gate silently validate another invocation's report.
    // An invocation-unique path makes that structurally impossible: two
    // concurrent gate runs can never write to the same file. The plain,
    // unwrapped `npm run test:e2e` command (used for the manual
    // "run it twice" determinism proof) leaves this env var unset and keeps
    // writing to the fixed path unchanged.
    [
      "json",
      {
        outputFile:
          process.env.PLAYWRIGHT_JSON_REPORT_PATH ?? "test-results/report.json",
      },
    ],
  ],
  timeout: deployedBaseUrl ? 300_000 : 30_000,
  expect: {
    timeout: deployedBaseUrl ? 240_000 : 5_000,
  },
  use: {
    baseURL: deployedBaseUrl ?? `http://127.0.0.1:${webPort}`,
    trace: "on-first-retry",
  },
  webServer: deployedBaseUrl
    ? undefined
    : [
        {
          command: `uv --directory ../.. run --package research-assistant-connector-adapter uvicorn research_assistant_connector_adapter.app:app --host 127.0.0.1 --port ${gatewayPort}`,
          url: `http://127.0.0.1:${gatewayPort}/health`,
          // Always false, even locally: this repo is frequently checked out
          // into multiple concurrent worktrees/sessions on one shared
          // machine. `!process.env.CI` would let Playwright silently attach
          // to *any* process already answering this health check --
          // including a different worktree's server running a different
          // branch/build -- producing cross-session contamination
          // (mismatched frontend/backend code) instead of a loud, honest
          // failure. Forcing an exclusive, freshly started server per run,
          // on this invocation's own OS-allocated port (see
          // `resolveLocalPorts` above), is required for deterministic,
          // mutually-non-blocking results when multiple invocations run
          // concurrently on this shared machine.
          reuseExistingServer: false,
        },
        {
          command: `uv --directory ../.. run --package research-assistant-api uvicorn research_assistant_api.app:app --host 127.0.0.1 --port ${apiPort}`,
          env: {
            RESEARCH_CONNECTOR_GATEWAY_URL: `http://127.0.0.1:${gatewayPort}`,
          },
          url: `http://127.0.0.1:${apiPort}/health`,
          // See connector-adapter server above: always exclusive, never reused.
          reuseExistingServer: false,
        },
        {
          command: "npm run start",
          env: {
            INTERNAL_API_URL: `http://127.0.0.1:${apiPort}`,
            PORT: String(webPort),
          },
          url: `http://127.0.0.1:${webPort}`,
          // See connector-adapter server above: always exclusive, never reused.
          reuseExistingServer: false,
        },
      ],
  projects: [
    {
      name: chromiumProjectName,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: tabletProjectName,
      testMatch: /visual-coverage\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 834, height: 1112 },
      },
    },
    {
      name: mobileProjectName,
      testMatch: /visual-coverage\.spec\.ts/,
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 390, height: 844 },
      },
    },
  ],
});
