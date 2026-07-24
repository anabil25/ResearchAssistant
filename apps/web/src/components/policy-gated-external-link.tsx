"use client";

/**
 * Reusable rendering of the {@link evaluateExternalUrlPolicy} decision for any
 * externally-sourced URL the product wants to expose as a clickable link (for
 * example, a connector's provider "terms of service" link).
 *
 * This is intentionally a plain, dependency-light component living under the
 * shared `src/components/` path (not inside any single feature view) so any
 * surface that renders a backend-controlled external URL -- the research
 * workspace's connector manager today, Agent Studio's connections view once
 * it merges, or any future consumer -- can render the same allow/blocked UI
 * without re-implementing the policy check or its accessible markup.
 *
 * Callers must never render a raw, unvalidated `href` from untrusted data;
 * always go through this component (or {@link evaluateExternalUrlPolicy}
 * directly, if a bespoke layout is required) instead.
 */

import type { ReactNode } from "react";
import { Lock } from "lucide-react";

import {
  describeUrlPolicyRejection,
  evaluateExternalUrlPolicy,
  type ExternalUrlPolicy,
} from "@/lib/url-policy";

export interface PolicyGatedExternalLinkProps {
  /** The raw, potentially-untrusted URL supplied by backend/connector data. */
  url: string | null | undefined;
  /** Link label/content shown when the URL is allowed (e.g. text + icon). */
  children: ReactNode;
  /** Optional class applied to both the allowed anchor and blocked status span. */
  className?: string;
  /** A deterministic surface-owned allowlist; defaults to connector terms hosts. */
  policy?: ExternalUrlPolicy;
}

/**
 * Renders an externally-sourced URL as a real `<a>` link when it passes
 * {@link evaluateExternalUrlPolicy}, or a visible, accessible "blocked/
 * unavailable" status indicator (with the specific rejection reason) when it
 * does not. Never silently drops the link with no visible state.
 */
export function PolicyGatedExternalLink({
  url,
  children,
  className,
  policy,
}: PolicyGatedExternalLinkProps) {
  const decision = evaluateExternalUrlPolicy(url, policy);

  if (decision.allowed) {
    return (
      <a
        href={decision.url}
        target="_blank"
        rel="noopener noreferrer"
        data-terms-state="ready"
        className={className}
      >
        {children}
      </a>
    );
  }

  const reason = describeUrlPolicyRejection(decision.reason);
  return (
    <span
      className={
        className ? `connector-terms-blocked ${className}` : "connector-terms-blocked"
      }
      role="status"
      data-terms-state="blocked-url"
      aria-label={reason}
    >
      <Lock size={13} aria-hidden="true" />
      {reason}
    </span>
  );
}
