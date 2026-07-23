/**
 * Tests the deprecated-shape adapter itself, plus a guard: no source file
 * under `src/` other than `lib/types.ts` (the type definition) and this
 * adapter/its test may reference `AgentCapabilityRef`. New Agent Studio
 * surfaces must be built against `CapabilityBindingView` instead.
 */
import fs from "node:fs";
import path from "node:path";

import { adaptLegacyCapabilityRef } from "@/lib/legacy-capability-adapter";
import type { AgentCapabilityRef } from "@/lib/types";

describe("adaptLegacyCapabilityRef", () => {
  it("adapts a legacy flat capability ref into an always-stale CapabilityBindingView", () => {
    const ref: AgentCapabilityRef = {
      id: "legacy-web-search",
      family: "web",
      operation: "search",
      maturity: "ga",
    };
    const view = adaptLegacyCapabilityRef(ref);

    expect(view.binding.descriptor_id).toBe("legacy-web-search");
    expect(view.binding.operation).toBe("search");
    expect(view.binding.instance_id).toBeNull();
    expect(view.binding.pinned_provider_version).toBeNull();
    expect(view.binding.descriptor_digest).toBeNull();
    expect(view.binding.instance_fingerprint).toBeNull();
    expect(view.binding.input_schema_digest).toBeNull();
    expect(view.binding.output_schema_digest).toBeNull();
    expect(view.binding.config_hash).toBeNull();
    expect(view.binding.connection_ref).toBeNull();
    expect(view.binding.policy_ref).toBeNull();
    expect(view.resolved_descriptor).toEqual({
      id: "legacy-web-search",
      version: "unknown",
      provider: "unknown",
      title: "web",
      description:
        "Adapted from a legacy capability reference; governance metadata defaulted pending real descriptor data.",
      operations: [
        {
          name: "search",
          maturity: "ga",
          operation_class: "read",
          side_effect_destinations: [],
          requires_approval: false,
          reason: null,
          source_url: null,
          source_version: null,
          last_verified_at: null,
          input_schema_digest: null,
          output_schema_digest: null,
        },
      ],
      auth_requirements: [],
      risk_tier: "unknown",
      data_boundary: "unknown",
      managed_foundry_native: false,
    });
    expect(view.resolved_operation).toBeNull();
    expect(view.resolved_instance).toBeNull();
    expect(view.attachable).toBe(false);
    expect(view.stale_reason).toMatch(/deprecated legacy AgentCapabilityRef shape/);
  });
});

describe("AgentCapabilityRef must stay behind the legacy adapter", () => {
  const srcRoot = path.resolve(__dirname, "..");
  const allowedFiles = new Set(
    [
      path.join(srcRoot, "lib", "types.ts"),
      path.join(srcRoot, "lib", "legacy-capability-adapter.ts"),
      path.join(srcRoot, "lib", "legacy-capability-adapter.test.ts"),
    ].map((file) => path.normalize(file)),
  );

  function walk(dir: string): string[] {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    return entries.flatMap((entry) => {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) return walk(fullPath);
      if (/\.(ts|tsx)$/.test(entry.name)) return [fullPath];
      return [];
    });
  }

  it("is never referenced outside lib/types.ts and the legacy adapter", () => {
    const offendingFiles: string[] = [];
    for (const file of walk(srcRoot)) {
      if (allowedFiles.has(path.normalize(file))) continue;
      const contents = fs.readFileSync(file, "utf8");
      if (contents.includes("AgentCapabilityRef")) {
        offendingFiles.push(path.relative(srcRoot, file));
      }
    }
    expect(offendingFiles).toEqual([]);
  });
});
