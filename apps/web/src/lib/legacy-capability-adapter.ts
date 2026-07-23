import type { AgentCapabilityRef, CapabilityBindingView } from "@/lib/types";

/**
 * The ONLY place in this codebase allowed to consume the deprecated,
 * flat `AgentCapabilityRef` shape. New Agent Studio surfaces must be built
 * against `CapabilityBindingView` (resolved descriptor + resolved instance +
 * persisted binding) instead — this adapter exists solely so a legacy
 * source that still returns the old flat ref shape can be displayed without
 * resurrecting `AgentCapabilityRef` as a first-class type anywhere else.
 * `legacy-capability-adapter.test.ts` fails the suite if any other source
 * file under `src/` references `AgentCapabilityRef`.
 *
 * The adapted view is always marked stale: a legacy ref carries no real
 * fingerprint, schema digest, or approval record, so it can never be
 * presented as a live, reconciled binding.
 */
export function adaptLegacyCapabilityRef(
  ref: AgentCapabilityRef,
): CapabilityBindingView {
  return {
    binding: {
      descriptor_id: ref.id,
      descriptor_version: "unknown",
      operation: ref.operation,
      instance_id: null,
      pinned_provider_version: null,
      schema_digest: null,
      config: {},
      connection_ref: null,
      policy_ref: null,
      attached_by: "unknown",
      attached_at: "unknown",
    },
    resolved_descriptor: {
      id: ref.id,
      version: "unknown",
      provider: "unknown",
      title: ref.family,
      description:
        "Adapted from a legacy capability reference; governance metadata defaulted pending real descriptor data.",
      operations: [
        {
          name: ref.operation,
          maturity: ref.maturity,
          operation_class: "read",
          side_effect_destinations: [],
          requires_approval: false,
          reason: null,
          source_url: null,
          source_version: null,
          last_verified_at: null,
        },
      ],
      auth_requirements: [],
      risk_tier: "unknown",
      data_boundary: "unknown",
      managed_foundry_native: false,
    },
    resolved_operation: null,
    resolved_instance: null,
    stale_reason:
      "Adapted from the deprecated legacy AgentCapabilityRef shape — no real instance, fingerprint, or approval data is available.",
    attachable: false,
  };
}
