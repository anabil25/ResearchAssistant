import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";
import ts from "typescript";

import { UI_COVERAGE_MANIFEST } from "../src/testing/interaction-manifest";
import {
  PLAYWRIGHT_ID_PATTERN,
  PLAYWRIGHT_STATE_TOKEN_PATTERN,
} from "../src/testing/runtime-coverage-verifier";

// Token grammar (bare `[pw.<alias>]` and per-state `[pw.<interaction-id>:<state>]`)
// lives in `runtime-coverage-verifier.ts` so the static AST scan here and the
// dynamic post-run JSON-report verifier agree on exactly the same patterns.

interface SkipGuardedTest {
  file: string;
  title: string;
}

interface SpecTokens {
  bareIds: Set<string>;
  statePairs: Set<string>;
  // "Trusted" = found in at least one test that is not skip-guarded, where
  // "skip-guarded" means any of:
  //   - a declarative `test.skip("title", fn)` or `test.fixme("title", fn)`;
  //   - the conditional self-skip/self-fixme statement pattern
  //     `test.skip(condition, reason)` / `test.fixme(condition, reason)` used
  //     as a statement inside a test's own body (e.g. gated on an env var);
  //   - nesting anywhere inside a `test.describe.skip(...)` or
  //     `test.describe.fixme(...)` group, at any depth (the ancestor context
  //     propagates to every test nested within, however deeply).
  // Only trusted tokens may satisfy a required (bare id / state) contract
  // entry — a token that appears exclusively inside a skip-guarded test must
  // not be able to satisfy required coverage, since the test backing it may
  // never actually run.
  trustedBareIds: Set<string>;
  trustedStatePairs: Set<string>;
  skipGuardedTests: SkipGuardedTest[];
}

/** Resolves the dotted callee path of a call expression, e.g. `test(...)` ->
 * `["test"]`, `test.skip(...)` -> `["test", "skip"]`,
 * `test.describe.skip(...)` -> `["test", "describe", "skip"]`. Returns `null`
 * for any call whose callee isn't a plain identifier-rooted property-access
 * chain (e.g. computed member access, IIFEs), which this gate has no
 * business trying to interpret. */
function getCalleePath(node: ts.CallExpression): string[] | null {
  const parts: string[] = [];
  let expr: ts.Expression = node.expression;
  while (ts.isPropertyAccessExpression(expr)) {
    parts.unshift(expr.name.text);
    expr = expr.expression;
  }
  if (ts.isIdentifier(expr)) {
    parts.unshift(expr.text);
    return parts;
  }
  return null;
}

function pathEquals(actual: string[], expected: string[]): boolean {
  return (
    actual.length === expected.length &&
    actual.every((part, index) => part === expected[index])
  );
}

// `test.fail` is treated identically to `skip`/`fixme` for trust purposes: a
// test that declares itself expected to fail must never satisfy required
// coverage, since an "unexpected pass" for such a test is not evidence the
// behavior genuinely works (the runtime verifier enforces the matching
// expectedStatus/outcome check for the same reason).
const DISABLING_MODIFIERS = new Set(["skip", "fixme", "fail"]);

/** True if `body` contains a `test.skip(...)`, `test.fixme(...)`, or
 * `test.fail(...)` call anywhere within it (the conditional
 * self-skip/self-fixme/self-fail statement pattern:
 * `test.skip(!someCondition, "reason");`). */
