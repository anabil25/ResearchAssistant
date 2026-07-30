import { execFileSync } from "node:child_process";

import { defineConfig, devices } from "@playwright/test";

import {
  defaultPortLockDeps,
  lockBelongsToInvocation,
  releasePortLock,
  touchPortLock,
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
/**
 * Ask the OS for `count` simultaneously-free ephemeral ports, then claim a
 * cross-process file lock on each, retrying the whole set if any candidate is
 * already claimed by a concurrent invocation.
 *
 * All `count` sockets are bound **at the same time** inside a single child
 * process and only closed once every port has been reported. That removes the
 * bind-close-bind pattern this replaced, where each port was probed by its own
 * child that bound, printed, and closed before the next child ran: the OS was
 * free to hand probe N+1 the exact port probe N had just released, so the
 * three "distinct" ports could collide with each other and the file lock was
 * the only thing catching it. Binding the whole set at once makes intra-set
 * distinctness a property of the OS allocator rather than of a retry loop.
 *
 * What genuinely cannot be closed here, and is stated plainly rather than
 * implied: the interval between this child exiting (releasing all `count`
 * sockets) and Playwright's `webServer` processes binding them. Handing a
 * live listening socket to an unrelated child process is not portable, so
 * every "find a free port" utility has this window. Two things bound its
 * consequences. Within this tooling, the port lock means a concurrent
 * invocation of this same config never *selects* a port we hold, which is the
 * only collision source that was actually reproducible in practice. Against a
 * genuinely unrelated process on the machine, the outcome is a loud
 * `EADDRINUSE` at server startup, not silent cross-talk between two runs.
 */
function allocateLockedPortsSync(count: number): number[] {
  const lockDeps = defaultPortLockDeps();
  const maxAttempts = 25;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const candidates = probeSimultaneouslyFreePorts(count);
    const claimed: number[] = [];
    for (const port of candidates) {
      if (!tryClaimPortLock(lockDeps, port, [...claimedPorts, ...claimed, port])) {
        break;
      }
      claimed.push(port);
    }
    if (claimed.length === count) {
      for (const port of claimed) {
        registerPortLockRelease(lockDeps, port);
      }
      return claimed;
    }
    // A concurrent invocation on this shared machine already claimed one of
    // these ports. Release the partial set so we never strand a claim we are
    // not going to use, and ask the OS for a whole new set.
    for (const port of claimed) {
      releasePortLock(lockDeps, port);
    }
  }
  throw new Error(
    `Could not claim ${count} free ephemeral ports after ${maxAttempts} attempts (a concurrent invocation claimed at least one candidate every time).`,
  );
}

/** Bind `count` ephemeral sockets at once in a short-lived child process and
 * return the ports the OS assigned. Synchronous (via `execFileSync`) because
 * a Playwright config file is loaded as CommonJS in some of the processes
 * that read it, where a top-level `await` is a syntax error. */
function probeSimultaneouslyFreePorts(count: number): number[] {
  const output = execFileSync(
    process.execPath,
    [
      "-e",
      "const net=require('node:net');" +
        `const count=${count};` +
        "const servers=[];const ports=[];" +
        "const fail=(e)=>{process.stderr.write(String(e));process.exit(1);};" +
        "const bindOne=()=>{" +
        "const s=net.createServer();" +
        "s.on('error',fail);" +
        "s.listen(0,'127.0.0.1',()=>{" +
        "servers.push(s);ports.push(s.address().port);" +
        // Every socket stays bound until the whole set is allocated, so the
        // OS can never hand the same port to two entries of this set.
        "if(ports.length<count){bindOne();return;}" +
        "let remaining=servers.length;" +
        "for(const server of servers){server.close(()=>{" +
        "remaining-=1;" +
        "if(remaining===0){process.stdout.write(ports.join(','));}" +
        "});}" +
        "});};" +
        "bindOne();",
    ],
    { encoding: "utf8" },
  );
  const ports = output
    .trim()
    .split(",")
    .map((value) => Number(value.trim()));
  if (
    ports.length !== count ||
    ports.some((port) => !Number.isInteger(port) || port <= 0)
  ) {
    throw new Error(
      `Could not determine ${count} assigned ephemeral ports (got: ${JSON.stringify(output)}).`,
    );
  }
  return ports;
}

