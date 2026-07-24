import { cpSync, existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

// Must agree with `next.config.ts`'s `distDir`. A gate invocation sets
// NEXT_DIST_DIR so its `next build` and the server started from that build
// both use the same invocation-unique directory, which is what makes two
// concurrent gate invocations in a single checkout possible at all (the
// default shared `.next` fails the second build with "Another next build
// process is already running").
const distDir = process.env.NEXT_DIST_DIR ?? ".next";
const standaloneRoot = path.join(distDir, "standalone");

if (!existsSync(path.join(standaloneRoot, "server.js"))) {
  throw new Error(
    `Run \`npm run build\` before starting the standalone server (looked in ${standaloneRoot}).`,
  );
}

if (existsSync("public")) {
  cpSync("public", path.join(standaloneRoot, "public"), { recursive: true });
}
// Next emits the standalone server expecting its static assets under the same
// `distDir` name it was built with, nested inside the standalone root.
cpSync(
  path.join(distDir, "static"),
  path.join(standaloneRoot, distDir, "static"),
  { recursive: true },
);

process.env.HOSTNAME ??= "0.0.0.0";
process.env.PORT ??= "3000";
await import(
  pathToFileURL(path.resolve(standaloneRoot, "server.js")).href
);
