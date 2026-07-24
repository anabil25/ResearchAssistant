import type { ConnectorSetting } from "@/lib/types";

/**
 * Normalized connector availability derived from the two real backend
 * fields that matter — `enabled` and `test_status` — mirroring the mapping
 * used by the Settings connector list (`connectorStatusInfo` in
 * `workspace-views.tsx`):
 *
 *   - `enabled === false`                         -> "disabled"
 *   - `test_status` "ready" | "ready_with_key"     -> "ready"
 *   - `test_status` "configuration_required"       -> "needs-connection"
 *   - `test_status` "unavailable"                  -> "unavailable"
 *   - anything else (e.g. never tested)            -> "untested"
 *
 * This is the single source of truth every surface that needs to decide
 * whether a connector can actually be used right now (not just whether an
 * administrator has switched it on) must consume — e.g. Matching Explorer's
 * source checkboxes and Grant's funding-source checkboxes, which previously
 * gated only on `enabled` and therefore could still select/submit a
 * connector whose latest probe reported `configuration_required` or
 * `unavailable`.
 *
 * Fail-closed: any `test_status` value that is not affirmatively "ready" or
 * "ready_with_key" is treated as not runnable. A connector is never
 * silently assumed usable.
 *
 * Note this is a UI-side derivation for display/gating only, not the
 * security boundary. The backend independently re-validates `enabled` (and
 * fails closed per-source with a truthful status at request time) before
 * any external retrieval call — see `retrieve_public_metadata` in
 * `services/api/src/research_assistant_api/public_research.py`.
 */
export type ConnectorAvailability =
  | "ready"
  | "needs-connection"
  | "unavailable"
  | "disabled"
  | "untested";

export function connectorAvailability(
  connector: Pick<ConnectorSetting, "enabled" | "test_status">,
): ConnectorAvailability {
  if (!connector.enabled) return "disabled";
  if (connector.test_status === "ready" || connector.test_status === "ready_with_key") {
    return "ready";
  }
  if (connector.test_status === "configuration_required") return "needs-connection";
  if (connector.test_status === "unavailable") return "unavailable";
  return "untested";
}

/**
 * True only for connectors that can actually be used as a live, runnable
 * source right now. Every non-"ready" availability (needs-connection,
 * unavailable, disabled, or untested) resolves to `false`, so a connector
 * that is merely `enabled` but unhealthy or unconfigured is never treated
 * as a runnable source.
 */
export function isConnectorRunnable(
  connector: Pick<ConnectorSetting, "enabled" | "test_status">,
): boolean {
  return connectorAvailability(connector) === "ready";
}

/** Short, human-readable caption for each non-ready availability category. */
export function connectorAvailabilityCaption(
  availability: ConnectorAvailability,
): string | null {
  switch (availability) {
    case "ready":
      return null;
    case "needs-connection":
      return "Needs connection setup";
    case "unavailable":
      return "Currently unavailable";
    case "disabled":
      return "Disabled in Settings";
    case "untested":
      return "Not yet tested";
  }
}
