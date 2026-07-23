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
      instance_id: ref.id,
      instance_version: null,
      instance_fingerprint: "unknown",
      input_schema_digest: null,
      output_schema_digest: null,
      config_ref: null,
      connection_ref: null,
      policy_ref: null,
      enabled: false,
      approval: {
        status: "not_required",
        record_id: null,
        scope_hash: null,
        actor: null,
        expires_at: null,
      },
    },
    resolved_descriptor: {
      id: ref.id,
      version: "unknown",
      family: ref.family,
      operation: ref.operation,
      risk_class: "read",
      description:
        "Adapted from a legacy capability reference; risk class defaulted to read pending real descriptor data.",
      digest: "unknown",
    },
    resolved_instance: null,
    stale_reason:
      "Adapted from the deprecated legacy AgentCapabilityRef shape — no real instance, fingerprint, or approval data is available.",
  };
}