function containsSelfDisablingAnnotation(body: ts.Node): boolean {
  let found = false;
  const walk = (node: ts.Node) => {
    if (found) return;
    if (ts.isCallExpression(node)) {
      const calleePath = getCalleePath(node);
      if (
        calleePath &&
        calleePath.length === 2 &&
        calleePath[0] === "test" &&
        DISABLING_MODIFIERS.has(calleePath[1])
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, walk);
  };
  walk(body);
  return found;
}

/** True if a `test(...)` / `test.skip(...)` / `test.fixme(...)` /
 * `test.fail(...)` call is the *declarative test-definition* shape — first
 * argument is a string title — as opposed to the *bare annotation* shape
 * Playwright also supports for conditionally skipping/fixme-ing/failing
 * every test in the containing file or describe block
 * (`test.skip(condition, description)`,
 * `test.fixme(({ browserName }) => browserName === "webkit", description)`,
 * with no title). Playwright distinguishes these by argument shape at
 * runtime; the only static, unambiguous signal available here is whether
 * the first argument is a string literal. */
function isDeclarativeDefinitionShape(node: ts.CallExpression): boolean {
  const first = node.arguments[0];
  return first !== undefined && ts.isStringLiteralLike(first);
}

/** Returns the callback argument of a `test(...)`/`test.describe(...)`
 * -family call, handling both the ordinary `(title, callback)` shape and
 * Playwright's `(title, testDetails, callback)` overload (where the middle
 * argument is a `{ tag, annotation }` details object): the callback is
 * always the *last* argument when that argument is itself a function
 * expression, regardless of how many arguments precede it.
 *
 * Deliberately returns `undefined` (rather than attempting to resolve it)
 * when the last argument is a plain identifier reference to a named
 * function/const declared elsewhere (e.g. `test("title", someHelperFn)`,
 * including the 3-arg `test(title, details, someHelperFn)` overload): this
 * static scan has no reliable, scope-correct way to resolve an arbitrary
 * identifier back to its declaration (it could be imported, reassigned,
 * shadowed, or declared after use). Callers must treat an `undefined`
 * result here as "callback body unknown" and fail closed -- i.e. never
 * trust such a test's coverage tokens -- rather than assuming the
 * unexamined body contains no self-skip/self-fixme/self-fail annotation. */
function getCallback(
  node: ts.CallExpression,
): ts.ArrowFunction | ts.FunctionExpression | undefined {
  const args = node.arguments;
  const last = args[args.length - 1];
  if (last && (ts.isArrowFunction(last) || ts.isFunctionExpression(last))) {
    return last;
  }
  return undefined;
}

/** Conservatively detects whether `node`'s subtree could, at runtime,
 * execute a bare block-level skip/fixme/fail annotation statement (e.g.
 * `test.skip(condition, "reason")`) that would run in the *enclosing*
 * block's own scope -- i.e. one not wrapped in its own
 * `test(...)`/`test.describe(...)` callback. `walkGeneric`'s structural
 * nesting recursion never threads a "block skipped" flag forward to later
 * sibling *statements* (only `walkStatements`/`processStatement` does
 * that, and historically only for annotations that were themselves direct
 * statements in the block) -- so a bare annotation nested inside an `if`,
 * loop, `switch`, `try`/`catch`, or IIFE that this static scan cannot
 * prove will or won't execute must conservatively be treated as if it
 * always disables everything declared afterward in the same block. Failing
 * closed (treating ambiguous nesting as disabling) is safer than failing
 * open (silently trusting later tokens that a real runtime skip could
 * hide behind). Does not descend into a nested, *declaratively-shaped*
 * `test(...)`/`test.describe(...)` call's own callback body -- that
 * scope's tests are handled independently by the normal recursive walk,
 * and a bare annotation fully contained within a different test's/
 * describe's own body has separately-covered semantics (self-disabling,
 * or scoped structural nesting) that must not leak out to this
 * enclosing block. */
function mayIntroduceBareDisablingAnnotation(node: ts.Node): boolean {
  if (ts.isCallExpression(node)) {
    const calleePath = getCalleePath(node);
    if (calleePath) {
      const isTestModifier =
        calleePath.length === 2 &&
        calleePath[0] === "test" &&
        DISABLING_MODIFIERS.has(calleePath[1]);
      const isPlainTest = pathEquals(calleePath, ["test"]);
      const isDescribeFamily =
        calleePath[0] === "test" && calleePath[1] === "describe";

      if (isTestModifier && !isDeclarativeDefinitionShape(node)) {
        // Exactly the scope-leaking bare annotation shape this helper
        // exists to detect: `test.skip(condition, "reason")` etc., called
        // directly (not wrapping a title/body of its own).
        return true;
      }
      if (
        (isPlainTest || isTestModifier || isDescribeFamily) &&
        isDeclarativeDefinitionShape(node)
      ) {
        // A declarative test(...)/describe(...) definition establishes its
        // own separate scope; its own nested bare annotations (if any) only
        // affect siblings within that scope, handled elsewhere -- do not
        // descend into its callback body from here.
        return false;
      }
    }
  }
  let found = false;
  ts.forEachChild(node, (child) => {
    if (!found && mayIntroduceBareDisablingAnnotation(child)) {
      found = true;
    }
  });
  return found;
}

/** Parse a single spec file's source and extract every `[pw.*]` /
 * `[pw.*:*]` token, split into "any occurrence" and "trusted" (not
 * skip-guarded) sets, plus the list of every skip-guarded test found. Pure
 * function of `(filename, source)` — no filesystem access — so it can be
 * exercised directly with synthetic snippets in the regression tests below,
 * independently of what currently exists in `e2e/*.spec.ts`.
 *
 * Skip-guard detection tracks two independent kinds of ancestor context:
 *
 * 1. *Structural* nesting: once a `test.describe.skip(...)` or
 *    `test.describe.fixme(...)` call is entered, every test nested within
 *    it — at any depth, through any number of plain (non-skipped)
 *    `test.describe(...)` groups in between — is skip-guarded, even if
 *    that individual `test(...)` call has no skip marker of its own.
 *
 * 2. *Lexical/sequential* block annotations: Playwright also supports
 *    calling `test.skip(condition, description)` / `test.fixme(...)` /
 *    `test.fail(...)` directly as a statement inside a `describe` callback
 *    (or at the top level of a file) — not wrapping any individual test —
 *    which conditionally disables *every test declared after it* in that
 *    same block, for the remainder of the block. This walker processes
 *    each block's statements in document order and threads a running
 *    "block skipped" flag forward across sibling statements to catch
 *    exactly this case, in addition to the structural nesting above. A
 *    describe.skip/fixme group's effect is scoped to its own nested
 *    subtree only and never leaks forward to its own siblings (that
 *    remains purely structural nesting, case 1 above). Conservatively,
 *    this also covers a bare annotation nested inside an `if`, loop,
 *    `switch`, `try`/`catch`, or IIFE that is itself a direct statement in
 *    the block: since this static scan cannot evaluate the runtime
 *    condition guarding such a construct, `mayIntroduceBareDisablingAnnotation`
 *    conservatively treats *any* such construct that could contain a bare
 *    disabling annotation as if it always disables subsequent siblings —
 *    failing closed rather than silently trusting tokens that a real,
 *    reachable runtime skip could hide behind.
 */
function extractTokensFromSource(filename: string, source: string): SpecTokens {
  const bareIds = new Set<string>();
  const statePairs = new Set<string>();
  const trustedBareIds = new Set<string>();
  const trustedStatePairs = new Set<string>();
  const skipGuardedTests: SkipGuardedTest[] = [];

  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );

  function recordTitle(title: ts.Expression, skipGuarded: boolean): void {
    if (!ts.isStringLiteralLike(title)) return;
    for (const match of title.text.matchAll(PLAYWRIGHT_ID_PATTERN)) {
      bareIds.add(match[1]);
      if (!skipGuarded) trustedBareIds.add(match[1]);
    }
    for (const match of title.text.matchAll(PLAYWRIGHT_STATE_TOKEN_PATTERN)) {
      const pair = `${match[1]}::${match[2]}`;
      statePairs.add(pair);
      if (!skipGuarded) trustedStatePairs.add(pair);
    }
    if (skipGuarded) {
      skipGuardedTests.push({ file: filename, title: title.text });
    }
  }

  // Generic fallback recursion for anything not encountered as a direct
  // statement in a tracked block (e.g. a test/describe call nested inside
  // an `if`, a loop, or an IIFE) — preserves the structural (nesting-based)
  // skip-guard behavior for such cases. Sequential block-annotation
  // tracking (threading a "block skipped" flag forward to later siblings)
  // is handled by `processStatement`'s callers via
  // `mayIntroduceBareDisablingAnnotation`, not by this recursion itself.
  function walkGeneric(node: ts.Node, ancestorSkipped: boolean): void {
    let childContext = ancestorSkipped;

    if (ts.isCallExpression(node)) {
      const calleePath = getCalleePath(node);
      if (calleePath) {
        const isDescribeSkip = pathEquals(calleePath, ["test", "describe", "skip"]);
        const isDescribeFixme = pathEquals(calleePath, ["test", "describe", "fixme"]);
        const isTestModifier =
          calleePath.length === 2 &&
          calleePath[0] === "test" &&
          DISABLING_MODIFIERS.has(calleePath[1]);
        const isPlainTest = pathEquals(calleePath, ["test"]);

        if (isDescribeSkip || isDescribeFixme || isTestModifier) {
          childContext = true;
        }

        if ((isPlainTest || isTestModifier) && isDeclarativeDefinitionShape(node)) {
          const title = node.arguments[0];
          const callback = getCallback(node);
          const selfDisabled =
            isPlainTest &&
            callback !== undefined &&
            containsSelfDisablingAnnotation(callback.body);
          // A plain `test(...)` whose last argument isn't a literal inline
          // arrow/function expression (e.g. a reference to a named
          // function or const declared elsewhere) cannot be statically
          // proven free of its own self-skip/self-fixme/self-fail
          // annotation: `getCallback` intentionally only recognizes
          // inline function bodies, so `containsSelfDisablingAnnotation`
          // has no body to inspect for an identifier reference. Fail
          // closed and treat any such unresolvable callback as
          // skip-guarded, the same as a real self-disabling annotation,
          // rather than silently trusting a body this scan never saw.
          const unresolvedCallback = isPlainTest && callback === undefined;
          const skipGuarded =
            ancestorSkipped || isTestModifier || selfDisabled || unresolvedCallback;
          recordTitle(title, skipGuarded);
        }
      }
    }

    ts.forEachChild(node, (child) => walkGeneric(child, childContext));
  }

  // Ordered statement-list walker: threads a running "block skipped" flag
  // across sibling statements so a bare `test.skip(condition, description)`
  // annotation statement disables every test declared after it in the same
  // block, in addition to the structural describe.skip/fixme nesting and
  // declarative-form handling shared with the generic fallback above.
  function walkStatements(
    statements: readonly ts.Statement[],
    ancestorSkipped: boolean,
  ): void {
    let running = ancestorSkipped;
    for (const statement of statements) {
      if (processStatement(statement, running)) {
        running = true;
      }
    }
  }

  /** Returns `true` iff `statement` is a bare block-level skip/fixme/fail
   * annotation (no title/body of its own), meaning every subsequent sibling
   * statement in the same list must be treated as skip-guarded from this
   * point forward. Describe-group statements always return `false` here —
   * their skip/fixme effect is purely structural (scoped to their own
   * nested subtree) and must never leak forward to their own siblings. */
  function processStatement(
    statement: ts.Statement,
    ancestorSkipped: boolean,
  ): boolean {
    if (
      !ts.isExpressionStatement(statement) ||
      !ts.isCallExpression(statement.expression)
    ) {
      walkGeneric(statement, ancestorSkipped);
      return mayIntroduceBareDisablingAnnotation(statement);
    }

    const call = statement.expression;
    const calleePath = getCalleePath(call);
    if (!calleePath) {
      walkGeneric(statement, ancestorSkipped);
      return mayIntroduceBareDisablingAnnotation(statement);
    }

    if (calleePath[0] === "test" && calleePath[1] === "describe") {
      const describeModifier = calleePath[2];
      const isDescribeSkipOrFixme =
        describeModifier === "skip" || describeModifier === "fixme";
      const effectiveSkipped = ancestorSkipped || isDescribeSkipOrFixme;
      const callback = getCallback(call);
      if (callback) {
        if (ts.isBlock(callback.body)) {
          walkStatements(callback.body.statements, effectiveSkipped);
        } else {
          walkGeneric(callback.body, effectiveSkipped);
        }
      }
      return false;
    }

    const isPlainTest = pathEquals(calleePath, ["test"]);
    const isTestModifier =
      calleePath.length === 2 &&
      calleePath[0] === "test" &&
      DISABLING_MODIFIERS.has(calleePath[1]);

    if (isPlainTest || isTestModifier) {
      if (isDeclarativeDefinitionShape(call)) {
        const title = call.arguments[0];
        const callback = getCallback(call);
        const selfDisabled =
          isPlainTest &&
          callback !== undefined &&
          containsSelfDisablingAnnotation(callback.body);
        // See the matching comment in `walkGeneric`: an unresolvable
        // (non-inline, e.g. named-identifier) callback must fail closed
        // as skip-guarded, since this scan cannot inspect a body it
        // never sees for a hidden self-skip/self-fixme/self-fail.
        const unresolvedCallback = isPlainTest && callback === undefined;
        const skipGuarded =
          ancestorSkipped || isTestModifier || selfDisabled || unresolvedCallback;
        recordTitle(title, skipGuarded);
        return false;
      }

      // Bare annotation statement (e.g. `test.skip(condition, "reason")` or
      // `test.fixme(({ browserName }) => browserName === "webkit", "...")`)
      // called directly in this block, not wrapping any individual test:
      // it disables every sibling test declared after it in this list.
      return true;
    }

    walkGeneric(statement, ancestorSkipped);
    return mayIntroduceBareDisablingAnnotation(statement);
  }

  walkStatements(sourceFile.statements, false);

  return { bareIds, statePairs, trustedBareIds, trustedStatePairs, skipGuardedTests };
}

