import { spawn } from "node:child_process";
import {
  mkdtempSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  defaultPortLockDeps,
  isPortLockHeld,
  releasePortLock,
  tryClaimPortLock,
  type PortLockDeps,
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
  };
});

const mockOpenSync = openSync as unknown as jest.Mock;
const mockRmSync = rmSync as unknown as jest.Mock;
const mockReadFileSync = readFileSync as unknown as jest.Mock;

function makeDeps(overrides: Partial<PortLockDeps> = {}): {
  deps: PortLockDeps;
  cleanup: () => void;
} {
  const lockDir = mkdtempSync(join(tmpdir(), "port-lock-test-"));
  const deps: PortLockDeps = {
    lockDir,
    pid: 4242,
    isProcessAlive: () => true,
    ...overrides,
  };
  return {
    deps,
    cleanup: () => rmSync(lockDir, { recursive: true, force: true }),
  };
}

describe("port-lock", () => {
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
      writeFileSync(lockPath, "999"); // seed the stale lock

      mockRmSync.mockImplementationOnce(() => {
        // Simulate a second, genuinely concurrent invocation completing
        // its own reclaim (unlink + re-create) in the exact instant
        // between this invocation's unlink and its own retry.
        writeFileSync(lockPath, "555");
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
      writeFileSync(lockPath, "888");

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
});
