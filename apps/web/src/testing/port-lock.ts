import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

/**
 * Persisted content of a claimed port's lock file. Beyond the owning PID
 * (which alone is an unreliable liveness signal -- see `isPortLockHeld` --
 * because OS PIDs are reused, so a totally unrelated later process can
 * coincidentally reuse a dead claimant's exact PID number), this carries:
 *
 * - `nonce`: a per-invocation random identity, independent of PID reuse,
 *   that lets `verifyLockIdentity` prove "the lock recorded as claimed by
 *   me is *still* the one I wrote" rather than merely "some process with
 *   my old PID number exists".
 * - `worktreeRoot`: which checkout/worktree claimed this port, purely for
 *   human diagnostics when investigating unexpected contention.
 * - `ports`: every port this same invocation has (so far) claimed, so one
 *   lock file's content shows the whole related set, not just itself.
 * - `claimedAt`/`heartbeatAt`: epoch milliseconds. `heartbeatAt` is
 *   refreshed periodically by the live owner (see `touchPortLock`); if it
 *   goes stale for longer than `heartbeatStaleMs` even though the owning
 *   PID number still (coincidentally) belongs to a live process, the lock
 *   is treated as abandoned and safely reclaimable.
 */
export interface PortLockRecord {
  pid: number;
  nonce: string;
  worktreeRoot: string;
  ports: number[];
  claimedAt: number;
  heartbeatAt: number;
}

/**
 * Injectable dependencies for the ephemeral-port file lock, so tests can
 * exercise the claim/reclaim logic deterministically without needing real
 * concurrent OS processes or race timing.
 */
export interface PortLockDeps {
  lockDir: string;
  pid: number;
  /** Unique per-invocation identity; see `PortLockRecord.nonce`. */
  nonce: string;
  worktreeRoot: string;
  isProcessAlive: (pid: number) => boolean;
  now: () => number;
  /** Heartbeat age (ms) beyond which a lock is stale even if its PID is alive. */
  heartbeatStaleMs: number;
}

const DEFAULT_HEARTBEAT_STALE_MS = 45_000;

export function defaultPortLockDeps(
  lockDir: string = join(tmpdir(), "research-assistant-playwright-port-locks"),
): PortLockDeps {
  return {
    lockDir,
    pid: process.pid,
    // Memoized into the environment (mirroring how playwright.config.ts
    // memoizes resolved ports) so every process that re-evaluates the
    // config within one Playwright invocation -- the top-level orchestrator
    // plus each parallel worker it spawns -- shares the exact same nonce
    // instead of each minting its own, which would make the later identity
    // handshake in globalSetup always fail (it compares against a freshly
    // generated nonce, not the one actually written into the lock file at
    // claim time).
    nonce: (() => {
      const existing = process.env.PLAYWRIGHT_PORT_LOCK_NONCE;
      if (existing) {
        return existing;
      }
      const generated = randomUUID();
      process.env.PLAYWRIGHT_PORT_LOCK_NONCE = generated;
      return generated;
    })(),
    worktreeRoot: process.cwd(),
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
    now: () => Date.now(),
    heartbeatStaleMs: DEFAULT_HEARTBEAT_STALE_MS,
  };
}

function lockPathFor(deps: PortLockDeps, port: number): string {
  return join(deps.lockDir, `${port}.lock`);
}

/**
 * Parse a lock file's content as a `PortLockRecord`. Returns null for any
 * unreadable or malformed content (vanished file, corrupt/legacy format)
 * rather than throwing -- an unparseable lock is treated the same as no
 * lock: safe to reclaim, never a live collision, since we cannot prove it
 * names a live, current owner.
 */
function readLockRecord(lockPath: string): PortLockRecord | null {
  let raw: string;
  try {
    raw = readFileSync(lockPath, "utf8");
  } catch {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as PortLockRecord).pid !== "number" ||
      typeof (parsed as PortLockRecord).nonce !== "string" ||
      typeof (parsed as PortLockRecord).heartbeatAt !== "number"
    ) {
      return null;
    }
    return parsed as PortLockRecord;
  } catch {
    return null;
  }
}

