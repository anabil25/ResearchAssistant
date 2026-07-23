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
}

function scanSpecFiles(): SpecTokens {
  const bareIds = new Set<string>();
  const statePairs = new Set<string>();
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
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "test"
      ) {
        const title = node.arguments[0];
        if (title && ts.isStringLiteralLike(title)) {
          for (const match of title.text.matchAll(PLAYWRIGHT_ID_PATTERN)) {
            bareIds.add(match[1]);
          }
          for (const match of title.text.matchAll(
            PLAYWRIGHT_STATE_TOKEN_PATTERN,
          )) {
            statePairs.add(`${match[1]}::${match[2]}`);
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return { bareIds, statePairs };
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

  const { bareIds: implementedIds, statePairs: implementedStatePairs } =
    scanSpecFiles();

  const missing = [...requiredIds].filter((id) => !implementedIds.has(id));
  const orphaned = [...implementedIds].filter((id) => !requiredIds.has(id));

  // Missing: a declared (interaction, state) pair with no implemented token anywhere.
  const missingStates = [...requiredStatePairs]
    .filter((pair) => !implementedStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  // Orphaned: an implemented token whose (interaction, state) pair is not declared —
  // this also covers "a test claims an undeclared state" for a *known* interaction id,
  // and "a test references an interaction id that does not exist" for an unknown one.
  const orphanedStates = [...implementedStatePairs]
    .filter((pair) => !requiredStatePairs.has(pair))
    .map((pair) => pair.replace("::", ":"))
    .sort();

  // Explicit, separately reported: tokens whose interaction id isn't in the manifest
  // at all (a stricter subset of `orphanedStates`, called out for clearer diagnostics).
  const unknownInteractionStates = orphanedStates.filter((pair) => {
    const id = pair.slice(0, pair.lastIndexOf(":"));
    return !knownInteractionIds.has(id);
  });

  const report = {
    missing,
    orphaned,
    stateCoverage: {
      requiredStateCount: requiredStatePairs.size,
      implementedStateCount: implementedStatePairs.size,
      missingStates,
      orphanedStates,
      unknownInteractionStates,
    },
  };

  await testInfo.attach("functional-coverage-contract.json", {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  });

  expect(
    report,
    "manifest and executable Playwright IDs must form a complete, per-state contract " +
      "with zero missing states, zero orphaned tokens, and zero unmapped interaction ids",
  ).toEqual({
    missing: [],
    orphaned: [],
    stateCoverage: {
      requiredStateCount: requiredStatePairs.size,
      implementedStateCount: requiredStatePairs.size,
      missingStates: [],
      orphanedStates: [],
      unknownInteractionStates: [],
    },
  });
});