/** Scan every real `e2e/*.spec.ts` file and merge their tokens/skip-guarded
 * tests into one aggregate result. */
function scanAllSpecFiles(): SpecTokens {
  const bareIds = new Set<string>();
  const statePairs = new Set<string>();
  const trustedBareIds = new Set<string>();
  const trustedStatePairs = new Set<string>();
  const skipGuardedTests: SkipGuardedTest[] = [];
  for (const filename of fs
    .readdirSync(__dirname)
    .filter((name) => name.endsWith(".spec.ts"))) {
    const source = fs.readFileSync(path.join(__dirname, filename), "utf8");
    const result = extractTokensFromSource(filename, source);
    for (const id of result.bareIds) bareIds.add(id);
    for (const pair of result.statePairs) statePairs.add(pair);
    for (const id of result.trustedBareIds) trustedBareIds.add(id);
    for (const pair of result.trustedStatePairs) trustedStatePairs.add(pair);
    skipGuardedTests.push(...result.skipGuardedTests);
  }
  return { bareIds, statePairs, trustedBareIds, trustedStatePairs, skipGuardedTests };
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
  } = scanAllSpecFiles();

  const missing = [...requiredIds].filter((id) => !trustedBareIds.has(id));
  // Orphan detection intentionally uses the raw (any-occurrence) sets, not the
  // trusted ones: a token claiming an undeclared state is a mistake worth
  // flagging even if the test happens to be skip-guarded.
  const orphaned = [...bareIds].filter((id) => !requiredIds.has(id));

  // Missing: a declared (interaction, state) pair with no *trusted* implemented
  // token anywhere — a token that exists only inside a skip-guarded test, or
  // nested at any depth inside a `test.describe.skip`/`test.describe.fixme`
  // group (see `extractTokensFromSource`), does NOT count, so a required path
  // can never hide behind a skip that might never actually run.
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
// `scanAllSpecFiles`'s trusted/raw split would catch it if that ever changed.
test("no skip-guarded test carries a required coverage token", () => {
  const { bareIds, statePairs, trustedBareIds, trustedStatePairs } =
    scanAllSpecFiles();

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
  const { skipGuardedTests } = scanAllSpecFiles();

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

// --- Regression suite for the second reviewer-identified gate weakness: the
// original skip-guard fix (see the two tests above) only recognized a
// `test.skip(...)` call directly wrapping/inside the test being scanned. It
// did not track ancestor context, so a token nested inside a
// `test.describe.skip(...)` group, a `test.describe.fixme(...)` group, a
// declarative `test.fixme(...)`, a self-fixme statement, or any deeper
// nesting of the above, could still be counted as "trusted" by a purely
// call-expression-local check even though the enclosing group (and therefore
// the test) never actually runs. Each case below is exercised directly
// against `extractTokensFromSource` with a synthetic, self-contained source
// snippet — independent of whatever currently exists in `e2e/*.spec.ts` —
// so these regressions are proven by the walker's actual behavior, not by
// what happens to be true of today's spec files.

test("a token nested inside test.describe.skip is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe.skip("a skipped group", () => {
      test("does something [pw.synthetic-check:describe-skip]", async () => {
        // never actually runs
      });
    });
  `;
  const result = extractTokensFromSource("synthetic-describe-skip.spec.ts", source);

  expect(result.statePairs.has("synthetic-check::describe-skip")).toBe(true);
  expect(result.trustedStatePairs.has("synthetic-check::describe-skip")).toBe(
    false,
  );
  expect(result.skipGuardedTests).toEqual([
    {
      file: "synthetic-describe-skip.spec.ts",
      title: "does something [pw.synthetic-check:describe-skip]",
    },
  ]);
});

test("a token nested inside test.describe.fixme is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe.fixme("a fixme group", () => {
      test("does something [pw.synthetic-check:describe-fixme]", async () => {
        // never actually runs
      });
    });
  `;
  const result = extractTokensFromSource("synthetic-describe-fixme.spec.ts", source);

  expect(result.statePairs.has("synthetic-check::describe-fixme")).toBe(true);
  expect(
    result.trustedStatePairs.has("synthetic-check::describe-fixme"),
  ).toBe(false);
});

