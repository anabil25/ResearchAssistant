/**
 * Direct unit tests for the pure reconciliation helpers in `lib/types.ts`.
 * These field shapes are verified field-for-field against the backend's real
 * committed Pydantic models (`agent_studio/models.py`, commit `d6df0fe`):
 * `CapabilityBinding` (flat pinned refs, no `enabled`/approval),
 * `CapabilityDescriptor`/`CapabilityOperation` (five-value `maturity`, no
 * separate lifecycle), `CapabilityInstance` (three-value `readiness`,
 * tenant/project scope), and `StudioApprovalRecord` (version-scoped
 * approval, never per-binding). These are exercised indirectly through
 * component tests too, but are covered here directly against every branch:
 * stale descriptor version, missing operation, unavailable instance, unknown
 * maturity, expired version-level approval, and expanded-vs-canonical reads.
 */
import {
  defaultPublicBoundary,
  derivePublicBoundaryFromWebAccess,
  isCapabilityAttachable,
  isStudioApprovalActive,
  resolveCapabilityBindingView,
  MEMORY_SCOPES,
  type CapabilityBinding,
  type CapabilityDescriptor,
  type CapabilityInstance,
  type CapabilityOperation,
  type StudioApprovalRecord,
} from "@/lib/types";

function operation(overrides: Partial<CapabilityOperation> = {}): CapabilityOperation {
  return {
    name: "search",
    maturity: "ga",
    operation_class: "read",
    side_effect_destinations: [],
    requires_approval: false,
    reason: null,
    source_url: null,
    source_version: null,
    last_verified_at: null,
    ...overrides,
  };
}

function descriptor(overrides: Partial<CapabilityDescriptor> = {}): CapabilityDescriptor {
  return {
    id: "web-search",
    version: "1.0.0",
    provider: "bing",
    title: "Web search",
    description: "Search the public web.",
    operations: [operation()],
    auth_requirements: [],
    risk_tier: "low",
    data_boundary: "project",
    managed_foundry_native: false,
    ...overrides,
  };
}

function instance(overrides: Partial<CapabilityInstance> = {}): CapabilityInstance {
  return {
    id: "web-search-instance-1",
    tenant_id: "tenant-demo",
    project_id: "project-demo",
    descriptor_id: "web-search",
    discovered_provider_version: "3.2.0",
    readiness: "ready",
    health_status: "healthy",
    config_fingerprint: "fp-1",
    unavailable_reason: null,
    discovered_at: "2026-01-01T00:00:00Z",
    registered_by: "platform",
    ...overrides,
  };
}

