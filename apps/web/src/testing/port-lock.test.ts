import { spawn } from "node:child_process";
import {
  mkdtempSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  defaultPortLockDeps,
  isPortLockHeld,
  lockBelongsToInvocation,
  releasePortLock,
  touchPortLock,
  tryClaimPortLock,
  verifyLockIdentity,
  type PortLockDeps,
  type PortLockRecord,
} from "./port-lock";

// `port-lock.ts` destructures `openSync`/`rmSync` as named imports from
// "node:fs" at module-load time, so a post-hoc `jest.spyOn` on a separately
// re-imported module namespace object in this test file would never be
// observed by port-lock.ts's own calls (different binding). `jest.mock`
// intercepts at require/resolution time instead, so both this test file's
// own `openSync`/`rmSync`/`readFileSync` imports above and port-lock.ts's
// internal ones resolve to the exact same mocked function references.
jest.mock("node:fs", () => {
  const actual = jest.requireActual<typeof import("node:fs")>("node:fs");
  return {
    ...actual,
    openSync: jest.fn(actual.openSync),
    rmSync: jest.fn(actual.rmSync),
    readFileSync: jest.fn(actual.readFileSync),
    writeFileSync: jest.fn(actual.writeFileSync),
    renameSync: jest.fn(actual.renameSync),
  };
});

const mockOpenSync = openSync as unknown as jest.Mock;
const mockRmSync = rmSync as unknown as jest.Mock;
const mockReadFileSync = readFileSync as unknown as jest.Mock;
const mockWriteFileSync = writeFileSync as unknown as jest.Mock;
const mockRenameSync = renameSync as unknown as jest.Mock;
const actualFs = jest.requireActual<typeof import("node:fs")>("node:fs");

let nonceCounter = 0;

function makeDeps(overrides: Partial<PortLockDeps> = {}): {
  deps: PortLockDeps;
  cleanup: () => void;
} {
  const lockDir = mkdtempSync(join(tmpdir(), "port-lock-test-"));
  nonceCounter += 1;
  const deps: PortLockDeps = {
    lockDir,
    pid: 4242,
    nonce: `test-nonce-${nonceCounter}`,
    worktreeRoot: "/fake/worktree-root",
    isProcessAlive: () => true,
    now: () => 1_000_000,
    heartbeatStaleMs: 45_000,
    ...overrides,
  };
  return {
    deps,
    cleanup: () => rmSync(lockDir, { recursive: true, force: true }),
  };
}

/** Seed a lock file with a well-formed JSON record directly (bypassing
 * `tryClaimPortLock`), for tests that need to simulate a pre-existing lock
 * from some other invocation without going through the claim path. */
function writeRawRecord(
  lockPath: string,
  overrides: Partial<PortLockRecord> = {},
): void {
  const record: PortLockRecord = {
    pid: 999,
    nonce: "seed-nonce",
    worktreeRoot: "/seed/worktree-root",
    ports: [],
    claimedAt: 0,
    heartbeatAt: 0,
    ...overrides,
  };
  writeFileSync(lockPath, JSON.stringify(record));
}

