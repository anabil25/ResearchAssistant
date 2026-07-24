import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Injectable dependencies for the ephemeral-port file lock, so tests can
 * exercise the claim/reclaim logic deterministically without needing real
 * concurrent OS processes or race timing.
 */
export interface PortLockDeps {
  lockDir: string;
  pid: number;
  isProcessAlive: (pid: number) => boolean;
}

export function defaultPortLockDeps(
  lockDir: string = join(tmpdir(), "research-assistant-playwright-port-locks"),
): PortLockDeps {
  return {
    lockDir,
    pid: process.pid,
    isProcessAlive: (pid: number) => {
      try {
        // Signal 0 sends nothing but throws if no such process exists --
        // the standard portable way to check liveness without actually
        // signaling the target process.
        process.kill(pid, 0);
        return true;
      } catch {
        return false;
      }
    },
  };
}

function lockPathFor(deps: PortLockDeps, port: number): string {
  return join(deps.lockDir, `${port}.lock`);
}

/**
 * True if `port` is currently claimed by a lock file whose recorded owner
 * PID is still alive. False if there is no lock file, or the lock file
 * names a PID that has since exited -- a stale lock left behind by a prior
 * invocation that did not clean up (e.g. it crashed), which is safe to
 * reclaim rather than treat as a live collision.
 */
export function isPortLockHeld(deps: PortLockDeps, port: number): boolean {
  const lockPath = lockPathFor(deps, port);
  if (!existsSync(lockPath)) {
    return false;
  }
  let ownerPid: number;
  try {
    ownerPid = Number(readFileSync(lockPath, "utf8").trim());
  } catch {
    // Lock file vanished (e.g. concurrent cleanup) between existsSync and
    // readFileSync -- treat as not held rather than erroring.
    return false;
  }
  return (
    Number.isInteger(ownerPid) && ownerPid > 0 && deps.isProcessAlive(ownerPid)
  );
}

/**
 * Attempt to atomically claim `port` for this process by writing a lock
 * file naming `deps.pid`. This is what closes the residual TOCTOU window
 * between the OS reporting a port free (via a `listen(0)` probe) and this
 * invocation's own webServer actually binding it: a *second*, concurrent
 * invocation of this same config on the same shared machine (e.g. another
 * worktree/session running `test:e2e` at the same moment) will see this
 * lock and pick a different port instead of also selecting the one this
 * invocation already claimed, even though the OS would happily hand the
 * same freed port to both processes' near-simultaneous probes.
 *
 * Returns true (and creates/overwrites the lock file) if the port is
 * unclaimed or its existing lock belongs to a process that is no longer
 * alive. Returns false without modifying anything if a still-live process
 * already holds this exact port's lock.
 */
export function tryClaimPortLock(deps: PortLockDeps, port: number): boolean {
  mkdirSync(deps.lockDir, { recursive: true });
  if (isPortLockHeld(deps, port)) {
    return false;
  }
  const lockPath = lockPathFor(deps, port);
  const fd = openSync(lockPath, "w");
  closeSync(fd);
  writeFileSync(lockPath, String(deps.pid));
  return true;
}

/** Release a previously claimed lock. Best-effort: a leftover lock from an
 * abnormal exit is safely reclaimed later by `tryClaimPortLock`'s
 * live-process check, so a failure here is not fatal. */
export function releasePortLock(deps: PortLockDeps, port: number): void {
  try {
    rmSync(lockPathFor(deps, port), { force: true });
  } catch {
    // Best-effort; see doc comment above.
  }
}