function binding(overrides: Partial<CapabilityBinding> = {}): CapabilityBinding {
  return {
    descriptor_id: "web-search",
    descriptor_version: "1.0.0",
    operation: "search",
    instance_id: "web-search-instance-1",
    pinned_provider_version: "2024-06-01",
    schema_digest: "sha256:schema1",
    config: {},
    connection_ref: "conn-bing",
    policy_ref: null,
    attached_by: "researcher@example.com",
    attached_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function approvalRecord(
  overrides: Partial<StudioApprovalRecord> = {},
): StudioApprovalRecord {
  return {
    id: "approval-1",
    version_id: "version-1",
    kind: "release_promotion",
    state: "pending",
    gated_action: "promote",
    destination: "production",
    requested_by: "researcher@example.com",
    requested_at: "2026-01-01T00:00:00Z",
    evidence_summary: "Passed advisory evals.",
    risk: "low",
    idempotency_key: "idem-1",
    approver_id: null,
    decided_at: null,
    rationale: null,
    content_hash: null,
    expires_at: null,
    ...overrides,
  };
}

describe("isCapabilityAttachable", () => {
  it("is false for a null/undefined operation", () => {
    expect(isCapabilityAttachable(null, null)).toBe(false);
    expect(isCapabilityAttachable(undefined, null)).toBe(false);
  });

  it("is false for unknown maturity even when instance readiness looks attachable", () => {
    expect(
      isCapabilityAttachable(
        operation({ maturity: "unknown" }),
        instance({ readiness: "ready" }),
      ),
    ).toBe(false);
  });

  it("is false for preview, unavailable, and retired maturity — only ga ever attaches", () => {
    for (const maturity of ["preview", "unavailable", "retired"] as const) {
      expect(isCapabilityAttachable(operation({ maturity }), null)).toBe(false);
    }
  });

  it("is true for a ga operation that needs no discovered instance", () => {
    expect(isCapabilityAttachable(operation({ maturity: "ga" }), null)).toBe(true);
  });

  it("is false for a ga operation whose required instance is not ready (degraded/unavailable)", () => {
    expect(
      isCapabilityAttachable(operation(), instance({ readiness: "degraded" })),
    ).toBe(false);
    expect(
      isCapabilityAttachable(operation(), instance({ readiness: "unavailable" })),
    ).toBe(false);
  });

  it("is true for a ga operation with a ready required instance", () => {
    expect(
      isCapabilityAttachable(operation(), instance({ readiness: "ready" })),
    ).toBe(true);
  });
});

describe("isStudioApprovalActive", () => {
  it("is false for pending and rejected states", () => {
    expect(isStudioApprovalActive(approvalRecord({ state: "pending" }))).toBe(false);
    expect(isStudioApprovalActive(approvalRecord({ state: "rejected" }))).toBe(false);
  });

  it("is true when approved with no expiry", () => {
    expect(
      isStudioApprovalActive(approvalRecord({ state: "approved", expires_at: null })),
    ).toBe(true);
  });

  it("is true when approved with a future expiry", () => {
    const now = new Date("2026-01-01T00:00:00Z");
    expect(
      isStudioApprovalActive(
        approvalRecord({ state: "approved", expires_at: "2026-06-01T00:00:00Z" }),
        now,
      ),
    ).toBe(true);
  });

  it("is false when approved but past its expiry — an expired approval is never active", () => {
    const now = new Date("2026-06-01T00:00:00Z");
    expect(
      isStudioApprovalActive(
        approvalRecord({ state: "approved", expires_at: "2026-01-01T00:00:00Z" }),
        now,
      ),
    ).toBe(false);
  });

  it("fails closed when approved but expires_at is unparsable", () => {
    expect(
      isStudioApprovalActive(
        approvalRecord({ state: "approved", expires_at: "not-a-real-date" }),
      ),
    ).toBe(false);
  });
});

describe("resolveCapabilityBindingView", () => {
  it("produces a non-stale, attachable expanded view when descriptor/operation/instance all resolve and agree with the pinned binding", () => {
    const view = resolveCapabilityBindingView(binding(), descriptor(), instance());
    expect(view.binding).toEqual(binding());
    expect(view.resolved_descriptor).toEqual(descriptor());
    expect(view.resolved_operation).toEqual(operation());
    expect(view.resolved_instance).toEqual(instance());
    expect(view.stale_reason).toBeNull();
    expect(view.attachable).toBe(true);
  });

  it("is stale when the descriptor can no longer be resolved at all (canonical binding still exists, expanded read fails)", () => {
    const view = resolveCapabilityBindingView(binding(), null, instance());
    expect(view.resolved_descriptor).toBeNull();
    expect(view.resolved_operation).toBeNull();
    expect(view.stale_reason).toMatch(/descriptor is no longer resolvable/);
    expect(view.attachable).toBe(false);
  });

  it("is stale when the resolved descriptor's catalog version no longer matches the pinned descriptor_version", () => {
    const view = resolveCapabilityBindingView(
      binding({ descriptor_version: "1.0.0" }),
      descriptor({ version: "2.0.0" }),
      instance(),
    );
    expect(view.stale_reason).toMatch(/catalog version has changed/);
  });

  it("is stale when the pinned operation is no longer present on the resolved descriptor", () => {
    const view = resolveCapabilityBindingView(
      binding({ operation: "vanished-operation" }),
      descriptor(),
      instance(),
    );
    expect(view.resolved_operation).toBeNull();
    expect(view.stale_reason).toMatch(/operation is no longer present/);
    expect(view.attachable).toBe(false);
  });

  it("is stale when the instance can no longer be resolved at all — the unavailable-instance case", () => {
    const view = resolveCapabilityBindingView(binding(), descriptor(), null);
    expect(view.resolved_instance).toBeNull();
    expect(view.stale_reason).toMatch(/instance is no longer resolvable/);
    expect(view.attachable).toBe(false);
  });

  it("is not stale on a missing instance when the binding never pinned one (instance_id null)", () => {
    const view = resolveCapabilityBindingView(
      binding({ instance_id: null }),
      descriptor(),
      null,
    );
    expect(view.stale_reason).toBeNull();
    expect(view.attachable).toBe(true);
  });

  it("is non-attachable when the resolved operation is unknown maturity, independent of staleness", () => {
    const view = resolveCapabilityBindingView(
      binding(),
      descriptor({ operations: [operation({ maturity: "unknown" })] }),
      instance(),
    );
    expect(view.stale_reason).toBeNull();
    expect(view.attachable).toBe(false);
  });

  it("is non-attachable when the resolved required instance is unavailable, independent of staleness", () => {
    const view = resolveCapabilityBindingView(
      binding(),
      descriptor(),
      instance({ readiness: "unavailable" }),
    );
    expect(view.stale_reason).toBeNull();
    expect(view.attachable).toBe(false);
  });

  it("expanded read is derived, never the canonical persisted binding shape", () => {
    // The expanded CapabilityBindingView wraps the canonical binding but adds
    // resolved_descriptor/resolved_operation/resolved_instance/stale_reason/
    // attachable — fields that must never appear on the persisted
    // CapabilityBinding itself (no enabled/approval either — see the real
    // backend CapabilityBinding model).
    const canonical = binding();
    const view = resolveCapabilityBindingView(canonical, descriptor(), instance());
    expect(canonical).not.toHaveProperty("resolved_descriptor");
    expect(canonical).not.toHaveProperty("resolved_operation");
    expect(canonical).not.toHaveProperty("resolved_instance");
    expect(canonical).not.toHaveProperty("stale_reason");
    expect(canonical).not.toHaveProperty("attachable");
    expect(canonical).not.toHaveProperty("enabled");
    expect(canonical).not.toHaveProperty("approval");
    expect(view.binding).toEqual(canonical);
    expect(view).toHaveProperty("resolved_descriptor");
    expect(view).toHaveProperty("resolved_operation");
    expect(view).toHaveProperty("resolved_instance");
    expect(view).toHaveProperty("stale_reason");
    expect(view).toHaveProperty("attachable");
  });
});

describe("derivePublicBoundaryFromWebAccess / defaultPublicBoundary", () => {
  it("returns the all-null default boundary when web_access is missing or empty", () => {
    expect(derivePublicBoundaryFromWebAccess(undefined)).toEqual(defaultPublicBoundary());
    expect(derivePublicBoundaryFromWebAccess("")).toEqual(defaultPublicBoundary());
  });

  it("classifies public-facing access as public_online, locked to read-only/approval-gated", () => {
    const view = derivePublicBoundaryFromWebAccess("Public-only deployment");
    expect(view.mode).toBe("public_online");
    expect(view.write_destinations).toEqual([]);
    expect(view.approval_required).toBe(true);
    expect(view.outbound_data_boundary).toBe("Public-only deployment");
  });

  it("classifies explicit denial phrasing as none, with no write destinations/approval", () => {
    expect(derivePublicBoundaryFromWebAccess("Never direct").mode).toBe("none");
    const view = derivePublicBoundaryFromWebAccess("none");
    expect(view.mode).toBe("none");
    expect(view.write_destinations).toBeNull();
    expect(view.approval_required).toBe(false);
  });

  it("leaves genuinely ambiguous web_access text as null rather than guessing a mode", () => {
    const view = derivePublicBoundaryFromWebAccess("Some custom internal routing");
    expect(view.mode).toBeNull();
    expect(view.write_destinations).toBeNull();
    expect(view.approval_required).toBeNull();
    expect(view.outbound_data_boundary).toBe("Some custom internal routing");
  });
});

describe("MEMORY_SCOPES", () => {
  it("lists the four independent memory scopes in the canonical order used across the UI", () => {
    // A direct reference (not just a type-level usage) so this top-level
    // exported array's own module-evaluation statement is exercised
    // explicitly, in addition to the (already-guaranteed, since the module
    // demonstrably loads for every other export above) import-time
    // evaluation of this const.
    expect(MEMORY_SCOPES).toEqual(["conversation", "user", "project", "private-agent"]);
  });
});
