import { writeFileSync } from "node:fs";
import { join } from "node:path";

import {
  defaultPortLockDeps,
  releasePortLock,
  tryClaimPortLock,
} from "./port-lock";
import globalSetup from "./port-lock-handshake";

/**
 * Playwright's real invocation always runs this module as `globalSetup`
 * after every configured `webServer` is confirmed healthy (see the module's
 * own doc comment for why that ordering matters). These tests exercise it
 * directly as a plain async function, which is equivalent for its own
 * internal logic since it reads no Playwright-specific context -- only
 * environment variables and the port-lock module.
 */
describe("port-lock-handshake globalSetup", () => {
  const ENV_KEYS = [
    "PLAYWRIGHT_BASE_URL",
    "PLAYWRIGHT_GATEWAY_PORT",
    "PLAYWRIGHT_API_PORT",
    "PLAYWRIGHT_WEB_PORT",
  ] as const;
  let savedEnv: Record<string, string | undefined>;

  beforeEach(() => {
    savedEnv = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
    for (const key of ENV_KEYS) {
      delete process.env[key];
    }
  });

  afterEach(() => {
    for (const key of ENV_KEYS) {
      const value = savedEnv[key];
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  });

  it("is a no-op in PLAYWRIGHT_BASE_URL (deployed-target) mode, even with claimed-port env vars also set", async () => {
    process.env.PLAYWRIGHT_BASE_URL = "https://deployed.example";
    process.env.PLAYWRIGHT_WEB_PORT = "40100";
    await expect(globalSetup()).resolves.toBeUndefined();
  });

  it("is a no-op when no PLAYWRIGHT_*_PORT env vars are set at all", async () => {
    await expect(globalSetup()).resolves.toBeUndefined();
  });

  it("ignores non-numeric or non-positive port env values and still resolves cleanly when the remaining valid claimed port verifies", async () => {
    const deps = defaultPortLockDeps();
    const validPort = 40101;
    process.env.PLAYWRIGHT_GATEWAY_PORT = "not-a-number";
    process.env.PLAYWRIGHT_API_PORT = "0"; // not > 0, must be filtered out
    process.env.PLAYWRIGHT_WEB_PORT = String(validPort);
    try {
      expect(tryClaimPortLock(deps, validPort)).toBe(true);
      await expect(globalSetup()).resolves.toBeUndefined();
    } finally {
      releasePortLock(deps, validPort);
    }
  });

  it("resolves cleanly when every claimed port's lock still names this exact invocation as owner", async () => {
    const deps = defaultPortLockDeps();
    const gatewayPort = 40102;
    const apiPort = 40103;
    const webPort = 40104;
    process.env.PLAYWRIGHT_GATEWAY_PORT = String(gatewayPort);
    process.env.PLAYWRIGHT_API_PORT = String(apiPort);
    process.env.PLAYWRIGHT_WEB_PORT = String(webPort);
    try {
      expect(tryClaimPortLock(deps, gatewayPort, [gatewayPort, apiPort, webPort])).toBe(true);
      expect(tryClaimPortLock(deps, apiPort, [gatewayPort, apiPort, webPort])).toBe(true);
      expect(tryClaimPortLock(deps, webPort, [gatewayPort, apiPort, webPort])).toBe(true);
      await expect(globalSetup()).resolves.toBeUndefined();
    } finally {
      releasePortLock(deps, gatewayPort);
      releasePortLock(deps, apiPort);
      releasePortLock(deps, webPort);
    }
  });

  it("throws a descriptive, actionable error naming every port whose lock was reclaimed by a foreign invocation (the exact contamination this handshake exists to catch)", async () => {
    const deps = defaultPortLockDeps();
    const stolenPort = 40105;
    const stillOwnedPort = 40106;
    process.env.PLAYWRIGHT_GATEWAY_PORT = String(stolenPort);
    process.env.PLAYWRIGHT_WEB_PORT = String(stillOwnedPort);
    try {
      expect(tryClaimPortLock(deps, stolenPort)).toBe(true);
      expect(tryClaimPortLock(deps, stillOwnedPort)).toBe(true);

      // Simulate a concurrent invocation legitimately reclaiming `stolenPort`
      // as stale (matching the real scenario: this invocation's heartbeat
      // went stale, a different invocation claimed the same port, and this
      // invocation is now the last to find out). Written directly rather
      // than through `tryClaimPortLock` because that function's exclusive
      // `wx` create would reject re-claiming an already-locked path --
      // exactly the point: a genuine foreign takeover only ever happens
      // after this process's own record was first removed/replaced by
      // someone else, which we simulate here by overwriting the file
      // in-place with a foreign owner's record.
      const stolenLockPath = join(deps.lockDir, `${stolenPort}.lock`);
      writeFileSync(
        stolenLockPath,
        JSON.stringify({
          pid: deps.pid + 1,
          nonce: "foreign-invocation-nonce",
          worktreeRoot: deps.worktreeRoot,
          ports: [stolenPort],
          claimedAt: deps.now(),
          heartbeatAt: deps.now(),
        }),
      );

      await expect(globalSetup()).rejects.toThrow(
        new RegExp(`\\b${stolenPort}\\b`),
      );
      await expect(globalSetup()).rejects.toThrow(/no longer recorded as the current owner/);
      // The still-genuinely-owned port must not be named in the failure --
      // only the actually-contended one.
      await expect(globalSetup()).rejects.not.toThrow(
        new RegExp(`\\b${stillOwnedPort}\\b`),
      );
    } finally {
      releasePortLock(deps, stillOwnedPort);
      releasePortLock(deps, stolenPort);
    }
  });
});