describe("port-lock", () => {
  it("does not delete a replacement lock when another invocation reclaims the port between the staleness judgement and the takeover", () => {
    // The exact read-then-delete race. Invocation B reads a stale lock,
    // judges it reclaimable, and is about to delete it -- but invocation C
    // completes its own legitimate takeover in that window. The old code
    // deleted C's brand-new lock and then created its own, leaving B and C
    // both convinced they owned the port and both serving on it.
    //
    // The interleaving is injected at the precise race point rather than
    // hoped for: the hook fires when B exclusively creates its mutation
    // token, which is the first thing B does after judging the lock stale.
    const { deps, cleanup } = makeDeps({
      pid: 777,
      isProcessAlive: (pid: number) => pid !== 999, // the seeded owner is dead
      now: () => 1_000_000,
    });
    const lockPath = join(deps.lockDir, "53500.lock");
    try {
      writeRawRecord(lockPath, { pid: 999, nonce: "dead-owner", heartbeatAt: 0 });

      let injected = false;
      mockOpenSync.mockImplementation(
        (target: unknown, flags: unknown, ...rest: unknown[]) => {
          if (
            !injected &&
            typeof target === "string" &&
            target.endsWith("53500.lock.mut")
          ) {
            injected = true;
            // Invocation C's completed takeover: a live owner with a fresh
            // heartbeat now holds this exact port.
            actualFs.rmSync(lockPath, { force: true });
            writeRawRecord(lockPath, {
              pid: 555,
              nonce: "live-replacement",
              heartbeatAt: 1_000_000,
            });
          }
          return actualFs.openSync(
            target as never,
            flags as never,
            ...(rest as never[]),
          );
        },
      );

      expect(tryClaimPortLock(deps, 53500)).toBe(false);

      // C's lock survives, byte-for-byte, and still names C.
      const surviving = JSON.parse(
        actualFs.readFileSync(lockPath, "utf8"),
      ) as PortLockRecord;
      expect(surviving.nonce).toBe("live-replacement");
      expect(surviving.pid).toBe(555);
    } finally {
      mockOpenSync.mockImplementation(actualFs.openSync);
      cleanup();
    }
  });

  it("does not delete a successor's lock when a superseded owner reclaims a stale-looking record that changed underneath it", () => {
    // The subtler half: the record is still stale-*looking* under the token
    // (its owner is dead and its heartbeat is old), but it is no longer the
    // same record we judged -- a different invocation's takeover landed
    // first. Deleting it would silently steal that invocation's claim.
    const { deps, cleanup } = makeDeps({
      pid: 777,
      isProcessAlive: () => false, // every recorded owner looks dead
      now: () => 1_000_000,
    });
    const lockPath = join(deps.lockDir, "53600.lock");
    try {
      writeRawRecord(lockPath, { pid: 999, nonce: "first-owner", heartbeatAt: 0 });

      let injected = false;
      mockOpenSync.mockImplementation(
        (target: unknown, flags: unknown, ...rest: unknown[]) => {
          if (
            !injected &&
            typeof target === "string" &&
            target.endsWith("53600.lock.mut")
          ) {
            injected = true;
            actualFs.rmSync(lockPath, { force: true });
            writeRawRecord(lockPath, {
              pid: 888,
              nonce: "second-owner",
              heartbeatAt: 0,
            });
          }
          return actualFs.openSync(
            target as never,
            flags as never,
            ...(rest as never[]),
          );
        },
      );

      expect(tryClaimPortLock(deps, 53600)).toBe(false);
      const surviving = JSON.parse(
        actualFs.readFileSync(lockPath, "utf8"),
      ) as PortLockRecord;
      expect(surviving.nonce).toBe("second-owner");
    } finally {
      mockOpenSync.mockImplementation(actualFs.openSync);
      cleanup();
    }
  });

  it("releasePortLock leaves a successor's lock intact instead of deleting it on the way out", () => {
    // A superseded invocation exiting must not free a port a live invocation
    // is actively serving on. The old unconditional delete did exactly that,
    // and the freed port could then be handed to a third invocation while the
    // successor was still bound to it.
    const { deps, cleanup } = makeDeps({ pid: 4242, nonce: "original-owner" });
    const lockPath = join(deps.lockDir, "53700.lock");
    try {
      expect(tryClaimPortLock(deps, 53700)).toBe(true);

      // A concurrent invocation reclaimed the port while we stalled.
      actualFs.rmSync(lockPath, { force: true });
      writeRawRecord(lockPath, { pid: 5555, nonce: "successor", heartbeatAt: 0 });

      releasePortLock(deps, 53700);

      const surviving = JSON.parse(
        actualFs.readFileSync(lockPath, "utf8"),
      ) as PortLockRecord;
      expect(surviving.nonce).toBe("successor");
      expect(surviving.pid).toBe(5555);
    } finally {
      cleanup();
    }
  });

  it("releasePortLock still clears an unparseable lock file left at our own port", () => {
    // Ownership cannot be proven either way for corrupt content, and leaving
    // it behind would block the port until the staleness window elapsed. It
    // is never a provable live claim, so clearing it is safe.
    const { deps, cleanup } = makeDeps({ pid: 4242 });
    const lockPath = join(deps.lockDir, "53800.lock");
    try {
      expect(tryClaimPortLock(deps, 53800)).toBe(true);
      writeFileSync(lockPath, "{ not json");

      releasePortLock(deps, 53800);

      expect(actualFs.existsSync(lockPath)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("refuses to mutate a lock while another invocation holds that port's mutation token", () => {
    // Serialization itself: while a live invocation is mid-mutation, every
    // other mutation on that exact port declines rather than interleaving.
    const { deps, cleanup } = makeDeps({ pid: 4242, isProcessAlive: () => true });
    const tokenPath = join(deps.lockDir, "53900.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 53900)).toBe(true);
      writeFileSync(
        tokenPath,
        JSON.stringify({ pid: 6001, nonce: "other", acquiredAt: 1_000_000 }),
      );

      expect(touchPortLock(deps, 53900)).toBe(false);
      // Our own lock is untouched, and still ours.
      expect(verifyLockIdentity(deps, 53900)).toBe(true);
      // The foreign token is left strictly alone.
      expect(actualFs.existsSync(tokenPath)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("reclaims a mutation token abandoned by a dead process so one crash cannot wedge a port forever", () => {
    const { deps, cleanup } = makeDeps({
      pid: 4242,
      isProcessAlive: (pid: number) => pid !== 6002, // the token holder crashed
    });
    const tokenPath = join(deps.lockDir, "54000.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54000)).toBe(true);
      writeFileSync(
        tokenPath,
        JSON.stringify({ pid: 6002, nonce: "crashed", acquiredAt: 1_000_000 }),
      );

      expect(touchPortLock(deps, 54000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("reclaims a mutation token whose holder is alive but has been stalled past the staleness window", () => {
    const { deps, cleanup } = makeDeps({
      pid: 4242,
      isProcessAlive: () => true,
      now: () => 1_000_000,
      heartbeatStaleMs: 45_000,
    });
    const tokenPath = join(deps.lockDir, "54100.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54100)).toBe(true);
      writeFileSync(
        tokenPath,
        JSON.stringify({
          pid: 6003,
          nonce: "stalled",
          acquiredAt: 1_000_000 - 45_001,
        }),
      );

      expect(touchPortLock(deps, 54100)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("reclaims a mutation token with unparseable content", () => {
    const { deps, cleanup } = makeDeps({ pid: 4242, isProcessAlive: () => true });
    const tokenPath = join(deps.lockDir, "54200.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54200)).toBe(true);
      writeFileSync(tokenPath, "{ not json at all");

      expect(touchPortLock(deps, 54200)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("loses the atomic rename race for an abandoned mutation token rather than proceeding alongside the winner", () => {
    const { deps, cleanup } = makeDeps({
      pid: 4242,
      isProcessAlive: (pid: number) => pid !== 6004,
    });
    const tokenPath = join(deps.lockDir, "54300.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54300)).toBe(true);
      writeFileSync(
        tokenPath,
        JSON.stringify({ pid: 6004, nonce: "crashed", acquiredAt: 0 }),
      );
      // Another reclaimer's rename landed first, so ours fails with ENOENT.
      mockRenameSync.mockImplementationOnce(() => {
        throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
      });

      expect(touchPortLock(deps, 54300)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("lockBelongsToInvocation matches on nonce alone so a worker process (different pid, same invocation) recognizes its own orchestrator's claim", () => {
    // One Playwright invocation spans the orchestrator plus every worker it
    // spawns. They share a nonce but necessarily have different pids, so a
    // worker re-evaluating the config and asking "do we already own this
    // port?" must not be answered with a pid-exact comparison -- doing so
    // made every worker conclude the port was foreign and abort the run.
    const { deps: orchestrator, cleanup } = makeDeps({
      pid: 9100,
      nonce: "shared-invocation-nonce",
    });
    try {
      expect(tryClaimPortLock(orchestrator, 55000)).toBe(true);

      const worker: PortLockDeps = { ...orchestrator, pid: 9101 };
      expect(lockBelongsToInvocation(worker, 55000)).toBe(true);
      // The stricter, pid-exact handshake check still (correctly) says no:
      // it answers a different question, asked only by the orchestrator.
      expect(verifyLockIdentity(worker, 55000)).toBe(false);
      expect(verifyLockIdentity(orchestrator, 55000)).toBe(true);

      // A genuinely foreign invocation is rejected by both.
      const foreign: PortLockDeps = { ...orchestrator, nonce: "other-invocation" };
      expect(lockBelongsToInvocation(foreign, 55000)).toBe(false);
      expect(verifyLockIdentity(foreign, 55000)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("lockBelongsToInvocation reports false when no lock file exists at all", () => {
    const { deps, cleanup } = makeDeps();
    try {
      expect(lockBelongsToInvocation(deps, 55100)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("treats a mutation token with valid JSON but the wrong shape as abandoned rather than trusting it", () => {
    // A legacy/corrupt token that happens to parse must not be able to wedge
    // a port: it names no provable live holder, so it is reclaimable.
    const { deps, cleanup } = makeDeps({ pid: 4242, isProcessAlive: () => true });
    const tokenPath = join(deps.lockDir, "54400.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54400)).toBe(true);
      writeFileSync(tokenPath, JSON.stringify({ pid: "not-a-number" }));

      expect(touchPortLock(deps, 54400)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("leaves a mutation token alone at release time when it is no longer ours", () => {
    // If our token was reclaimed as abandoned while we were mid-mutation, it
    // now belongs to another invocation's in-progress mutation. Deleting it
    // on our way out would hand a third process a simultaneous mutation
    // window -- exactly the race the token exists to prevent.
    const { deps, cleanup } = makeDeps({ pid: 4242, isProcessAlive: () => true });
    const tokenPath = join(deps.lockDir, "54500.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54500)).toBe(true);

      // Swap in a foreign token exactly while our own mutation is running.
      mockWriteFileSync.mockImplementation((target: unknown, data: unknown) => {
        const result = actualFs.writeFileSync(target as never, data as never);
        if (typeof target === "string" && target.endsWith("54500.lock")) {
          actualFs.writeFileSync(
            tokenPath,
            JSON.stringify({ pid: 7001, nonce: "foreign", acquiredAt: 1_000_000 }),
          );
        }
        return result;
      });

      expect(touchPortLock(deps, 54500)).toBe(true);

      const surviving = JSON.parse(
        actualFs.readFileSync(tokenPath, "utf8"),
      ) as { nonce: string };
      expect(surviving.nonce).toBe("foreign");
    } finally {
      mockWriteFileSync.mockImplementation(actualFs.writeFileSync);
      cleanup();
    }
  });

  it("treats a vanished mutation token as absent rather than throwing", () => {
    // The token file can disappear between operations (another invocation
    // reclaiming it, or a cleanup sweep). Reading it must degrade to "no
    // token" rather than propagating ENOENT out of a release path.
    const { deps, cleanup } = makeDeps({ pid: 4242, isProcessAlive: () => true });
    const tokenPath = join(deps.lockDir, "54600.lock.mut");
    try {
      expect(tryClaimPortLock(deps, 54600)).toBe(true);

      mockWriteFileSync.mockImplementation((target: unknown, data: unknown) => {
        const result = actualFs.writeFileSync(target as never, data as never);
        if (typeof target === "string" && target.endsWith("54600.lock")) {
          actualFs.rmSync(tokenPath, { force: true });
        }
        return result;
      });

      expect(touchPortLock(deps, 54600)).toBe(true);
      expect(actualFs.existsSync(tokenPath)).toBe(false);
    } finally {
      mockWriteFileSync.mockImplementation(actualFs.writeFileSync);
      cleanup();
    }
  });

  it("claims an unclaimed port", () => {
    const { deps, cleanup } = makeDeps();
    try {
      expect(isPortLockHeld(deps, 51000)).toBe(false);
      expect(tryClaimPortLock(deps, 51000)).toBe(true);
      expect(isPortLockHeld(deps, 51000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("refuses to claim a port already held by a live process (the real concurrent-invocation race)", () => {
    const { deps: firstInvocation, cleanup } = makeDeps({
      pid: 111,
      isProcessAlive: () => true,
    });
    try {
      expect(tryClaimPortLock(firstInvocation, 52000)).toBe(true);

      // A second, concurrent invocation on the same machine (different pid,
      // same lock directory) tries to claim the exact same port the OS
      // handed both of them in the TOCTOU window -- it must lose.
      const secondInvocation: PortLockDeps = {
        ...firstInvocation,
        pid: 222,
        isProcessAlive: (pid: number) => pid === 111, // only the first pid is alive
      };
      expect(tryClaimPortLock(secondInvocation, 52000)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("reclaims a stale lock left by a process that is no longer alive", () => {
    const { deps: crashedInvocation, cleanup } = makeDeps({
      pid: 999,
      isProcessAlive: () => true,
    });
    try {
      expect(tryClaimPortLock(crashedInvocation, 53000)).toBe(true);

      // The owning process crashed without releasing the lock. A later
      // invocation must be able to reclaim the port rather than treat the
      // leftover lock file as a permanent, live collision.
      const laterInvocation: PortLockDeps = {
        ...crashedInvocation,
        pid: 1000,
        isProcessAlive: () => false, // pid 999 no longer exists
      };
      expect(isPortLockHeld(laterInvocation, 53000)).toBe(false);
      expect(tryClaimPortLock(laterInvocation, 53000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("releasePortLock clears a claim so a later attempt (even simulating the same live pid) succeeds cleanly", () => {
    const { deps, cleanup } = makeDeps();
    try {
      expect(tryClaimPortLock(deps, 54000)).toBe(true);
      releasePortLock(deps, 54000);
      expect(isPortLockHeld(deps, 54000)).toBe(false);
      expect(tryClaimPortLock(deps, 54000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("treats two distinct ports independently", () => {
    const { deps, cleanup } = makeDeps();
    try {
      expect(tryClaimPortLock(deps, 55000)).toBe(true);
      expect(isPortLockHeld(deps, 55001)).toBe(false);
      expect(tryClaimPortLock(deps, 55001)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("rethrows an unexpected (non-EEXIST) filesystem error from the exclusive create instead of silently reporting a lost race", () => {
    const { deps, cleanup } = makeDeps();
    try {
      mockOpenSync.mockImplementationOnce(() => {
        const error = new Error("disk full") as NodeJS.ErrnoException;
        error.code = "ENOSPC";
        throw error;
      });
      expect(() => tryClaimPortLock(deps, 56000)).toThrow("disk full");
    } finally {
      cleanup();
    }
  });

  it("loses a genuine simultaneous reclaim race: a concurrent creator wins the atomic re-create between this invocation's stale-lock unlink and its own retry", () => {
    // Regression for the exact TOCTOU gap the prior check-then-write
    // implementation had: reclaiming a stale lock is itself a two-step
    // sequence (unlink, then re-create). This proves the *final* atomic
    // `openSync(path, "wx")` -- not the earlier staleness check -- is what
    // actually decides the winner when two invocations race to reclaim the
    // same stale lock at the same instant.
    const { deps, cleanup } = makeDeps({
      pid: 1,
      isProcessAlive: () => false, // the existing lock's owner is dead
    });
    const lockPath = join(deps.lockDir, "57000.lock");
    try {
      writeRawRecord(lockPath, { pid: 999 }); // seed the stale lock

      mockRmSync.mockImplementationOnce(() => {
        // Simulate a second, genuinely concurrent invocation completing
        // its own reclaim (unlink + re-create) in the exact instant
        // between this invocation's unlink and its own retry.
        writeRawRecord(lockPath, { pid: 555, nonce: "concurrent-winner" });
      });
      expect(tryClaimPortLock(deps, 57000)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("still completes a reclaim when the unlink call itself errors but the file is actually gone (e.g. a concurrent cleanup already removed it)", () => {
    const { deps, cleanup } = makeDeps({
      pid: 2,
      isProcessAlive: () => false,
    });
    const lockPath = join(deps.lockDir, "58000.lock");
    try {
      writeRawRecord(lockPath, { pid: 888 });

      mockRmSync.mockImplementationOnce(
        (path: string, options?: import("node:fs").RmOptions) => {
          jest
            .requireActual<typeof import("node:fs")>("node:fs")
            .rmSync(path, options);
          throw new Error("simulated concurrent cleanup race");
        },
      );
      expect(tryClaimPortLock(deps, 58000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("under genuine simultaneous OS-process concurrency (not just sequential/mocked calls), exactly one of many real processes racing to create the same lock path wins", async () => {
    // The unit-level tests above prove the algorithm's branches using
    // mocks/sequencing within a single process. This test instead spawns
    // many independent real `node` processes and has them all attempt the
    // exact same exclusive-create primitive `tryClaimPortLock` relies on
    // (`openSync(path, "wx")`, treating `EEXIST` as "lost the race") against
    // the identical lock path at the same instant, proving the OS-level
    // atomicity guarantee actually holds across real process boundaries --
    // not just within this single Node process's synchronous call stack.
    const { deps, cleanup } = makeDeps();
    const lockPath = join(deps.lockDir, "59000.lock");
    const CONCURRENT_PROCESSES = 12;

    // A minimal, dependency-free child script mirroring exactly the
    // exclusive-create primitive from `exclusiveCreateLock` in port-lock.ts.
    const childScript = `
      const { openSync, writeFileSync, closeSync } = require("node:fs");
      const path = process.argv[1]; // argv[0] is execPath; -e has no script-path slot
      try {
        const fd = openSync(path, "wx");
        writeFileSync(fd, String(process.pid));
        closeSync(fd);
        process.exit(0); // won the race
      } catch (error) {
        if (error && error.code === "EEXIST") {
          process.exit(1); // lost the race, as expected
        }
        process.exit(2); // unexpected error
      }
    `;

    const runChild = () =>
      new Promise<number>((resolve, reject) => {
        const child = spawn(process.execPath, ["-e", childScript, lockPath]);
        child.on("error", reject);
        child.on("exit", (code) => resolve(code ?? -1));
      });

    try {
      const results = await Promise.all(
        Array.from({ length: CONCURRENT_PROCESSES }, () => runChild()),
      );
      const winners = results.filter((code) => code === 0);
      const losers = results.filter((code) => code === 1);
      const unexpected = results.filter((code) => code !== 0 && code !== 1);

      expect(unexpected).toEqual([]);
      expect(winners.length).toBe(1);
      expect(losers.length).toBe(CONCURRENT_PROCESSES - 1);
    } finally {
      cleanup();
    }
  });

  it("defaultPortLockDeps' real isProcessAlive reports true for this live process and false for a reserved/never-real pid", () => {
    const deps = defaultPortLockDeps(
      mkdtempSync(join(tmpdir(), "port-lock-default-deps-test-")),
    );
    try {
      expect(deps.pid).toBe(process.pid);
      // This process is, definitionally, alive.
      expect(deps.isProcessAlive(process.pid)).toBe(true);
      // A PID far outside any real process-table range never exists and
      // reliably makes `process.kill(pid, 0)` throw `ESRCH` on POSIX and
      // Windows alike (unlike e.g. PID 0, which Windows treats as a valid,
      // non-throwing signal target), exercising the `catch` branch's
      // `false` result.
      expect(deps.isProcessAlive(999_999_999)).toBe(false);
    } finally {
      rmSync(deps.lockDir, { recursive: true, force: true });
    }
  });

  it("defaultPortLockDeps carries a non-empty per-invocation nonce, a worktree root, and a positive heartbeat staleness threshold", () => {
    const originalNonceEnv = process.env.PLAYWRIGHT_PORT_LOCK_NONCE;
    delete process.env.PLAYWRIGHT_PORT_LOCK_NONCE;
    try {
      const deps = defaultPortLockDeps(
        mkdtempSync(join(tmpdir(), "port-lock-default-deps-nonce-test-")),
      );
      try {
        expect(typeof deps.nonce).toBe("string");
        expect(deps.nonce.length).toBeGreaterThan(0);
        expect(deps.worktreeRoot).toBe(process.cwd());
        expect(deps.heartbeatStaleMs).toBeGreaterThan(0);
        expect(typeof deps.now()).toBe("number");

        // A second call within the same process must reuse the exact same
        // nonce (memoized via the environment) rather than minting a fresh
        // one -- otherwise the later identity handshake in globalSetup
        // would always fail, since it would compare against a nonce
        // different from the one actually written into the lock file at
        // claim time.
        const second = defaultPortLockDeps(deps.lockDir);
        expect(second.nonce).toBe(deps.nonce);
      } finally {
        rmSync(deps.lockDir, { recursive: true, force: true });
      }
    } finally {
      if (originalNonceEnv === undefined) {
        delete process.env.PLAYWRIGHT_PORT_LOCK_NONCE;
      } else {
        process.env.PLAYWRIGHT_PORT_LOCK_NONCE = originalNonceEnv;
      }
    }
  });

  it("defaultPortLockDeps defaults its lock directory to a path under the OS temp dir when none is supplied", () => {
    const deps = defaultPortLockDeps();
    expect(deps.lockDir.startsWith(tmpdir())).toBe(true);
    expect(deps.lockDir).toContain("research-assistant-playwright-port-locks");
  });

  it("isPortLockHeld treats a lock file that vanishes between existsSync and readFileSync as not held", () => {
    const { deps, cleanup } = makeDeps();
    const lockPath = join(deps.lockDir, "60000.lock");
    try {
      writeFileSync(lockPath, "123");
      mockReadFileSync.mockImplementationOnce(() => {
        // Simulate a concurrent release/cleanup racing between this
        // function's own `existsSync` check (which just saw the file) and
        // its `readFileSync` call.
        const error = new Error("ENOENT: no such file or directory") as NodeJS.ErrnoException;
        error.code = "ENOENT";
        throw error;
      });
      expect(isPortLockHeld(deps, 60000)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("treats a malformed or legacy plain-PID lock file as unheld (safe to reclaim) rather than throwing", () => {
    const { deps, cleanup } = makeDeps({ isProcessAlive: () => true });
    const lockPath = join(deps.lockDir, "67000.lock");
    try {
      writeFileSync(lockPath, "999"); // legacy plain-PID format, not JSON
      expect(isPortLockHeld(deps, 67000)).toBe(false);
      expect(tryClaimPortLock(deps, 67000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("treats genuinely unparseable JSON content (distinct from the merely-wrong-shape plain-PID case above) as unheld rather than throwing", () => {
    const { deps, cleanup } = makeDeps({ isProcessAlive: () => true });
    const lockPath = join(deps.lockDir, "68000.lock");
    try {
      // "999" above is valid JSON (a bare number) that fails the shape
      // check; this is invalid JSON syntax entirely, exercising
      // readLockRecord's `JSON.parse` `catch` branch specifically, not its
      // shape-validation branch.
      writeFileSync(lockPath, "{ this is not valid json at all");
      expect(isPortLockHeld(deps, 68000)).toBe(false);
      expect(tryClaimPortLock(deps, 68000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("touchPortLock returns false (rather than throwing) when the heartbeat rewrite itself fails, e.g. the lock directory vanished out from under it", () => {
    const { deps, cleanup } = makeDeps({ pid: 14_000 });
    try {
      expect(tryClaimPortLock(deps, 69000)).toBe(true);
      // Target the heartbeat rewrite specifically. `touchPortLock` now runs
      // under a per-port mutation token, so the first write it performs is
      // the token's own exclusive create (to a file descriptor); a blanket
      // `mockImplementationOnce` would fail that instead and never reach the
      // rewrite under test. Fail only the write addressed to the lock file
      // itself, and let every other write through to the real implementation.
      mockWriteFileSync.mockImplementation((target: unknown, data: unknown) => {
        if (typeof target === "string" && target.endsWith(".lock")) {
          throw new Error("ENOENT: no such file or directory");
        }
        return actualFs.writeFileSync(target as never, data as never);
      });
      expect(touchPortLock(deps, 69000)).toBe(false);
    } finally {
      mockWriteFileSync.mockImplementation(actualFs.writeFileSync);
      cleanup();
    }
  });

  it("treats a lock as stale (reclaimable) once its heartbeat is older than heartbeatStaleMs, even though its PID number is technically still alive -- the PID-reuse gap a pure liveness check misses", () => {
    let currentTime = 0;
    const { deps: firstInvocation, cleanup } = makeDeps({
      pid: 4242,
      isProcessAlive: () => true, // stays "alive" for the whole test
      now: () => currentTime,
      heartbeatStaleMs: 45_000,
    });
    try {
      expect(tryClaimPortLock(firstInvocation, 61000)).toBe(true);

      // A much later invocation's clock shows the heartbeat is far beyond
      // the staleness window -- e.g. this exact PID number was reused by a
      // completely unrelated process after the real claimant crashed or
      // stalled without releasing its lock.
      currentTime = 100_000; // 100s later, > 45s heartbeatStaleMs
      const laterInvocation: PortLockDeps = {
        ...firstInvocation,
        pid: 5000,
        nonce: "later-invocation-nonce",
      };
      expect(isPortLockHeld(laterInvocation, 61000)).toBe(false);
      expect(tryClaimPortLock(laterInvocation, 61000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("touchPortLock refreshes the heartbeat so a live, still-owning invocation is never mistaken for stale", () => {
    let currentTime = 0;
    const { deps, cleanup } = makeDeps({
      pid: 7000,
      isProcessAlive: () => true,
      now: () => currentTime,
      heartbeatStaleMs: 45_000,
    });
    try {
      expect(tryClaimPortLock(deps, 62000)).toBe(true);

      currentTime = 40_000; // still within the staleness window
      expect(touchPortLock(deps, 62000)).toBe(true);

      // Without the touch above, 80_000ms since claimedAt=0 would exceed
      // the 45s threshold. With the heartbeat refreshed at 40_000, the age
      // relative to *that* refresh is only 40_000ms, still fresh.
      currentTime = 80_000;
      expect(isPortLockHeld(deps, 62000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("touchPortLock is a no-op that returns false when the lock no longer names this invocation (already reclaimed by a different owner)", () => {
    const { deps: original, cleanup } = makeDeps({ pid: 8000 });
    try {
      expect(tryClaimPortLock(original, 63000)).toBe(true);
      const impostor: PortLockDeps = {
        ...original,
        pid: 9000,
        nonce: "impostor-nonce",
      };
      expect(touchPortLock(impostor, 63000)).toBe(false);
      // The genuine owner's record is untouched by the impostor's failed
      // attempt -- it still verifies as owned by the original invocation.
      expect(verifyLockIdentity(original, 63000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("verifyLockIdentity confirms the current on-disk lock still names this exact invocation, and rejects a foreign nonce/pid or a not-yet-claimed port", () => {
    const { deps: owner, cleanup } = makeDeps({ pid: 10_000 });
    try {
      expect(verifyLockIdentity(owner, 64000)).toBe(false); // nothing claimed yet
      expect(tryClaimPortLock(owner, 64000)).toBe(true);
      expect(verifyLockIdentity(owner, 64000)).toBe(true);

      const foreign: PortLockDeps = {
        ...owner,
        pid: 11_000,
        nonce: "foreign-nonce",
      };
      expect(verifyLockIdentity(foreign, 64000)).toBe(false);
    } finally {
      cleanup();
    }
  });

  it("simulates the real foreign-takeover scenario the globalSetup identity handshake guards against: after a stale reclaim by a different invocation, the original claimant's verifyLockIdentity now correctly fails", () => {
    let currentTime = 0;
    const { deps: original, cleanup } = makeDeps({
      pid: 12_000,
      isProcessAlive: () => true,
      now: () => currentTime,
      heartbeatStaleMs: 45_000,
    });
    try {
      expect(tryClaimPortLock(original, 65000)).toBe(true);
      expect(verifyLockIdentity(original, 65000)).toBe(true);

      currentTime = 100_000; // original invocation stalled; heartbeat now stale
      const concurrentInvocation: PortLockDeps = {
        ...original,
        pid: 13_000,
        nonce: "concurrent-nonce",
      };
      expect(tryClaimPortLock(concurrentInvocation, 65000)).toBe(true);

      // The original invocation's own identity check -- exactly what
      // port-lock-handshake.ts's globalSetup performs after every local
      // webServer is confirmed healthy -- must now report it no longer
      // owns this port, so the run aborts loudly instead of silently
      // testing against a port a different invocation has since claimed.
      expect(verifyLockIdentity(original, 65000)).toBe(false);
      expect(verifyLockIdentity(concurrentInvocation, 65000)).toBe(true);
    } finally {
      cleanup();
    }
  });

  it("records the full set of this invocation's claimed ports (not just the single port each individual lock file represents) for diagnostics", () => {
    const { deps, cleanup } = makeDeps();
    try {
      expect(tryClaimPortLock(deps, 66000, [66000, 66001, 66002])).toBe(true);
      const raw = JSON.parse(
        readFileSync(join(deps.lockDir, "66000.lock"), "utf8"),
      ) as PortLockRecord;
      expect(raw.ports).toEqual([66000, 66001, 66002]);
      expect(raw.worktreeRoot).toBe(deps.worktreeRoot);
      expect(raw.nonce).toBe(deps.nonce);
      expect(raw.pid).toBe(deps.pid);
    } finally {
      cleanup();
    }
  });
});