let portLockCleanupRegistered = false;
const claimedPorts: number[] = [];

// How often the live owner refreshes each claimed lock's heartbeat --
// comfortably inside `heartbeatStaleMs` (45s, see port-lock.ts) so a
// concurrent invocation never mistakes an active, merely-slow-starting
// server for an abandoned one.
const HEARTBEAT_INTERVAL_MS = 15_000;

/**
 * Release this invocation's port-lock claims when its process exits (normal
 * exit or SIGINT/SIGTERM), so a later invocation doesn't need to wait for
 * the (unnecessary, since we're exiting cleanly) stale-lock/liveness check
 * to reclaim the same port. Also starts (once per process, not once per
 * port) the periodic heartbeat that keeps every claimed lock looking
 * "actively owned" for as long as this invocation is genuinely alive --
 * without it, a slow `npm run build`/server-startup phase alone could
 * exceed `heartbeatStaleMs` and cause a concurrent invocation to
 * legitimately (from its point of view) reclaim the same port mid-run.
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
  const heartbeatTimer = setInterval(() => {
    for (const claimedPort of claimedPorts) {
      touchPortLock(lockDeps, claimedPort);
    }
  }, HEARTBEAT_INTERVAL_MS);
  // Never let the heartbeat alone keep the orchestrator process alive --
  // it should only run for as long as the process has other real work.
  heartbeatTimer.unref();
  const releaseAll = () => {
    clearInterval(heartbeatTimer);
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
 * All three ports are resolved together (see `resolveLocalPorts`) and
 * memoized into `process.env` immediately.
 *
 * Playwright reloads this config file in more than one process per
 * invocation: the top-level orchestrator (which actually starts the
 * `webServer` entries) evaluates it once, and each parallel test worker it
 * spawns evaluates it again independently to rebuild its own config/project
 * view. If port resolution allocated fresh ports every time the config module
 * ran, the orchestrator and its workers would each land on a *different*
 * OS-assigned set -- the orchestrator's servers would come up correctly on
 * ports A/B/C, but a worker re-evaluating the config would compute new ports
 * A'/B'/C', point `baseURL`/env at those instead, and every `page.goto` would
 * hit `ERR_CONNECTION_REFUSED` against a port nothing is listening on
 * (reproduced while debugging this fix).
 *
 * Writing the resolved ports back into `process.env` fixes this: worker
 * processes spawned by the orchestrator inherit its environment at spawn
 * time, so they see the already-resolved values instead of allocating again
 * -- one OS-allocated set per invocation, shared by every process that loads
 * this config, not one per config-module evaluation.
 *
 * Resolve the three local ports this config's `webServer` entries and
 * `baseURL` use for the connector adapter, the API, and the web app.
 * Overridable via env var (to reproduce or pin a specific run, or to share
 * already-resolved values across the processes described above); otherwise
 * they default to a freshly OS-allocated, lock-claimed set for this
 * invocation. Replaces the previous fixed
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
  const envVarNames = [
    "PLAYWRIGHT_GATEWAY_PORT",
    "PLAYWRIGHT_API_PORT",
    "PLAYWRIGHT_WEB_PORT",
  ] as const;
  const preset = envVarNames.map((name) => process.env[name]);

  if (preset.every((value) => value)) {
    const ports = preset.map((value) => Number(value));
    // Two very different situations reach here and must be told apart,
    // because the globalSetup identity handshake requires this invocation to
    // hold a lock on every port it uses:
    //
    //  - A worker process re-loading this config after our own orchestrator
    //    memoized its freshly-allocated ports into the environment. The
    //    orchestrator already holds the locks; the worker must not try to
    //    claim them again. `PLAYWRIGHT_PORTS_RESOLVED_BY` carries the nonce
    //    of the invocation that resolved them, which is what distinguishes
    //    this case unambiguously. Comparing the *lock file's* nonce instead
    //    is not equivalent and was wrong: if a concurrent invocation ever
    //    reclaims one of our locks as stale, every worker then treats the
    //    port as an unclaimable external override and dies during config
    //    load, turning a condition the handshake is designed to report
    //    cleanly into a pile of unrelated-looking test failures.
    //  - A human or CI job pinning ports explicitly. Nothing claimed a lock
    //    for those, so the handshake would fail the run outright -- an
    //    override that made the tooling refuse to start. Claim them here
    //    through the same lock instead, so a pinned port participates in
    //    exactly the same mutual exclusion as an allocated one.
    if (process.env.PLAYWRIGHT_PORTS_RESOLVED_BY !== invocationNonce()) {
      claimPresetPorts(ports);
      process.env.PLAYWRIGHT_PORTS_RESOLVED_BY = invocationNonce();
    }
    const [gatewayPort, apiPort, webPort] = ports;
    return { gatewayPort, apiPort, webPort };
  }

  if (preset.some((value) => value)) {
    throw new Error(
      "Partial port override: set all of " +
        `${envVarNames.join(", ")} or none of them. Mixing a pinned port with ` +
        "an OS-allocated one silently leaves the pinned port outside this " +
        "invocation's port-lock set.",
    );
  }

  const [gatewayPort, apiPort, webPort] = allocateLockedPortsSync(3);
  envVarNames.forEach((name, index) => {
    process.env[name] = String([gatewayPort, apiPort, webPort][index]);
  });
  process.env.PLAYWRIGHT_PORTS_RESOLVED_BY = invocationNonce();
  return { gatewayPort, apiPort, webPort };
}

/** This invocation's identity, shared by the orchestrator and every worker it
 * spawns (see `defaultPortLockDeps`, which memoizes it into the environment). */