test("a token nested two levels deep inside test.describe.skip is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("an outer, non-skipped group", () => {
      test.describe.skip("an inner skipped group", () => {
        test("does something [pw.synthetic-check:deep-describe-skip]", async () => {
          // never actually runs
        });
      });
    });
  `;
  const result = extractTokensFromSource(
    "synthetic-nested-describe-skip.spec.ts",
    source,
  );

  expect(
    result.statePairs.has("synthetic-check::deep-describe-skip"),
  ).toBe(true);
  expect(
    result.trustedStatePairs.has("synthetic-check::deep-describe-skip"),
  ).toBe(false);
});

test("a declarative test.fixme is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test.fixme("does something [pw.synthetic-check:declarative-fixme]", async () => {
      // never actually runs
    });
  `;
  const result = extractTokensFromSource("synthetic-test-fixme.spec.ts", source);

  expect(
    result.statePairs.has("synthetic-check::declarative-fixme"),
  ).toBe(true);
  expect(
    result.trustedStatePairs.has("synthetic-check::declarative-fixme"),
  ).toBe(false);
  expect(result.skipGuardedTests).toEqual([
    {
      file: "synthetic-test-fixme.spec.ts",
      title: "does something [pw.synthetic-check:declarative-fixme]",
    },
  ]);
});

