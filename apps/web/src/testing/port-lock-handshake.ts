import { defaultPortLockDeps, verifyLockIdentity } from "./port-lock";

/**
 * Playwright `globalSetup` entry point: the identity-handshake half of the
 * port-lock hardening (see `port-lock.ts`'s doc comments for the claim/
 * heartbeat/reclaim half).
 *
 * Playwright's own documented lifecycle starts every configured
 * `webServer` and waits for each to answer its health-check `url` *before*
 * running `globalSetup`, and runs `globalSetup` *before* any test starts.
 * That gives this function a genuine post-startup vantage point: by the
 * time it runs, every local server this invocation asked for is
 * confirmed up. This re-reads each claimed port's lock file and confirms
 * it still names this exact invocation (matching nonce and pid) as
 * owner -- not merely that a lock exists.
 *
 * Why this can still fail even though `tryClaimPortLock` succeeded earlier
 * in the same invocation: the claim happens when the config module is
 * first evaluated, well before servers finish starting (which can take
 * several seconds). If this process were somehow stalled long enough for
 * its own heartbeat to look abandoned to a *different*, concurrently
 * running invocation, that invocation could legitimately reclaim the same
 * port as stale and start its own server on it in the meantime. Without
 * this check, this invocation's tests would silently run against a port
 * it no longer has exclusive ownership of (possibly now serving a
 * different worktree/branch's build). This check turns that into a loud,
 * immediate failure instead -- "fail fast, rerun" rather than a confusing,
 * hard-to-diagnose cross-invocation contamination.
 */
export default async function globalSetup(): Promise<void> {
  if (process.env.PLAYWRIGHT_BASE_URL) {
    // Deployed-target mode: no local webServer, no port claims to verify.
    return;
  }

  const claimedPorts = [
    process.env.PLAYWRIGHT_GATEWAY_PORT,
    process.env.PLAYWRIGHT_API_PORT,
    process.env.PLAYWRIGHT_WEB_PORT,
  ]
    .filter((value): value is string => Boolean(value))
    .map(Number)
    .filter((port) => Number.isInteger(port) && port > 0);

  if (claimedPorts.length === 0) {
    return;
  }

  const deps = defaultPortLockDeps();
  const foreignOwnership = claimedPorts.filter(
    (port) => !verifyLockIdentity(deps, port),
  );

  if (foreignOwnership.length > 0) {
    throw new Error(
      "Port lock identity handshake failed for port(s) " +
        `${foreignOwnership.join(", ")}: this invocation's claim is no ` +
        "longer recorded as the current owner. A concurrent invocation on " +
        "this shared machine likely reclaimed it as stale (e.g. this " +
        "process stalled long enough for its heartbeat to look " +
        "abandoned) and is now using it for its own server. Aborting " +
        "before running any test against a port this invocation can no " +
        "longer prove exclusive ownership of; rerun once contention " +
        "clears.",
    );
  }
}
