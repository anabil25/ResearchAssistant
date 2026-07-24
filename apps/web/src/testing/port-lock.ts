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
 * Attempt to atomically claim `port` for this process by creating a lock
 * file naming `deps.pid`. This closes the residual TOCTOU window between
 * the OS reporting a port free (via a `listen(0)` probe) and this
 * invocation's own webServer actually binding it: a *second*, concurrent
 * invocation of this same config on the same shared machine (e.g. another
 * worktree/session running `test:e2e` at the same moment) will see this
 * lock and pick a different port instead of also selecting the one this
 * invocation already claimed, even though the OS would happily hand the
 * same freed port to both processes' near-simultaneous probes.
 *
 * The claim itself uses `openSync(path, "wx")` -- Node's binding for the
 * POSIX/Win32 exclusive-create flag (`O_CREAT | O_EXCL`), which the
 * kernel guarantees is a single atomic operation: if two processes race
 * to create the same path this way, exactly one call succeeds and the
 * other fails with `EEXIST`, with no gap either process could observe
 * "not yet claimed" in between. This replaces a prior
 * check-then-non-exclusive-write sequence (`isPortLockHeld` followed by
 * a plain `"w"` open) that had exactly that gap: two simultaneous
 * claimants could both observe the port as unclaimed and both then
 * "successfully" write a lock file, each overwriting the other's PID.
 *
 * Returns true (and creates the lock file) if the port is unclaimed or
 * its existing lock belongs to a process that is no longer alive.
 * Returns false without modifying anything if a still-live process
 * already holds this exact port's lock -- including when this
 * invocation loses a genuine simultaneous race to reclaim a stale lock
 * (see the reclaim branch below).
 */
export function tryClaimPortLock(deps: PortLockDeps, port: number): boolean {
  mkdirSync(deps.lockDir, { recursive: true });
  const lockPath = lockPathFor(deps, port);

  if (exclusiveCreateLock(lockPath, deps.pid)) {
    return true;
  }

  // The lock file already exists. If its owner process is dead, this is a
  // stale lock left behind by a prior invocation that crashed/didn't clean
  // up -- reclaim it. A second, genuinely concurrent invocation may reach
  // this same branch at the same instant; both may unlink the stale file,
  // but only one of the two `exclusiveCreateLock` retries below can win
  // the atomic re-create, so the loser correctly reports the port as
  // held (by the winner) rather than both believing they own it.
  if (!isPortLockHeld(deps, port)) {
    try {
      rmSync(lockPath, { force: true });
    } catch {
      // Another process may already be mid-reclaim; fall through to the
      // exclusive re-create attempt below, which is the actual arbiter.
    }
    return exclusiveCreateLock(lockPath, deps.pid);
  }

  return false;
}

/**
 * Core atomic primitive behind `tryClaimPortLock`: create `lockPath`
 * exclusively and write `pid` into it in one uninterrupted sequence.
 * Returns false (without side effects) if the path already exists;
 * rethrows any other filesystem error.
 */
function exclusiveCreateLock(lockPath: string, pid: number): boolean {
  let fd: number;
  try {
    fd = openSync(lockPath, "wx");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      return false;
    }
    throw error;
  }
  try {
    writeFileSync(fd, String(pid));
  } finally {
    closeSync(fd);
  }
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
