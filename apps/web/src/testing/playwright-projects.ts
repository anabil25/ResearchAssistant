// Single source of truth for the set of Playwright projects this repo's
// release gate requires to have actually executed at least one test.
// Referenced by both `playwright.config.ts` (to name the configured
// projects) and by `scripts/verify-playwright-runtime-coverage.mjs` / the
// atomic release-gate script (to validate, via `validateReportSchema`, that
// a completed run's JSON report genuinely reflects an execution under every
// one of these projects -- not e.g. a partial Chromium-only run silently
// satisfying the coverage check). Sharing this single array between config
// and verification means the two can never silently drift apart.
//
// Kept as its own zero-import module (like `interaction-manifest.ts` and
// `runtime-coverage-verifier.ts`) so it can be loaded standalone via
// on-the-fly `ts.transpileModule` from the CLI scripts without any
// node_modules-relative import resolution to worry about (unlike
// `playwright.config.ts` itself, which imports `@playwright/test` and so
// cannot be safely loaded that way from an arbitrary temp directory).
export const REQUIRED_PLAYWRIGHT_PROJECT_NAMES = [
  "chromium",
  "tablet-chromium",
  "mobile-chromium",
] as const;
