/**
 * Deterministic policy for any external URL the product renders or opens
 * on behalf of a user (for example, a connector's provider "terms" link).
 *
 * The policy is intentionally conservative and centralized: it is the only
 * place that decides whether an externally-sourced URL is safe to expose as
 * a clickable link. Callers must never bypass this module to render a raw
 * `href` from untrusted/backend-controlled data.
 *
 * Rules (all must hold for a URL to be allowed):
 *  - Must parse as an absolute URL.
 *  - Scheme must be exactly `https:`.
 *  - Must not embed credentials (`user:pass@host`).
 *  - Must not specify a non-default port (only the implicit HTTPS port,
 *    443, is permitted).
 *  - Hostname must not be a loopback/private/link-local/internal address
 *    or a bare/`.local`/`.internal`/`.test` hostname.
 *  - Hostname must appear in the approved allowlist of research connector
 *    terms-of-service publishers.
 */

export type UrlPolicyRejectionReason =
  | "missing-url"
  | "unparseable-url"
  | "unsupported-scheme"
  | "embedded-credentials"
  | "unsafe-port"
  | "private-or-local-host"
  | "unapproved-host";

export type UrlPolicyDecision =
  | { allowed: true; url: string; host: string }
  | { allowed: false; reason: UrlPolicyRejectionReason };

export interface ExternalUrlPolicy {
  /**
   * Exact, lowercase hostnames approved by the owning product surface.
   * Keep these deterministic in production code; never derive them from
   * retrieved content or an API response.
   */
  allowedHosts: ReadonlySet<string>;
}

/**
 * Hosts that are known, approved publishers of terms-of-service documents
 * for the research connectors registered in
 * `packages/research_connectors/src/research_assistant_connectors/registry.py`.
 * Keep this list in sync with that registry when connectors are added.
 */
export const APPROVED_EXTERNAL_URL_HOSTS: ReadonlySet<string> = new Set([
  "www.ncbi.nlm.nih.gov",
  "pubmed.ncbi.nlm.nih.gov",
  "europepmc.org",
  "www.crossref.org",
  "openalex.org",
  "docs.openalex.org",
  "info.arxiv.org",
  "arxiv.org",
  "clinicaltrials.gov",
  "www.grants.gov",
  "grants.gov",
  "reporter.nih.gov",
  "datacite.org",
  "support.datacite.org",
  "info.orcid.org",
  "orcid.org",
  "ror.org",
  "www.semanticscholar.org",
]);

export const APPROVED_RESEARCH_SOURCE_HOSTS: ReadonlySet<string> = new Set([
  ...APPROVED_EXTERNAL_URL_HOSTS,
  "cdc.gov",
  "doi.org",
  "ghdx.healthdata.org",
  "healthdata.org",
  "jamanetwork.com",
  "nejm.org",
  "who.int",
  "www.cdc.gov",
  "www.healthdata.org",
  "www.jamanetwork.com",
  "www.nejm.org",
  "www.who.int",
]);

export const CONNECTOR_TERMS_URL_POLICY: ExternalUrlPolicy = {
  allowedHosts: APPROVED_EXTERNAL_URL_HOSTS,
};

export const RESEARCH_SOURCE_URL_POLICY: ExternalUrlPolicy = {
  allowedHosts: APPROVED_RESEARCH_SOURCE_HOSTS,
};

const LOOPBACK_HOSTS = new Set(["localhost", "0.0.0.0", "::1", "[::1]"]);
const PRIVATE_IPV4_PATTERNS: RegExp[] = [
  /^127\./,
  /^10\./,
  /^192\.168\./,
  /^169\.254\./,
  /^172\.(1[6-9]|2\d|3[0-1])\./,
];
const INTERNAL_SUFFIXES = [".local", ".internal", ".localhost", ".test"];

function isPrivateOrLocalHost(hostname: string): boolean {
  const lowered = hostname.toLowerCase();
  if (LOOPBACK_HOSTS.has(lowered)) return true;
  if (PRIVATE_IPV4_PATTERNS.some((pattern) => pattern.test(lowered))) {
    return true;
  }
  if (INTERNAL_SUFFIXES.some((suffix) => lowered.endsWith(suffix))) {
    return true;
  }
  // Bare hostnames with no dot (e.g. "server", "gateway") are never public.
  if (!lowered.includes(".")) return true;
  return false;
}

/**
 * Evaluate whether an externally-sourced URL is safe to render as a link
 * or open in a new tab. This is a pure, deterministic function: the same
 * input always produces the same decision, and it never performs network
 * access (no DNS resolution) -- only lexical/structural checks.
 */
export function evaluateExternalUrlPolicy(
  candidate: string | null | undefined,
  policy: ExternalUrlPolicy = CONNECTOR_TERMS_URL_POLICY,
): UrlPolicyDecision {
  if (!candidate || candidate.trim().length === 0) {
    return { allowed: false, reason: "missing-url" };
  }

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return { allowed: false, reason: "unparseable-url" };
  }

  if (parsed.protocol !== "https:") {
    return { allowed: false, reason: "unsupported-scheme" };
  }

  if (parsed.username.length > 0 || parsed.password.length > 0) {
    return { allowed: false, reason: "embedded-credentials" };
  }

  if (parsed.port.length > 0 && parsed.port !== "443") {
    return { allowed: false, reason: "unsafe-port" };
  }

  const hostname = parsed.hostname.toLowerCase();
  if (isPrivateOrLocalHost(hostname)) {
    return { allowed: false, reason: "private-or-local-host" };
  }

  if (!policy.allowedHosts.has(hostname)) {
    return { allowed: false, reason: "unapproved-host" };
  }

  return { allowed: true, url: parsed.toString(), host: hostname };
}

/** Human-readable explanation for a blocked decision, for UI display. */
export function describeUrlPolicyRejection(
  reason: UrlPolicyRejectionReason,
): string {
  switch (reason) {
    case "missing-url":
      return "No terms URL was provided by this connector.";
    case "unparseable-url":
      return "The terms URL is not a valid address.";
    case "unsupported-scheme":
      return "Only secure (https) links can be opened.";
    case "embedded-credentials":
      return "Links that embed credentials are not allowed.";
    case "unsafe-port":
      return "This link targets a non-standard port and is blocked.";
    case "private-or-local-host":
      return "This link targets a private or local network address and is blocked.";
    case "unapproved-host":
      return "This link targets a host that is not on the approved list.";
  }
}
