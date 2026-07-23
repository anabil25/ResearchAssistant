/** @jest-environment node */

import { spawnSync } from "node:child_process";

describe("ResearchMarkdown with installed markdown libraries", () => {
  it("preserves allowed hash links through rehype-harden", () => {
    const script = String.raw`
      import { createRequire } from "node:module";
      import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
      import { join } from "node:path";
      import React from "react";
      import { renderToStaticMarkup } from "react-dom/server";
      import ts from "typescript";

      const sourcePath = join(
        process.cwd(),
        "src",
        "components",
        "research-markdown.tsx",
      );
      const outputDirectory = join(
        process.cwd(),
        ".next",
        "jest-integration",
        "research-markdown-" + process.pid,
      );
      const outputPath = join(outputDirectory, "research-markdown.cjs");

      mkdirSync(outputDirectory, { recursive: true });
      try {
        const source = readFileSync(sourcePath, "utf8");
        const compiled = ts.transpileModule(source, {
          compilerOptions: {
            esModuleInterop: true,
            jsx: ts.JsxEmit.ReactJSX,
            module: ts.ModuleKind.CommonJS,
            target: ts.ScriptTarget.ES2022,
          },
          fileName: sourcePath,
        }).outputText;
        writeFileSync(outputPath, compiled);

        const require = createRequire(import.meta.url);
        const { ResearchMarkdown } = require(outputPath);
        const html = renderToStaticMarkup(
          React.createElement(ResearchMarkdown, {
            content: "[**Methods**](#methods)",
          }),
        );
        process.stdout.write(html);
      } finally {
        rmSync(outputDirectory, { force: true, recursive: true });
      }
    `;
    const result = spawnSync(process.execPath, ["--input-type=module", "-"], {
      cwd: process.cwd(),
      encoding: "utf8",
      input: script,
    });

    expect(result.stderr).toBe("");
    expect(result.status).toBe(0);
    expect(result.stdout).toContain(
      '<a href="#methods" target="_blank" rel="noopener noreferrer"><strong>Methods</strong><span class="sr-only"> (opens in a new tab)</span></a>',
    );
    expect(result.stdout).not.toContain("[blocked]");
  });
});
