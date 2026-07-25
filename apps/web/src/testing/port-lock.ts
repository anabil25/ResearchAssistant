import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
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

function mutationTokenPathFor(deps: PortLockDeps, port: number): string {
  return `${lockPathFor(deps, port)}.mut`;
}

/** Content of a per-port mutation token (see `withMutationToken`). */
interface MutationTokenRecord {
  pid: number;
  nonce: string;
  acquiredAt: number;
}

function readMutationToken(tokenPath: string): MutationTokenRecord | null {
  let raw: string;
  try {
    raw = readFileSync(tokenPath, "utf8");
  } catch {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      typeof (parsed as MutationTokenRecord).pid !== "number" ||
      typeof (parsed as MutationTokenRecord).nonce !== "string" ||
      typeof (parsed as MutationTokenRecord).acquiredAt !== "number"
    ) {
      return null;
    }
    return parsed as MutationTokenRecord;
  } catch {
    return null;
  }
}

/**
 * Try to take exclusive, cross-process ownership of the right to *mutate*
 * `port`'s lock file.
 *
 * Every operation that writes or deletes a lock file -- stale-lock takeover,
 * heartbeat refresh, and release -- runs while holding this token, which
 * makes the lock file itself stable for the duration of each operation and
 * lets each one safely re-verify ownership after acquiring it. Without that
 * serialization, each operation was an independent check-then-act:
 *
 * - takeover read the lock, judged it stale, then deleted it -- but a
 *   concurrent invocation could have reclaimed the port in between, so the
 *   delete destroyed a *newly live* owner's lock and both invocations then
 *   believed they owned the port;
 * - heartbeat verified the nonce matched, then wrote -- clobbering a
 *   replacement owner that appeared in the gap;
 * - release deleted unconditionally -- so an invocation whose claim had
 *   already been reclaimed deleted its successor's lock on the way out.
 *
 * The token is itself created with `openSync(path, "wx")` (`O_CREAT |
 * O_EXCL`), which the kernel guarantees is atomic: of two simultaneous
 * claimants exactly one succeeds and the other gets `EEXIST`. A token left
 * behind by a process that died mid-mutation is reclaimed only via
 * `renameSync`, which is likewise atomic -- of several racers that all judge
 * the same token abandoned, only one rename can succeed and the rest get
 * `ENOENT` -- so an abandoned token can never be "reclaimed" by two
 * processes at once either.
 */
function tryAcquireMutationToken(deps: PortLockDeps, port: number): boolean {
  const tokenPath = mutationTokenPathFor(deps, port);
  const token: MutationTokenRecord = {
    pid: deps.pid,
    nonce: deps.nonce,
    acquiredAt: deps.now(),
  };
  if (exclusiveCreateJson(tokenPath, token)) {
    return true;
  }

  const existing = readMutationToken(tokenPath);
  const abandoned =
    existing === null ||
    !(
      Number.isInteger(existing.pid) &&
      existing.pid > 0 &&
      deps.isProcessAlive(existing.pid)
    ) ||
    deps.now() - existing.acquiredAt > deps.heartbeatStaleMs;
  if (!abandoned) {
    return false;
  }

  // Atomic arbitration between concurrent reclaimers of an abandoned token.
  const quarantinePath = `${tokenPath}.${deps.nonce}.abandoned`;
  try {
    renameSync(tokenPath, quarantinePath);
  } catch {
    return false;
  }
  try {
    rmSync(quarantinePath, { force: true });
  } catch {
    // The quarantined copy is inert and uniquely named; leaving it behind
    // costs a stray file and nothing else.
  }
  return exclusiveCreateJson(tokenPath, token);
}

/** Release a mutation token, but only if it is still ours. A token we have
 * already lost (reclaimed as abandoned while we stalled) belongs to someone
 * else mid-mutation, and deleting it would hand a third process a
 * simultaneous mutation window -- reintroducing exactly the race the token
 * exists to prevent. */
function releaseMutationToken(deps: PortLockDeps, port: number): void {
  const tokenPath = mutationTokenPathFor(deps, port);
  const existing = readMutationToken(tokenPath);
  if (existing === null || existing.nonce !== deps.nonce || existing.pid !== deps.pid) {
    return;
  }
  try {
    rmSync(tokenPath, { force: true });
  } catch {
    // Best-effort: a leftover token is reclaimed as abandoned later.
  }
}

/** Run `mutate` while holding `port`'s mutation token, returning `fallback`
 * without side effects if the token cannot be acquired (another invocation is
 * mid-mutation on this exact port). */