test("a self-fixme statement inside a test body is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test("does something [pw.synthetic-check:self-fixme]", async ({}, testInfo) => {
      test.fixme(someRuntimeCondition, "only on some platforms");
      // the rest of the body would still execute here in real Playwright
      // semantics only if the condition is false; the point is this token
      // must not be trusted regardless, since it is conditional at runtime.
    });
  `;
  const result = extractTokensFromSource("synthetic-self-fixme.spec.ts", source);

  expect(result.statePairs.has("synthetic-check::self-fixme")).toBe(true);
  expect(result.trustedStatePairs.has("synthetic-check::self-fixme")).toBe(
    false,
  );
});

test("a token in a plain, non-skipped nested describe group remains trusted", () => {
  // Positive control: proves the ancestor-context tracking doesn't
  // over-correct into treating every describe-nested test as untrusted.
  const source = `
    import { test } from "@playwright/test";
    test.describe("a normal group", () => {
      test("does something [pw.synthetic-check:plain-describe]", async () => {
        // runs normally
      });
    });
  `;
  const result = extractTokensFromSource("synthetic-plain-describe.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::plain-describe")).toBe(
    true,
  );
  expect(result.skipGuardedTests).toEqual([]);
});

test("a skip.describe group does not leak skip context to its sibling tests", () => {
  // Proves the ancestor context is scoped to the skipped subtree only: a
  // sibling test declared after (or outside) the skipped group must remain
  // trusted.
  const source = `
    import { test } from "@playwright/test";
    test.describe.skip("a skipped group", () => {
      test("inside the skipped group [pw.synthetic-check:sibling-inside]", async () => {});
    });
    test("outside the skipped group [pw.synthetic-check:sibling-outside]", async () => {});
  `;
  const result = extractTokensFromSource("synthetic-sibling-scope.spec.ts", source);

  expect(
    result.trustedStatePairs.has("synthetic-check::sibling-inside"),
  ).toBe(false);
  expect(
    result.trustedStatePairs.has("synthetic-check::sibling-outside"),
  ).toBe(true);
});

test("a top-level, non-nested test with no skip marker remains trusted (sanity)", () => {
  const source = `
    import { test } from "@playwright/test";
    test("does something [pw.synthetic-check:top-level]", async () => {
      // runs normally
    });
  `;
  const result = extractTokensFromSource("synthetic-top-level.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::top-level")).toBe(
    true,
  );
  expect(result.skipGuardedTests).toEqual([]);
});

// --- Regression suite for the reviewer-identified gate weakness: a
// *lexical* `test.skip(condition, description)` / `test.fixme(...)` /
// `test.fail(...)` statement called directly inside a `describe` callback
// (or at the top level of a file) — not wrapping any individual test — is a
// documented Playwright feature that disables every test declared *after*
// it in that same block. The original ancestor-context fix only tracked
// *structural* nesting (inside a call's own subtree), so this sequential,
// statement-order-dependent form could slip through uncaught. Each case
// below is exercised directly against `extractTokensFromSource`.

test("a bare test.skip(condition, description) annotation disables later sibling tests in the same describe block", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with a block-level skip annotation", () => {
      test("before the annotation [pw.synthetic-check:before-block-skip]", async () => {});
      test.skip(someRuntimeCondition, "not implemented on this platform");
      test("after the annotation [pw.synthetic-check:after-block-skip]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-block-skip.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-block-skip")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-block-skip")).toBe(
    false,
  );
  expect(result.statePairs.has("synthetic-check::after-block-skip")).toBe(true);
});

test("a bare test.fixme(condition, description) annotation disables later sibling tests in the same describe block", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with a block-level fixme annotation", () => {
      test("before the annotation [pw.synthetic-check:before-block-fixme]", async () => {});
      test.fixme(({ browserName }) => browserName === "webkit", "tracked in issue #1");
      test("after the annotation [pw.synthetic-check:after-block-fixme]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-block-fixme.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-block-fixme")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-block-fixme")).toBe(
    false,
  );
});

test("a bare test.fail(condition, description) annotation disables later sibling tests in the same describe block", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with a block-level fail annotation", () => {
      test("before the annotation [pw.synthetic-check:before-block-fail]", async () => {});
      test.fail(someRuntimeCondition, "known regression, tracked in issue #2");
      test("after the annotation [pw.synthetic-check:after-block-fail]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-block-fail.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-block-fail")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-block-fail")).toBe(
    false,
  );
});

test("a bare test.skip annotation at the top level of a file disables later sibling tests outside any describe", () => {
  const source = `
    import { test } from "@playwright/test";
    test("before the annotation [pw.synthetic-check:before-file-skip]", async () => {});
    test.skip(someRuntimeCondition, "not implemented on this platform");
    test("after the annotation [pw.synthetic-check:after-file-skip]", async () => {});
  `;
  const result = extractTokensFromSource("synthetic-file-skip.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-file-skip")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-file-skip")).toBe(
    false,
  );
});

test("a bare test.skip annotation nested inside an `if` block conservatively disables later sibling tests in the enclosing describe", () => {
  // Real gap fix: previously, a bare skip/fixme/fail annotation encountered
  // only via walkGeneric's fallback (i.e. not a direct statement in the
  // block) never propagated its disabling effect forward to later
  // siblings -- only annotations that were themselves direct block
  // statements did. This left a real loophole: since this static scan
  // cannot evaluate `someRuntimeCondition`, a bare `test.skip(...)` guarded
  // by an unrelated `if` must conservatively be assumed reachable, and
  // must disable everything declared after the `if` in the same block.
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with an if-nested block-level skip annotation", () => {
      test("before the if [pw.synthetic-check:before-if-skip]", async () => {});
      if (someRuntimeCondition) {
        test.skip(true, "disabled under this condition");
      }
      test("after the if [pw.synthetic-check:after-if-skip]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-if-block-skip.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-if-skip")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-if-skip")).toBe(
    false,
  );
  expect(result.statePairs.has("synthetic-check::after-if-skip")).toBe(true);
});

test("a bare test.fixme annotation nested inside a `for` loop conservatively disables later sibling tests in the enclosing describe", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with a loop-nested block-level fixme annotation", () => {
      test("before the loop [pw.synthetic-check:before-loop-fixme]", async () => {});
      for (const platform of skippedPlatforms) {
        test.fixme(platform === currentPlatform, "tracked in issue #3");
      }
      test("after the loop [pw.synthetic-check:after-loop-fixme]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-loop-block-fixme.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-loop-fixme")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-loop-fixme")).toBe(
    false,
  );
});

test("a bare test.fail annotation nested inside an IIFE conservatively disables later sibling tests in the enclosing describe", () => {
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with an IIFE-nested block-level fail annotation", () => {
      test("before the IIFE [pw.synthetic-check:before-iife-fail]", async () => {});
      (function () {
        test.fail(someRuntimeCondition, "known regression, tracked in issue #4");
      })();
      test("after the IIFE [pw.synthetic-check:after-iife-fail]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-iife-block-fail.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-iife-fail")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-iife-fail")).toBe(
    false,
  );
});

test("a bare test.skip annotation nested inside a `while` loop at the top level of a file conservatively disables later top-level siblings", () => {
  const source = `
    import { test } from "@playwright/test";
    test("before the while [pw.synthetic-check:before-while-skip]", async () => {});
    while (someRuntimeCondition) {
      test.skip(true, "disabled under this condition");
      break;
    }
    test("after the while [pw.synthetic-check:after-while-skip]", async () => {});
  `;
  const result = extractTokensFromSource("synthetic-while-block-skip.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::before-while-skip")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-while-skip")).toBe(
    false,
  );
});

test("a describe/test call fully nested inside a plain `if` block still records its own token and is trusted when unguarded (sanity)", () => {
  // Sanity companion to the conservative-disabling tests above: a test
  // declared *inside* an `if` block with no skip annotation anywhere must
  // still be found and trusted -- the conservative fallback must not
  // become so broad that it starts treating ordinary conditional test
  // registration as untrusted.
  const source = `
    import { test } from "@playwright/test";
    test.describe("a group with a plain if-nested test and no skip annotation", () => {
      if (someRuntimeCondition) {
        test("nested in a plain if [pw.synthetic-check:plain-if-nested]", async () => {});
      }
      test("after the plain if [pw.synthetic-check:after-plain-if]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-plain-if.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::plain-if-nested")).toBe(
    true,
  );
  expect(result.trustedStatePairs.has("synthetic-check::after-plain-if")).toBe(
    true,
  );
});

