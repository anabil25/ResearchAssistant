import type { NextConfig } from "next";

const isDevelopment = process.env.NODE_ENV !== "production";
const contentSecurityPolicy = [
  "default-src 'self'",
  "img-src 'self' data:",
  "style-src 'self' 'unsafe-inline'",
  `script-src 'self' 'unsafe-inline'${isDevelopment ? " 'unsafe-eval'" : ""}`,
  `connect-src 'self'${isDevelopment ? " ws: http://127.0.0.1:* http://localhost:*" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const nextConfig: NextConfig = {
  allowedDevOrigins: ["127.0.0.1"],
  // Overridable so concurrent invocations of the E2E gate in a *single*
  // checkout each build into their own directory. `npm run test:e2e` is
  // `next build && playwright test`, so every gate invocation runs a real
  // build; with the default shared `.next`, two simultaneous invocations
  // collide and one fails with "Another next build process is already
  // running". Per-invocation output/report directories did not help, because
  // the collision is in the build step that precedes Playwright entirely.
  // `scripts/run-e2e-coverage-gate.mjs` sets this (and
  // `scripts/start-standalone.mjs` reads the same variable) so the server it
  // starts serves the build this invocation actually produced.
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
          {
            key: "Content-Security-Policy",
            value: contentSecurityPolicy,
          },
        ],
      },
    ];
  },
};

export default nextConfig;
