import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";
import ts from "typescript";

import { UI_COVERAGE_MANIFEST } from "../src/testing/interaction-manifest";

const PLAYWRIGHT_ID_PATTERN = /\[(pw\.[a-z0-9.-]+)\]/g;

function implementedPlaywrightIds(): Set<string> {
  const ids = new Set<string>();
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
            ids.add(match[1]);
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }
  return ids;
}

test("interaction manifest has no missing or orphaned Playwright IDs", async (
  {},
  testInfo,
) => {
  const requiredIds = new Set(
    UI_COVERAGE_MANIFEST.flatMap((interaction) => [
      ...interaction.playwrightTestIds,
      ...interaction.playwrightStateTestIds.flatMap(({ testIds }) => testIds),
    ]),
  );
  const implementedIds = implementedPlaywrightIds();
  const missing = [...requiredIds].filter((id) => !implementedIds.has(id));
  const orphaned = [...implementedIds].filter((id) => !requiredIds.has(id));
  const statesWithoutTests = UI_COVERAGE_MANIFEST.flatMap((interaction) =>
    interaction.playwrightStateTestIds
      .filter(({ testIds }) => testIds.length === 0)
      .map(({ state }) => `${interaction.id}:${state}`),
  );

  await testInfo.attach("functional-coverage-contract.json", {
    body: JSON.stringify({ missing, orphaned, statesWithoutTests }, null, 2),
    contentType: "application/json",
  });

  expect(
    { missing, orphaned, statesWithoutTests },
    "manifest and executable Playwright IDs must form a complete contract",
  ).toEqual({ missing: [], orphaned: [], statesWithoutTests: [] });
});
