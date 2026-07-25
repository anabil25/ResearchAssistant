import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    // Invocation-unique Next build directories produced by concurrent E2E
    // gate runs (see scripts/gate-invocation-paths.mjs). Same rationale as
    // `.next/**`: generated build output, and there can be many copies of it.
    ".next-gate/**",
    "out/**",
    "build/**",
    "coverage/**",
    "playwright-report/**",
    "test-results/**",
    "next-env.d.ts",
    // Generated Jest coverage reports (gitignored) should never be linted.
    "coverage/**",
  ]),
]);

export default eslintConfig;
