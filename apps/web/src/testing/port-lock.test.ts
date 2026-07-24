import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  isPortLockHeld,
  releasePortLock,
  tryClaimPortLock,
  type PortLockDeps,
} from "./port-lock";

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
});
