"use client";

import {
  BadgeCheck,
  Building2,
  CalendarDays,
  ExternalLink,
} from "lucide-react";

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

function exactGrantUrl(opportunity: VerifiedGrantOpportunity): string | null {
  const expected = `https://www.grants.gov/search-results-detail/${opportunity.grants_gov_id}`;
  if (opportunity.canonical_url !== expected) return null;
  const decision = evaluateExternalUrlPolicy(
    opportunity.canonical_url,
    RESEARCH_SOURCE_URL_POLICY,
  );
  return decision.allowed ? decision.url : null;
}

export function GrantOpportunityList({
  opportunities,
}: {
  opportunities: VerifiedGrantOpportunity[];
}) {
  const linkedOpportunities = opportunities.flatMap((opportunity) => {
    const url = exactGrantUrl(opportunity);
    return url ? [{ opportunity, url }] : [];
  });
  if (linkedOpportunities.length === 0) return null;

  return (
    <section className="grant-results" aria-label="Verified grant opportunities">
      <header className="grant-results-header">
        <span>
          <BadgeCheck size={16} aria-hidden="true" />
          Opportunities
        </span>
        <small>{linkedOpportunities.length} verified</small>
      </header>
      <ol className="grant-results-list">
        {linkedOpportunities.map(({ opportunity, url }) => {
          const posted = displayDate(opportunity.posted_date);
          const closes = displayDate(opportunity.close_date);
          const archived = displayDate(opportunity.archive_date);
          const status = opportunity.status.replaceAll("_", " ");
          return (
            <li key={opportunity.grants_gov_id}>
              <article className="grant-result">
                <div className="grant-result-kicker">
                  <span data-relevance={opportunity.relevance}>
                    {opportunity.relevance === "direct" ? "Direct match" : "Adjacent match"}
                  </span>
                  <span>{status}</span>
                </div>
                <h3>
                  <a href={url} target="_blank" rel="noopener noreferrer">
                    <span>{opportunity.opportunity_number}: </span>
                    {opportunity.title}
                    <ExternalLink size={15} aria-hidden="true" />
                    <span className="sr-only"> (opens in a new tab)</span>
                  </a>
                </h3>
                <div className="grant-result-facts">
                  <span>
                    <Building2 size={14} aria-hidden="true" />
                    {opportunity.agency}
                  </span>
                  {posted ? (
                    <span>
                      <CalendarDays size={14} aria-hidden="true" />
                      Posted {posted}
                    </span>
                  ) : null}
                  {closes ? (
                    <span>
                      <CalendarDays size={14} aria-hidden="true" />
                      Closes {closes}
                    </span>
                  ) : archived ? (
                    <span>
                      <CalendarDays size={14} aria-hidden="true" />
                      Archived {archived}
                    </span>
                  ) : null}
                </div>
                <p>{opportunity.relevance_rationale}</p>
              </article>
            </li>
          );
        })}
      </ol>
    </section>
  );
}