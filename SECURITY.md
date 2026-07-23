# Security and Responsible AI Boundary

## POC statement

This accelerator is a proof of concept. Its included data is synthetic and it
does not claim production readiness, legal advice, IRB approval, grant
compliance, or scientific validity.

## Implemented controls

- Managed identity for Azure service-to-service access.
- Dedicated Entra identities for every Foundry Hosted Agent.
- Keys/local authentication disabled for Foundry, Search, Cosmos, and Document
  Intelligence.
- Resource-scoped RBAC for Search, Storage, Cosmos, Durable Task,
  models, agent invocation, and Azure Monitor ingestion.
- Source ACL filtering before model context.
- Tenant context derived from the authenticated platform principal (or the
  explicit local demo identity), never trusted from the request body.
- Prompt-injection detection for instruction-like source content.
- No arbitrary browser, shell, or code-execution tool.
- Foundry Web Search is available only to selected profiles, only when the user
  enables it, and only when the request is classified public.
- Offline specialists are separate tool-free deployments. Public web tools
  exist only on three public-online deployments that receive a distinct
  acknowledged public query and no internal evidence. Institutional and
  dataset workflows cannot enable public web tools.
- Hosted Agents never query the multi-tenant Search index directly. The API
  applies tenant/source filters and passes only authorized evidence.
- Direct external connectors are source-allowlisted, result-bounded, and
  metadata-only.
- No external write, grant submission, or paid compute tool in an agent.
- Human approval records actor, timestamp, rationale, exact action,
  destination, and idempotency key, then resumes the same durable instance.
- Blob versioning/soft delete and durable run state.
- Prompt/content telemetry recording disabled by default.
- CSP, security headers, internal API ingress, non-root containers, health
  probes, dependency locking, and automated audits.
- Runtime uploads are MIME-allowlisted, bounded to 20 MB, checksummed, stored
  through managed identity and a Blob private endpoint, structurally extracted,
  and indexed only after deterministic metadata/ACL assignment.

## Production requirements

Before using institutional, confidential, regulated, export-controlled, or
participant data:

1. Enable Entra end-user authentication and tenant/group authorization.
2. Configure Search ACL fields from authoritative identity sources.
3. Use VNet integration, private endpoints/DNS, controlled egress, and disable
   public network access.
4. Complete data-residency, retention, deletion, backup/restore, legal-hold,
   DLP, and incident-response review.
5. Configure approved publisher/API terms, licenses, robots policies, and rate
   limits.
6. Validate Document Intelligence and model data-processing terms for the
   selected region/deployment type.
7. Run red-team and evaluation suites against representative institutional
   content, including indirect prompt injection and cross-tenant access tests.
8. Set budgets, concurrency, token, document-size, and external-compute limits.
9. Establish human owners for policy sources, retraction/correction review,
   grant facts, and generated artifacts.
10. Complete the institution's accessibility conformance assessment. Automated
    axe checks are evidence, not a WCAG conformance claim.

The included deployment intentionally uses a demo identity because the sample
contains synthetic/public data. Tenant-spoofing tests still prove that a body
tenant cannot override the server identity. Real institutional use must enable
Container Apps/Entra authentication, set the trusted-platform-header switch
only after the ingress overwrites client-supplied identity headers, and remove
the demo identity before data onboarding.

Foundry Web Search uses Grounding with Bing services. Its Data Protection
Addendum and geographic boundary differ from Azure-hosted application data.
Never send confidential, participant, export-controlled, or secret content to
public web search. Semantic Scholar and NCBI keys are optional third-party
credentials and must not be stored in agent environment variables or source.

## Reporting

Do not open a public issue for a suspected vulnerability involving credentials,
tenant data, or a deployed endpoint. Use the owning institution's private
security reporting process.