function withMutationToken<T>(
  deps: PortLockDeps,
  port: number,
  fallback: T,
  mutate: () => T,
): T {
  if (!tryAcquireMutationToken(deps, port)) {
    return fallback;
  }
  try {
    return mutate();
  } finally {
    releaseMutationToken(deps, port);
  }
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
  const record = readLockRecord(lockPathFor(deps, port));
  return record !== null && recordIsLive(deps, record);
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

  // The lock file already exists. Judge it *before* taking the mutation
  // token, so the overwhelmingly common "port is genuinely in use" case costs
  // nothing and never touches the filesystem.
  const observed = readLockRecord(lockPath);
  if (observed !== null && recordIsLive(deps, observed)) {
    return false;
  }

  // It looks stale. Take the mutation token so no concurrent takeover,
  // heartbeat, or release can interleave with the delete-and-recreate below,
  // then re-verify under the token that the file is still the exact record we
  // judged stale. If it changed -- because the previous owner released it and
  // a third invocation legitimately claimed the port in the gap, or because
  // the "dead" owner turned out to be alive and heartbeated -- we must not
  // delete it: doing so would destroy a live owner's claim and leave two
  // invocations both believing they own this port.
  return withMutationToken(deps, port, false, () => {
    const current = readLockRecord(lockPath);
    if (current !== null) {
      if (recordIsLive(deps, current)) {
        return false;
      }
      if (observed !== null && !isSameLockRecord(current, observed)) {
        // A different (still stale-looking) record than the one we judged --
        // someone else's takeover landed first. Theirs is the current claim;
        // fail rather than immediately re-taking it.
        return false;
      }
    }
    // Reachable only when the path is absent (released in the meantime),
    // holds unparseable content (never a provable live claim, so safe to
    // reclaim), or still holds the exact stale record we judged. `rmSync`
    // with `force` is a no-op on an absent path, so the absent case falls
    // through to a plain exclusive create.
    try {
      rmSync(lockPath, { force: true });
    } catch {
      // Fall through: the exclusive re-create below is the actual arbiter.
    }
    return exclusiveCreateLock(lockPath, record);
  });
}

/** True if `record` names a live owner with a fresh heartbeat -- the same
 * judgement `isPortLockHeld` makes, factored out so a record that has already
 * been read can be re-judged without a second filesystem read (which would
 * reintroduce a check-then-act gap of its own). */
function recordIsLive(deps: PortLockDeps, record: PortLockRecord): boolean {
  if (
    !(
      Number.isInteger(record.pid) &&
      record.pid > 0 &&
      deps.isProcessAlive(record.pid)
    )
  ) {
    return false;
  }
  return deps.now() - record.heartbeatAt <= deps.heartbeatStaleMs;
}

/** Identity comparison for a lock record: same owner *and* same heartbeat
 * generation. The heartbeat is included deliberately -- a record whose
 * heartbeat advanced since we read it belongs to an owner that is demonstrably
 * alive, so it is no longer the abandoned record we judged. */
function isSameLockRecord(left: PortLockRecord, right: PortLockRecord): boolean {
  return (
    left.pid === right.pid &&
    left.nonce === right.nonce &&
    left.heartbeatAt === right.heartbeatAt
  );
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
  return exclusiveCreateJson(lockPath, record);
}

/** Create `path` exclusively (`O_CREAT | O_EXCL`) and write `value` as JSON
 * into it in one uninterrupted sequence. Returns false (without side effects)
 * if the path already exists; rethrows any other filesystem error. */
function exclusiveCreateJson(path: string, value: unknown): boolean {
  let fd: number;
  try {
    fd = openSync(path, "wx");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      return false;
    }
    throw error;
  }
  try {
    writeFileSync(fd, JSON.stringify(value));
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
  return withMutationToken(deps, port, false, () => {
    const lockPath = lockPathFor(deps, port);
    const record = readLockRecord(lockPath);
    // Re-verified *under the token*, so no takeover can land between this
    // check and the write below. Previously this was a plain check-then-write
    // and a concurrent takeover in that gap meant the refresh overwrote the
    // new, legitimate owner's record with our own stale identity.
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
  });
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

/**
 * True if `port`'s lock belongs to this *invocation* -- matching nonce only,
 * deliberately ignoring pid.
 *
 * One Playwright invocation spans several processes: the orchestrator that
 * claims the ports and starts the servers, plus every parallel worker it
 * spawns, each of which re-evaluates `playwright.config.ts` independently.
 * They share a nonce (memoized into `PLAYWRIGHT_PORT_LOCK_NONCE`, see
 * `defaultPortLockDeps`) but necessarily have different pids, so a worker
 * asking "does this invocation already own this port?" cannot use
 * `verifyLockIdentity` -- that comparison is pid-exact on purpose, because
 * the globalSetup handshake it serves runs in the orchestrator and wants the
 * strictest possible statement of ownership.
 */
export function lockBelongsToInvocation(deps: PortLockDeps, port: number): boolean {
  const record = readLockRecord(lockPathFor(deps, port));
  return record !== null && record.nonce === deps.nonce;
}

/** Release a previously claimed lock, but only if it is still genuinely ours.
 *
 * The unconditional delete this replaces was a real correctness bug at exit:
 * an invocation whose claim had already been reclaimed as stale (because it
 * stalled long enough for its heartbeat to lapse) would, on the way out,
 * delete its *successor's* lock file -- freeing a port that a live invocation
 * was actively serving on, and letting a third invocation claim it. Verifying
 * ownership under the mutation token makes release a no-op in exactly that
 * case.
 *
 * Best-effort in every other respect: a leftover lock from an abnormal exit
 * is safely reclaimed later by `tryClaimPortLock`'s staleness check, so
 * failing to acquire the token (another invocation is mid-mutation) is not
 * fatal either. */
export function releasePortLock(deps: PortLockDeps, port: number): void {
  withMutationToken<void>(deps, port, undefined, () => {
    const lockPath = lockPathFor(deps, port);
    const record = readLockRecord(lockPath);
    if (record !== null && (record.nonce !== deps.nonce || record.pid !== deps.pid)) {
      // Someone else's claim -- reclaimed from us while we were stalled.
      return;
    }
    try {
      rmSync(lockPath, { force: true });
    } catch {
      // Best-effort; see doc comment above.
    }
  });
}
