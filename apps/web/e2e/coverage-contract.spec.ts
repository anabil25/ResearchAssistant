import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";
import ts from "typescript";

import { UI_COVERAGE_MANIFEST } from "../src/testing/interaction-manifest";

// Bare `[pw.<alias>]` tokens: an interaction-level claim that at least one Playwright
// test exists for one of the ids literally listed in `interaction.testIds`. This is
// the original, still-valid convention and is unaffected by the fix below.
const PLAYWRIGHT_ID_PATTERN = /\[(pw\.[a-z0-9.-]+)\]/g;

// `[pw.<interaction-id>:<state>]` tokens: a truthful, per-state claim. `<interaction-id>`
// must be the exact `interaction.id` string from the manifest (e.g.
// "shell.navigation.close-mobile"), and `<state>` must be one of that interaction's
// declared `states`. This is the machine-checkable replacement for the deleted
// `playwrightStateTestIds` blanket derivation: instead of trusting a pre-computed
// mapping, the gate below scans real test titles and requires an explicit token for
// every (interaction, state) pair — no auto-assigning one test to every state.
const PLAYWRIGHT_STATE_TOKEN_PATTERN =
  /\[pw\.([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*):([a-z][a-z0-9-]*)\]/g;

interface SpecTokens {
  bareIds: Set<string>;
  statePairs: Set<string>;
  // "Trusted" = found in at least one test that is NOT skip-guarded. Only these
  // may satisfy a required (bare id / state) contract entry — a token that
  // appears exclusively inside a skip-guarded test (declaratively via
  // `test.skip("title", fn)`, or conditionally via the self-skip pattern
  // `test.skip(condition, reason)` as a statement inside the test body, e.g.
  // gated on an env var) must NOT be able to satisfy required coverage, since
  // the test backing it may never actually run.
  trustedBareIds: Set<string>;
  trustedStatePairs: Set<string>;
}

function isTestDotSkip(node: ts.Node): node is ts.CallExpression {
  return (
    ts.isCallExpression(node) &&
    ts.isPropertyAccessExpression(node.expression) &&
    node.expression.name.text === "skip" &&
    ts.isIdentifier(node.expression.expression) &&
    node.expression.expression.text === "test"
  );
}

/** True if `body` contains a `test.skip(...)` call anywhere within it (the
 * conditional self-skip pattern: `test.skip(!someCondition, "reason");`). */
function containsSelfSkip(body: ts.Node): boolean {
  let found = false;
  const walk = (node: ts.Node) => {
    if (found) return;
    if (isTestDotSkip(node)) {
      found = true;
      return;
    }
    ts.forEachChild(node, walk);
  };
  walk(body);
  return found;
}

function scanSpecFiles(): SpecTokens {
  const bareIds = new Set<string>();
  const statePairs = new Set<string>();
  const trustedBareIds = new Set<string>();
  const trustedStatePairs = new Set<string>();
  for (const filename of fs
    .readdirSync(__dirname)
    .filter((name) => name.endsWith(".spec.ts"))) {
    const source = fs.readFileSync(path.join(__dirname, filename), "utf8");
    const sourceFile = ts.createSourceFile(
      filename,
      source,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TS,
    );
    const visit = (node: ts.Node) => {
      const isPlainTestCall =
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "test";
      const isDeclarativeSkip = isTestDotSkip(node);

      if (
        (isPlainTestCall || isDeclarativeSkip) &&
        ts.isCallExpression(node)
      ) {
        const title = node.arguments[0];
        const callback = node.arguments[1];
        const selfSkipped =
          isPlainTestCall &&
          callback !== undefined &&
          (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback)) &&
          containsSelfSkip(callback.body);
        const skipGuarded = isDeclarativeSkip || selfSkipped;

        if (title && ts.isStringLiteralLike(title)) {
          for (const match of title.text.matchAll(PLAYWRIGHT_ID_PATTERN)) {
            bareIds.add(match[1]);
            if (!skipGuarded) trustedBareIds.add(match[1]);
          }
          for (const match of title.text.matchAll(
            PLAYWRIGHT_STATE_TOKEN_PATTERN,
          )) {
            const pair = `${match[1]}::${match[2]}`;
            statePairs.add(pair);
            if (!skipGuarded) trustedStatePairs.add(pair);
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return { bareIds, statePairs, trustedBareIds, trustedStatePairs };
}

interface SkipGuardedTest {
  file: string;
  title: string;
}

/** Every test that is skip-guarded (declaratively via `test.skip("title", fn)`,
 * or conditionally via the self-skip `test.skip(condition, reason)` statement
 * pattern), with its file and title — used to name-and-prove exactly which
 * tests are exempt from the trusted-coverage set, rather than leaving that
 * fact implicit. */
function listSkipGuardedTests(): SkipGuardedTest[] {
  const results: SkipGuardedTest[] = [];
  for (const filename of fs
    .readdirSync(__dirname)
    .filter((name) => name.endsWith(".spec.ts"))) {
    const source = fs.readFileSync(path.join(__dirname, filename), "utf8");
    const sourceFile = ts.createSourceFile(
      filename,
      source,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TS,
    );
    const visit = (node: ts.Node) => {
      const isPlainTestCall =
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "test";
      const isDeclarativeSkip = isTestDotSkip(node);

      if ((isPlainTestCall || isDeclarativeSkip) && ts.isCallExpression(node)) {
        const title = node.arguments[0];
        const callback = node.arguments[1];
        const selfSkipped =
          isPlainTestCall &&
          callback !== undefined &&
          (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback)) &&
          containsSelfSkip(callback.body);
        const skipGuarded = isDeclarativeSkip || selfSkipped;

        if (skipGuarded && title && ts.isStringLiteralLike(title)) {
          results.push({ file: filename, title: title.text });
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return results;
}

test("interaction manifest has no missing or orphaned Playwright IDs", async (
  {},
  testInfo,
) => {
  const requiredIds = new Set(
    UI_COVERAGE_MANIFEST.flatMap((interaction) => interaction.playwrightTestIds),
  );

  // The full required (interaction, state) contract: every declared state on every
  // interaction must have a truthful `[pw.<id>:<state>]` token somewhere in e2e/.
  const requiredStatePairs = new Set(
    UI_COVERAGE_MANIFEST.flatMap((interaction) =>
      interaction.states.map((state) => `${interaction.id}::${state}`),
    ),
  );
  const knownInteractionIds = new Set(
    UI_COVERAGE_MANIFEST.map((interaction) => interaction.id),
  );

  const {
    bareIds,
    statePairs,
    trustedBareIds,
    trustedStatePairs,
  } = scanSpecFiles();

  const missing = [...requiredIds].filter((id) => !trustedBareIds.has(id));
  // Orphan detection intentionally uses the raw (any-occurrence) sets, not the
  // trusted ones: a token claiming an undeclared state is a mistake worth
  // flagging even if the test happens to be skip-guarded.
  const orphaned = [...bareIds].filter((id) => !requiredIds.has(id));

  // Missing: a declared (interaction, state) pair with no *trusted* implemented
  // token anywhere — a token that exists only inside a skip-guarded test (see
  // `scanSpecFiles`) does NOT count, so a required path can never hide behind
  // a skip that might never actually run.
  const missingStates = [...requiredStatePairs]
    .filter((pair) => !trustedStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  // Orphaned: an implemented token whose (interaction, state) pair is not declared —
  // this also covers "a test claims an undeclared state" for a *known* interaction id,
  // and "a test references an interaction id that does not exist" for an unknown one.
  const orphanedStates = [...statePairs]
    .filter((pair) => !requiredStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  // Explicit, separately reported: tokens whose interaction id isn't in the manifest
  // at all (a stricter subset of `orphanedStates`, called out for clearer diagnostics).
  const unknownInteractionStates = orphanedStates.filter((pair) => {
    const id = pair.slice(0, pair.lastIndexOf(":"));
    return !knownInteractionIds.has(id);
  });

  // Diagnostic-only: required tokens that exist *somewhere* in the spec files
  // but exclusively inside skip-guarded tests — i.e. they would look covered
  // by a naive "any occurrence" scan but cannot actually run unconditionally.
  // These are already folded into `missing`/`missingStates` above; surfaced
  // separately here purely so a future regression is diagnosable at a glance
  // instead of looking like an ordinary uncovered state.
  const requiredIdsOnlyBehindSkip = [...requiredIds].filter(
    (id) => bareIds.has(id) && !trustedBareIds.has(id),
  );
  const requiredStatesOnlyBehindSkip = [...requiredStatePairs]
    .filter((pair) => statePairs.has(pair) && !trustedStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  const report = {
    missing,
    orphaned,
    stateCoverage: {
      requiredStateCount: requiredStatePairs.size,
      implementedStateCount: trustedStatePairs.size,
      missingStates,
      orphanedStates,
      unknownInteractionStates,
      requiredIdsOnlyBehindSkip,
      requiredStatesOnlyBehindSkip,
    },
  };

  await testInfo.attach("functional-coverage-contract.json", {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  });

  expect(
    report,
    "manifest and executable Playwright IDs must form a complete, per-state contract " +
      "with zero missing states, zero orphaned tokens, zero unmapped interaction ids, " +
      "and zero required tokens hiding behind a skip-guarded test",
  ).toEqual({
    missing: [],
    orphaned: [],
    stateCoverage: {
      requiredStateCount: requiredStatePairs.size,
      implementedStateCount: requiredStatePairs.size,
      missingStates: [],
      orphanedStates: [],
      unknownInteractionStates: [],
      requiredIdsOnlyBehindSkip: [],
      requiredStatesOnlyBehindSkip: [],
    },
  });
});

// Regression guard for the reviewer-identified loophole: the AST scan above is
// purely static (it reads `test(...)` title strings from source), so a test
// that is always/conditionally skipped at runtime would otherwise still count
// as "implemented" simply by having the right token in its title. This test
// proves, by direct inspection, that the one skip-guarded test in the suite
// (`workbench.spec.ts`'s "capture the V3 UI foundation..." legacy screenshot
// capture, gated on `UX_SCREENSHOT_DIR`) carries zero `[pw.*]` tokens — so no
// required interaction/state path is currently hidden behind it — and that
// `scanSpecFiles`'s trusted/raw split would catch it if that ever changed.
test("no skip-guarded test carries a required coverage token", () => {
  const { bareIds, statePairs, trustedBareIds, trustedStatePairs } =
    scanSpecFiles();

  const bareIdsOnlyBehindSkip = [...bareIds].filter(
    (id) => !trustedBareIds.has(id),
  );
  const statePairsOnlyBehindSkip = [...statePairs].filter(
    (pair) => !trustedStatePairs.has(pair),
  );

  expect(
    { bareIdsOnlyBehindSkip, statePairsOnlyBehindSkip },
    "every [pw.*] / [pw.*:*] token found anywhere in e2e/*.spec.ts must also " +
      "appear in a test that is not skip-guarded (declaratively or via the " +
      "conditional self-skip pattern), otherwise it could satisfy the coverage " +
      "contract while never actually executing",
  ).toEqual({ bareIdsOnlyBehindSkip: [], statePairsOnlyBehindSkip: [] });
});

// Named, explicit proof for the exact skip the reviewer flagged: asserts there
// is exactly one skip-guarded test in the whole suite, that it is the known
// legacy screenshot capture in `workbench.spec.ts` (conditional on
// `UX_SCREENSHOT_DIR`), and that its title carries no `[pw.*]` token. If a new
// skip is ever introduced anywhere in e2e/, or this one's title gains a token,
// this assertion fails loudly instead of silently trusting an unproven skip.
test("the sole skip-guarded test is the known legacy screenshot capture with no coverage token", () => {
  const skipGuardedTests = listSkipGuardedTests();

  expect(
    skipGuardedTests,
    "exactly one skip-guarded test is expected in e2e/*.spec.ts today " +
      "(workbench.spec.ts's UX_SCREENSHOT_DIR-gated legacy capture); if this " +
      "fails, a new skip was introduced and must be proven not to hide a " +
      "required coverage token before it can be trusted",
  ).toEqual([
    {
      file: "workbench.spec.ts",
      title: "capture the V3 UI foundation at desktop and mobile",
    },
  ]);

  for (const { title } of skipGuardedTests) {
    expect(
      [...title.matchAll(PLAYWRIGHT_ID_PATTERN)],
      `skip-guarded test "${title}" must carry no [pw.*] token`,
    ).toEqual([]);
    expect(
      [...title.matchAll(PLAYWRIGHT_STATE_TOKEN_PATTERN)],
      `skip-guarded test "${title}" must carry no [pw.*:*] state token`,
    ).toEqual([]);
  }
});

