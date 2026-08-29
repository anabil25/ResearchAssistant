"use client";

import { ExternalLink } from "lucide-react";

import type { VerifiedGrantOpportunity } from "@/lib/types";
import {
  evaluateExternalUrlPolicy,
  RESEARCH_SOURCE_URL_POLICY,
} from "@/lib/url-policy";

function displayDate(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeZone: "UTC",
  }).format(parsed);
}

function compactUsd(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function awardAmount(opportunity: VerifiedGrantOpportunity): string | null {
  const ceiling = opportunity.award_ceiling ?? 0;
  const floor = opportunity.award_floor ?? 0;
  if (ceiling > 0 && floor > 0 && floor !== ceiling) {
    return `${compactUsd(floor)}\u2013${compactUsd(ceiling)}`;
  }
  if (ceiling > 0) return compactUsd(ceiling);
  if (floor > 0) return `${compactUsd(floor)}+`;
  return null;
}

function exactGrantUrl(opportunity: VerifiedGrantOpportunity): string | null {
  const expected = `https://www.grants.gov/search-results-detail/${opportunity.grants_gov_id}`;
  if (opportunity.canonical_url !== expected) return null;
  const decision = evaluateExternalUrlPolicy(
    opportunity.canonical_url,
    RESEARCH_SOURCE_URL_POLICY,
  );
  return decision.allowed ? decision.url : null;
}

function linkedGrantOpportunities(opportunities: VerifiedGrantOpportunity[]) {
  return opportunities.flatMap((opportunity) => {
    const url = exactGrantUrl(opportunity);
    return url ? [{ opportunity, url }] : [];
  });
}

export function verifiedGrantReferenceLinks(
  opportunities: VerifiedGrantOpportunity[],
): Record<string, string> {
  return Object.fromEntries(
    linkedGrantOpportunities(opportunities).flatMap(({ opportunity, url }) => [
      [opportunity.opportunity_number, url],
      [opportunity.grants_gov_id, url],
    ]),
  );
}

export function GrantOpportunityList({
  opportunities,
}: {
  opportunities: VerifiedGrantOpportunity[];
}) {
  const linkedOpportunities = linkedGrantOpportunities(opportunities);
  if (linkedOpportunities.length === 0) return null;

  return (
    <section className="grant-results" aria-label="Verified grant opportunities">
      <header className="grant-results-header">
        <span>Verified opportunities</span>
        <small>
          {linkedOpportunities.length} {linkedOpportunities.length === 1 ? "result" : "results"}
        </small>
      </header>
      <div className="grant-results-table-wrap">
        <table className="grant-results-table">
          <caption className="sr-only">
            Grants.gov opportunities verified from provider records
          </caption>
          <colgroup>
            <col className="grant-col-opportunity" />
            <col className="grant-col-agency" />
            <col className="grant-col-award" />
            <col className="grant-col-availability" />
            <col className="grant-col-fit" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Opportunity</th>
              <th scope="col">Agency</th>
              <th scope="col">Award</th>
              <th scope="col">Availability</th>
              <th scope="col">Fit</th>
            </tr>
          </thead>
          <tbody>
            {linkedOpportunities.map(({ opportunity, url }) => {
              const posted = displayDate(opportunity.posted_date);
              const closes = displayDate(opportunity.close_date);
              const archived = displayDate(opportunity.archive_date);
              const status = opportunity.status.replaceAll("_", " ");
              const award = awardAmount(opportunity);
              const date = closes
                ? `Closes ${closes}`
                : posted
                  ? `Posted ${posted}`
                  : archived
                    ? `Archived ${archived}`
                    : "Date not announced";
              const fit =
                opportunity.relevance === "direct"
                  ? "Direct"
                  : opportunity.relevance === "adjacent"
                    ? "Adjacent"
                    : "Review fit";
              return (
                <tr key={opportunity.grants_gov_id}>
                  <td className="grant-opportunity" data-label="Opportunity">
                    <a
                      className="grant-opportunity-number"
                      href={url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {opportunity.opportunity_number}
                      <ExternalLink size={13} aria-hidden="true" />
                      <span className="sr-only"> (opens in a new tab)</span>
                    </a>
                    <strong>{opportunity.title}</strong>
                    <small>{opportunity.relevance_rationale}</small>
                  </td>
                  <td data-label="Agency">{opportunity.agency}</td>
                  <td className="grant-award" data-label="Award">
                    {award ?? <span className="grant-award-empty">Not stated</span>}
                  </td>
                  <td data-label="Availability">
                    <span className="grant-status">{status}</span>
                    <small>{date}</small>
                  </td>
                  <td data-label="Fit">
                    <span
                      className="grant-fit"
                      data-relevance={opportunity.relevance}
                    >
                      {fit}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}