test("a bare skip annotation fully contained inside a *different, nested* describe's own if-block does not leak out to the outer block's siblings", () => {
  // The conservative fallback must stop at a declaratively-shaped nested
  // test()/describe() call's own callback body: a bare annotation reachable
  // only inside that inner describe's own if-block affects that inner
  // describe's own later siblings (if any), not the *outer* block's
  // siblings declared after the whole inner describe call.
  const source = `
    import { test } from "@playwright/test";
    test.describe("outer", () => {
      test.describe("inner", () => {
        if (someRuntimeCondition) {
          test.skip(true, "disabled under this condition");
        }
        test("inner after if [pw.synthetic-check:inner-after-if]", async () => {});
      });
      test("outer after inner describe [pw.synthetic-check:outer-after-inner]", async () => {});
    });
  `;
  const result = extractTokensFromSource(
    "synthetic-nested-describe-if-scope.spec.ts",
    source,
  );

  expect(result.trustedStatePairs.has("synthetic-check::inner-after-if")).toBe(
    false,
  );
  expect(
    result.trustedStatePairs.has("synthetic-check::outer-after-inner"),
  ).toBe(true);
});

test("a declarative test.fail is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test.fail("does something [pw.synthetic-check:declarative-fail]", async () => {
      // expected to fail; an unexpected pass must never satisfy coverage
    });
  `;
  const result = extractTokensFromSource("synthetic-test-fail.spec.ts", source);

  expect(result.statePairs.has("synthetic-check::declarative-fail")).toBe(true);
  expect(result.trustedStatePairs.has("synthetic-check::declarative-fail")).toBe(
    false,
  );
  expect(result.skipGuardedTests).toEqual([
    {
      file: "synthetic-test-fail.spec.ts",
      title: "does something [pw.synthetic-check:declarative-fail]",
    },
  ]);
});

test("a self-fail statement inside a test body is untrusted", () => {
  const source = `
    import { test } from "@playwright/test";
    test("does something [pw.synthetic-check:self-fail]", async () => {
      test.fail(someRuntimeCondition, "known regression");
    });
  `;
  const result = extractTokensFromSource("synthetic-self-fail.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::self-fail")).toBe(false);
});

test("a describe.skip group's effect does not leak forward to its own siblings (structural, not sequential)", () => {
  // Distinguishes the two mechanisms: a describe.skip/fixme *group* only
  // disables its own nested subtree (structural nesting); it must not also
  // behave like a bare block-annotation statement that poisons its own
  // following top-level siblings.
  const source = `
    import { test } from "@playwright/test";
    test.describe.skip("a skipped group", () => {
      test("inside [pw.synthetic-check:describe-skip-inside]", async () => {});
    });
    test.describe("a normal sibling group declared after the skipped one", () => {
      test("inside the normal group [pw.synthetic-check:describe-skip-sibling]", async () => {});
    });
  `;
  const result = extractTokensFromSource(
    "synthetic-describe-skip-no-leak.spec.ts",
    source,
  );

  expect(
    result.trustedStatePairs.has("synthetic-check::describe-skip-inside"),
  ).toBe(false);
  expect(
    result.trustedStatePairs.has("synthetic-check::describe-skip-sibling"),
  ).toBe(true);
});

test("a token declared before a block-level annotation remains trusted (order matters)", () => {
  // Sanity check for statement ordering: the annotation must only affect
  // tests declared *after* it textually, never ones declared before.
  const source = `
    import { test } from "@playwright/test";
    test.describe("ordering sanity", () => {
      test("first, before any annotation [pw.synthetic-check:order-before]", async () => {});
      test.skip(true, "skip everything from here on");
      test("second, after the annotation [pw.synthetic-check:order-after]", async () => {});
    });
  `;
  const result = extractTokensFromSource("synthetic-order-sanity.spec.ts", source);

  expect(result.trustedStatePairs.has("synthetic-check::order-before")).toBe(true);
  expect(result.trustedStatePairs.has("synthetic-check::order-after")).toBe(false);
});

test("self-skip detection still works inside the test(title, testDetails, callback) three-argument overload", () => {
  // Playwright supports an overload where the second argument is a
  // `{ tag, annotation }` details object and the actual test body is the
  // third argument. The callback lookup must find the real body (the last
  // function-typed argument), not mistake the details object for it.
  const source = `
    import { test } from "@playwright/test";
    test(
      "does something [pw.synthetic-check:three-arg-self-skip]",
      { tag: "@slow" },
      async () => {
        test.skip(someRuntimeCondition, "only on some platforms");
      },
    );
  `;
  const result = extractTokensFromSource(
    "synthetic-three-arg-overload.spec.ts",
    source,
  );

  expect(
    result.trustedStatePairs.has("synthetic-check::three-arg-self-skip"),
  ).toBe(false);
});

test("the test(title, testDetails, callback) three-argument overload remains trusted when unguarded", () => {
  // Positive control for the three-argument overload: proves the callback
  // lookup doesn't over-correct into treating every such call as untrusted.
  const source = `
    import { test } from "@playwright/test";
    test(
      "does something [pw.synthetic-check:three-arg-trusted]",
      { tag: "@fast" },
      async () => {
        // runs normally
      },
    );
  `;
  const result = extractTokensFromSource(
    "synthetic-three-arg-trusted.spec.ts",
    source,
  );

  expect(
    result.trustedStatePairs.has("synthetic-check::three-arg-trusted"),
  ).toBe(true);
});

test("a named-function callback reference is untrusted even when its (unseen) body contains a real self-skip", () => {
  // `getCallback` only recognizes an inline arrow/function expression as
  // the last argument. A `test("title", someNamedFn)` call passes a plain
  // identifier reference instead, so this scan can never see into
  // `someNamedFn`'s body to check for a self-skip/self-fixme/self-fail
  // annotation -- it must fail closed and treat the token as untrusted,
  // never silently assume the unexamined body is safe. Here the named
  // function genuinely does self-skip, proving the guard is load-bearing
  // and not merely conservative overkill against nothing.
  const source = `
    import { test } from "@playwright/test";
    async function realBody({}, testInfo) {
      test.skip(someRuntimeCondition, "only on some platforms");
    }
    test("does something [pw.synthetic-check:named-callback-self-skip]", realBody);
  `;
  const result = extractTokensFromSource(
    "synthetic-named-callback.spec.ts",
    source,
  );

  expect(
    result.statePairs.has("synthetic-check::named-callback-self-skip"),
  ).toBe(true);
  expect(
    result.trustedStatePairs.has("synthetic-check::named-callback-self-skip"),
  ).toBe(false);
  expect(result.skipGuardedTests).toEqual([
    {
      file: "synthetic-named-callback.spec.ts",
      title: "does something [pw.synthetic-check:named-callback-self-skip]",
    },
  ]);
});

test("a named-function callback reference is untrusted in the three-argument test(title, testDetails, callback) overload too", () => {
  // Same blind spot as above, but through the `{ tag, annotation }`
  // details-object overload: the named function is still the last
  // argument and still unresolvable by this scan.
  const source = `
    import { test } from "@playwright/test";
    async function realBody() {
      test.fixme(someRuntimeCondition, "only on some platforms");
    }
    test(
      "does something [pw.synthetic-check:three-arg-named-callback]",
      { tag: "@slow" },
      realBody,
    );
  `;
  const result = extractTokensFromSource(
    "synthetic-three-arg-named-callback.spec.ts",
    source,
  );

  expect(
    result.trustedStatePairs.has(
      "synthetic-check::three-arg-named-callback",
    ),
  ).toBe(false);
});