function invocationNonce(): string {
  return defaultPortLockDeps().nonce;
}

/** Bring explicitly pinned ports under this invocation's port lock.
 *
 * Reached only for ports this invocation did not resolve itself. A genuinely
 * unclaimed pinned port is claimed now. A pinned port held by a *different*
 * live invocation is a hard error: proceeding would start servers on a port
 * another run is already serving on, which is the exact collision the lock
 * exists to prevent, and failing here names the problem far more clearly than
 * the EADDRINUSE (or, worse, the silent cross-talk) that would follow. */
function claimPresetPorts(ports: readonly number[]): void {
  const lockDeps = defaultPortLockDeps();
  for (const port of ports) {
    if (!Number.isInteger(port) || port <= 0) {
      throw new Error(
        `Invalid pinned Playwright port ${JSON.stringify(port)}: expected a positive integer.`,
      );
    }
    if (lockBelongsToInvocation(lockDeps, port)) {
      continue;
    }
    if (!tryClaimPortLock(lockDeps, port, [...claimedPorts, ...ports])) {
      throw new Error(
        `Pinned Playwright port ${port} is already locked by another live invocation on this machine. ` +
          "Release it, wait for that invocation to finish, or unset the " +
          "PLAYWRIGHT_*_PORT overrides to let this invocation allocate its own ports.",
      );
    }
    registerPortLockRelease(lockDeps, port);
  }
}

const { gatewayPort, apiPort, webPort } = deployedBaseUrl
  ? { gatewayPort: 0, apiPort: 0, webPort: 0 }
  : resolveLocalPorts();

export default defineConfig({
  testDir: "./e2e",
  // The identity-handshake half of the port-lock hardening (see
  // src/testing/port-lock-handshake.ts's doc comment): runs once, after
  // Playwright has confirmed every local webServer below is actually up
  // and answering its health check, and aborts the run loudly if this
  // invocation's port-lock claims were reclaimed as stale by a concurrent
  // invocation in the meantime. A no-op in deployed-target mode (no local
  // webServer, no port claims to verify).
  globalSetup: "./src/testing/port-lock-handshake",
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
    ["html", { open: "never", outputFolder: process.env.PLAYWRIGHT_HTML_REPORT_DIR ?? "playwright-report" }],
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
      testMatch: /(visual-coverage|agent-chat)\.spec\.ts/,
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 834, height: 1112 },
      },
    },
    {
      name: mobileProjectName,
      testMatch: /(visual-coverage|agent-chat)\.spec\.ts/,
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 390, height: 844 },
      },
    },
  ],
});