/**
 * True if `port` is currently claimed by a lock file that is both (a) owned
 * by a PID that is still alive, and (b) has a heartbeat newer than
 * `heartbeatStaleMs`. Either condition failing means the lock is safe to
 * reclaim:
 *
 * - Dead PID: the classic stale-lock case, a prior invocation that crashed
 *   or was killed without releasing its claim.
 * - Stale heartbeat despite a "live" PID: closes a real gap the pure
 *   liveness check misses -- OS PIDs get reused, so a genuinely dead
 *   claimant's PID number can, by the time this check runs, belong to a
 *   totally unrelated later process that happens to still be alive. A
 *   heartbeat that stopped advancing minutes ago is a much stronger
 *   abandonment signal than "no live process currently has this PID".
 */
export function isPortLockHeld(deps: PortLockDeps, port: number): boolean {
  const lockPath = lockPathFor(deps, port);
  const record = readLockRecord(lockPath);
  if (!record) {
    return false;
  }
  if (!(Number.isInteger(record.pid) && record.pid > 0 && deps.isProcessAlive(record.pid))) {
    return false;
  }
  const heartbeatAge = deps.now() - record.heartbeatAt;
  return heartbeatAge <= deps.heartbeatStaleMs;
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
export function tryClaimPortLock(
  deps: PortLockDeps,
  port: number,
  invocationPorts: readonly number[] = [port],
): boolean {
  mkdirSync(deps.lockDir, { recursive: true });
  const lockPath = lockPathFor(deps, port);
  const record = buildRecord(deps, invocationPorts);

  if (exclusiveCreateLock(lockPath, record)) {
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
    return exclusiveCreateLock(lockPath, record);
  }

  return false;
}

function buildRecord(
  deps: PortLockDeps,
  invocationPorts: readonly number[],
): PortLockRecord {
  const timestamp = deps.now();
  return {
    pid: deps.pid,
    nonce: deps.nonce,
    worktreeRoot: deps.worktreeRoot,
    ports: [...invocationPorts],
    claimedAt: timestamp,
    heartbeatAt: timestamp,
  };
}

/**
 * Core atomic primitive behind `tryClaimPortLock`: create `lockPath`
 * exclusively and write `record` (as JSON) into it in one uninterrupted
 * sequence. Returns false (without side effects) if the path already
 * exists; rethrows any other filesystem error.
 */
function exclusiveCreateLock(lockPath: string, record: PortLockRecord): boolean {
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
    writeFileSync(fd, JSON.stringify(record));
  } finally {
    closeSync(fd);
  }
  return true;
}

/**
 * Refresh the heartbeat of a lock this invocation currently owns, so
 * `isPortLockHeld` does not treat it as abandoned by another invocation
 * while this process is still actively using the port. Only rewrites the
 * file if the recorded `nonce` still matches this invocation's own nonce --
 * if it does not (because the lock was reclaimed as stale and re-claimed by
 * someone else in the meantime), this is a no-op that returns false rather
 * than clobbering the new, legitimate owner's record.
 */
export function touchPortLock(deps: PortLockDeps, port: number): boolean {
  const lockPath = lockPathFor(deps, port);
  const record = readLockRecord(lockPath);
  if (!record || record.nonce !== deps.nonce || record.pid !== deps.pid) {
    return false;
  }
  try {
    writeFileSync(
      lockPath,
      JSON.stringify({ ...record, heartbeatAt: deps.now() }),
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * The "identity handshake" step: confirm the lock file currently on disk
 * for `port` still records *this exact invocation* (matching nonce and
 * pid) as owner, not merely that some lock exists. Called from
 * `globalSetup` (see `port-lock-handshake.ts`) once Playwright has
 * confirmed every local `webServer` is actually up and answering its
 * health check, so a run only proceeds against ports this invocation can
 * still prove exclusive, current ownership of. Returns false if the lock
 * is missing, unparseable, or -- the specific foreign-takeover case this
 * guards against -- now names a different nonce/pid because a concurrent
 * invocation reclaimed it as stale (e.g. this process stalled long enough
 * for its own heartbeat to look abandoned) and claimed it for itself.
 */
export function verifyLockIdentity(deps: PortLockDeps, port: number): boolean {
  const record = readLockRecord(lockPathFor(deps, port));
  return record !== null && record.nonce === deps.nonce && record.pid === deps.pid;
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
