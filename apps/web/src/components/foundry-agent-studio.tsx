"use client";

import { Bot, Boxes, RefreshCw, Search } from "lucide-react";
import { useEffect, useState } from "react";

import { AsyncStateBanner, EmptyBlock, LoadingBlock, classifyAsyncError } from "@/components/async-state";
import { getFoundryAgentInventory } from "@/lib/api";
import type { FoundryAgentInventoryItem } from "@/lib/types";

function statusLabel(value: string | null): string {
  return value ? value.replaceAll("_", " ") : "Unknown";
}

function agentTypeLabel(value: FoundryAgentInventoryItem["agent_type"]): string {
  const labels: Record<FoundryAgentInventoryItem["agent_type"], string> = {
    hosted: "Hosted",
    prompt: "Prompt",
    workflow: "Workflow",
    external: "External",
    unknown: "Unknown type",
  };
  return labels[value];
}

// The API reports "no Foundry project configured" with the same 503 it uses for a
// real outage. Retrying a missing deployment setting can never succeed, so match
// the server's fixed wording to tell a configuration state apart from a failure.
function isFoundryUnconfigured(message: string): boolean {
  return message.toLowerCase().includes("no foundry project endpoint is configured");
}

export function FoundryAgentCatalog() {
  const [agents, setAgents] = useState<FoundryAgentInventoryItem[] | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<ReturnType<typeof classifyAsyncError> | null>(null);

  const load = () => {
    setAgents(null);
    setError(null);
    void getFoundryAgentInventory()
      .then(setAgents)
      .catch((next: unknown) => setError(classifyAsyncError(next)));
  };

  useEffect(() => {
    let cancelled = false;
    void getFoundryAgentInventory()
      .then((next) => {
        if (!cancelled) setAgents(next);
      })
      .catch((next: unknown) => {
        if (!cancelled) setError(classifyAsyncError(next));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const visibleAgents = (agents ?? []).filter((agent) =>
    `${agent.name} ${agent.description ?? ""}`.toLowerCase().includes(query.toLowerCase()),
  );

  return (
    <div className="operational-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Foundry project</span>
          <h1>Agents</h1>
          <p>Hosted services available in this project.</p>
        </div>
      </header>
      <div className="connector-toolbar">
        <label className="search-field">
          <Search size={16} />
          <span className="sr-only">Search agents</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by name or description"
          />
        </label>
        <button type="button" className="icon-button" aria-label="Refresh agent inventory" onClick={load}>
          <RefreshCw size={16} />
        </button>
      </div>
      {error ? (
        isFoundryUnconfigured(error.message) ? (
          <AsyncStateBanner
            kind="unavailable"
            message="No Foundry project is configured for this environment. Configure FOUNDRY_PROJECT_ENDPOINT to list deployed agents."
          />
        ) : (
          <AsyncStateBanner kind={error.kind} message={error.message} onRetry={load} />
        )
      ) : agents === null ? (
        <LoadingBlock label="Loading Foundry agent inventory..." />
      ) : visibleAgents.length === 0 ? (
        <EmptyBlock
          title={query ? "No agents match this search" : "No agents found"}
          description={query ? "Clear the search to see the project inventory." : "This project has no deployed agents."}
        />
      ) : (
        <div className="agent-registry-grid">
          {visibleAgents.map((agent) => (
            <article className="panel agent-registry-card" key={agent.name}>
              <div className="agent-registry-card-heading">
                <span className="agent-registry-icon">
                  {agent.agent_type === "hosted" ? <Boxes size={18} /> : <Bot size={18} />}
                </span>
                <div>
                  <strong>{agent.name}</strong>
                  <span className="agent-registry-owner">
                    {agentTypeLabel(agent.agent_type)}
                  </span>
                </div>
                <span className="agent-registry-lifecycle">{statusLabel(agent.status)}</span>
              </div>
              <p className="agent-registry-purpose">{agent.description ?? "No description was returned by Foundry."}</p>
              <dl className="agent-registry-facts">
                <div>
                  <dt>{agent.model_deployments.length === 1 ? "Model" : "Models"}</dt>
                  <dd>{agent.model_deployments.length > 0 ? agent.model_deployments.join(", ") : "Not reported"}</dd>
                </div>
                <div><dt>Version</dt><dd>{agent.version ?? "Not reported"}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
