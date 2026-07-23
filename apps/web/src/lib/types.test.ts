/**
 * Direct unit tests for the pure reconciliation helpers in `lib/types.ts`
 * introduced in the capability-contract redesign (descriptor/instance/
 * binding split, time-bound approval, derived staleness). These are exercised
 * indirectly through component tests too, but are covered here directly
 * against every branch so the reconciliation logic itself — stale
 * fingerprint, unknown maturity, expired approval, unavailable instance, and
 * expanded-vs-canonical reads — has an explicit, isolated test.
 */
import {
  isCapabilityApprovalActive,
  isCapabilityAttachable,
  resolveCapabilityBindingView,
  type CapabilityApprovalSummary,
  type CapabilityBinding,
  type CapabilityDescriptor,
  type CapabilityInstance,
} from "@/lib/types";

function descriptor(overrides: Partial<CapabilityDescriptor> = {}): CapabilityDescriptor {
  return {
    id: "web-search",
    version: "1.0.0",
    family: "web",
    operation: "search",
    risk_class: "read",
    description: "Search the public web.",
    digest: "sha256:desc1",
    ...overrides,
  };
}

function instance(overrides: Partial<CapabilityInstance> = {}): CapabilityInstance {
  return {
    id: "web-search-instance-1",
    descriptor_id: "web-search",
    descriptor_digest: "sha256:desc1",
    version: "3.2.0",
    fingerprint: "fp-1",
    tenant_id: "tenant-demo",
    workspace_id: "workspace-demo",
    maturity: "ga",
    lifecycle: "active",
    lifecycle_reason: null,
    provider: "bing",
    destination: null,
    readiness: "ready",
    ...overrides,
  };
}

function approval(overrides: Partial<CapabilityApprovalSummary> = {}): CapabilityApprovalSummary {
  return {
    status: "not_required",
    record_id: null,
    scope_hash: null,
    actor: null,
    expires_at: null,
    ...overrides,
  };
}

function binding(overrides: Partial<CapabilityBinding> = {}): CapabilityBinding {
  return {
    descriptor: { id: "web-search", version: "1.0.0" },
    operation: "search",
    instance: { id: "web-search-instance-1", version: "3.2.0", fingerprint: "fp-1" },
    configuration: null,
    connection: null,
    policy: null,
    provider_contract_version: "2024-06-01",
    destination_constraints: null,
    input_schema_digest: "sha256:in1",
    output_schema_digest: "sha256:out1",
    enabled: true,
    approval: approval(),
    ...overrides,
  };
}

describe("isCapabilityAttachable", () => {
  it("is false for a null/undefined instance", () => {
    expect(isCapabilityAttachable(null)).toBe(false);
    expect(isCapabilityAttachable(undefined)).toBe(false);
  });

  it("is false for unknown maturity even when readiness and maturity fields look attachable", () => {
    expect(
      isCapabilityAttachable(instance({ maturity: "unknown", readiness: "ready" })),
    ).toBe(false);
  });

  it("is false for a non-ready instance (unavailable/degraded/unknown readiness)", () => {
    expect(isCapabilityAttachable(instance({ readiness: "unavailable" }))).toBe(false);
    expect(isCapabilityAttachable(instance({ readiness: "degraded" }))).toBe(false);
    expect(isCapabilityAttachable(instance({ readiness: "unknown" }))).toBe(false);
  });

  it("is false for a ready, non-GA (preview) instance", () => {
    expect(isCapabilityAttachable(instance({ maturity: "preview" }))).toBe(false);
  });

  it("is false for a ready, GA instance whose lifecycle is not active (deprecated/retired) — maturity and lifecycle are independent", () => {
    expect(
      isCapabilityAttachable(instance({ maturity: "ga", lifecycle: "deprecated" })),
    ).toBe(false);
    expect(
      isCapabilityAttachable(instance({ maturity: "ga", lifecycle: "retired" })),
    ).toBe(false);
  });

  it("is true only for a ready, GA, active-lifecycle instance", () => {
    expect(
      isCapabilityAttachable(
        instance({ maturity: "ga", readiness: "ready", lifecycle: "active" }),
      ),
    ).toBe(true);
  });
});

