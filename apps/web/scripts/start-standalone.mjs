import { cpSync, existsSync } from "node:fs";

const standaloneRoot = ".next/standalone";
if (!existsSync(`${standaloneRoot}/server.js`)) {
  throw new Error("Run `npm run build` before starting the standalone server.");
}

if (existsSync("public")) {
  cpSync("public", `${standaloneRoot}/public`, { recursive: true });
}
cpSync(".next/static", `${standaloneRoot}/.next/static`, { recursive: true });

process.env.HOSTNAME ??= "0.0.0.0";
process.env.PORT ??= "3000";
await import("../.next/standalone/server.js");