describe("isCapabilityApprovalActive", () => {
  it("is true when approval is not required", () => {
    expect(isCapabilityApprovalActive(approval({ status: "not_required" }))).toBe(true);
  });

  it("is false for pending, rejected, expired, and revoked statuses", () => {
    for (const status of ["pending", "rejected", "expired", "revoked"] as const) {
      expect(isCapabilityApprovalActive(approval({ status }))).toBe(false);
    }
  });

  it("is true when approved with no expiry", () => {
    expect(
      isCapabilityApprovalActive(approval({ status: "approved", expires_at: null })),
    ).toBe(true);
  });

  it("is true when approved with a future expiry", () => {
    const now = new Date("2026-01-01T00:00:00Z");
    expect(
      isCapabilityApprovalActive(
        approval({ status: "approved", expires_at: "2026-06-01T00:00:00Z" }),
        now,
      ),
    ).toBe(true);
  });

  it("is false when approved but past its expiry — an expired approval is never active", () => {
    const now = new Date("2026-06-01T00:00:00Z");
    expect(
      isCapabilityApprovalActive(
        approval({ status: "approved", expires_at: "2026-01-01T00:00:00Z" }),
        now,
      ),
    ).toBe(false);
  });

  it("fails closed when approved but expires_at is unparsable", () => {
    expect(
      isCapabilityApprovalActive(
        approval({ status: "approved", expires_at: "not-a-real-date" }),
      ),
    ).toBe(false);
  });
});

describe("resolveCapabilityBindingView", () => {
  it("produces a non-stale expanded view when the resolved descriptor and instance agree with the pinned binding", () => {
    const view = resolveCapabilityBindingView(binding(), descriptor(), instance());
    expect(view.binding).toEqual(binding());
    expect(view.resolved_descriptor).toEqual(descriptor());
    expect(view.resolved_instance).toEqual(instance());
    expect(view.stale_reason).toBeNull();
  });

  it("is stale when the descriptor can no longer be resolved at all (canonical binding still exists, expanded read fails)", () => {
    const view = resolveCapabilityBindingView(binding(), null, instance());
    expect(view.resolved_descriptor).toBeNull();
    expect(view.stale_reason).toMatch(/descriptor is no longer resolvable/);
  });

  it("is stale when the instance can no longer be resolved at all — the unavailable-instance case", () => {
    const view = resolveCapabilityBindingView(binding(), descriptor(), null);
    expect(view.resolved_instance).toBeNull();
    expect(view.stale_reason).toMatch(/instance is no longer resolvable/);
  });

  it("is stale when the resolved instance's live fingerprint no longer matches the pinned instance.fingerprint", () => {
    const view = resolveCapabilityBindingView(
      binding({ instance: { id: "web-search-instance-1", version: "3.2.0", fingerprint: "fp-pinned" } }),
      descriptor(),
      instance({ fingerprint: "fp-drifted" }),
    );
    expect(view.stale_reason).toMatch(/fingerprint no longer matches/);
  });

  it("is stale when the descriptor's governance digest has changed since the instance was discovered", () => {
    const view = resolveCapabilityBindingView(
      binding(),
      descriptor({ digest: "sha256:desc-new" }),
      instance({ descriptor_digest: "sha256:desc-old" }),
    );
    expect(view.stale_reason).toMatch(/governance\/semantics digest has changed/);
  });

  it("expanded read is derived, never the canonical persisted binding shape", () => {
    // The expanded CapabilityBindingView wraps the canonical binding but adds
    // resolved_descriptor/resolved_instance/stale_reason — fields that must
    // never appear on the persisted CapabilityBinding itself.
    const canonical = binding();
    const view = resolveCapabilityBindingView(canonical, descriptor(), instance());
    expect(canonical).not.toHaveProperty("resolved_descriptor");
    expect(canonical).not.toHaveProperty("resolved_instance");
    expect(canonical).not.toHaveProperty("stale_reason");
    expect(view.binding).toEqual(canonical);
    expect(view).toHaveProperty("resolved_descriptor");
    expect(view).toHaveProperty("resolved_instance");
    expect(view).toHaveProperty("stale_reason");
  });
});
