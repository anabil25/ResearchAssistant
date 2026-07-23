"use client";

import {
  Bot,
  ChevronDown,
  ChevronUp,
  Cpu,
  Globe2,
  Plus,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  createAgentDraft,
  forkAgent,
  getAgentEvaluation,
  getAgentHealth,
  getAgentReleases,
  getAgentStudioCatalog,
  type WorkspaceData,
} from "@/lib/api";
import type {
  AgentDraftIntent,
  AgentEvaluationSummary,
  AgentHealthSummary,
  AgentReleaseSummary,
  AgentSummary,
  CapabilityId,
} from "@/lib/types";
import { buildLegacyAgentSummaries } from "@/lib/agent-catalog";
import { CAPABILITY_CARDS } from "@/components/workspace-views";
import {
  AsyncStateBanner,
  EmptyBlock,
  LoadingBlock,
  classifyAsyncError,
} from "@/components/async-state";

interface AgentRegistryProps {
  data: WorkspaceData | null;
  onOpenAgent: (agentId: string) => void;
}

type OwnerFilter = "all" | "platform" | "researcher";

function filterAgentSummaries(
  list: AgentSummary[],
  ownerFilter: OwnerFilter,
  query: string,
): AgentSummary[] {
  return list.filter((entry) => {
    const matchesOwner =
      ownerFilter === "all" || entry.owner_kind === ownerFilter;
    const matchesQuery = `${entry.name} ${entry.purpose ?? ""}`
      .toLowerCase()
      .includes(query.toLowerCase());
    return matchesOwner && matchesQuery;
  });
}

export function capabilityTitle(capability: CapabilityId | null): string | null {
  if (!capability) return null;
  return (
    CAPABILITY_CARDS.find((card) => card.id === capability)?.shortTitle ??
    capability
  );
}

export function AgentRegistryCard({
  entry,
  data,
  onOpenAgent,
}: {
  entry: AgentSummary;
  data: WorkspaceData | null;
  onOpenAgent: (agentId: string) => void;
}) {
  const live = (data?.agents ?? []).find((agent) => agent.id === entry.id);
  const [expanded, setExpanded] = useState(false);
  const [loadingLive, setLoadingLive] = useState(false);
  const [health, setHealth] = useState<AgentHealthSummary | null>(null);
  const [evaluation, setEvaluation] = useState<AgentEvaluationSummary | null>(
    null,
  );
  const [releases, setReleases] = useState<AgentReleaseSummary[] | null>(null);
  const [liveError, setLiveError] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);
  const [forking, setForking] = useState(false);
  const [forkResult, setForkResult] = useState<string | null>(null);

  const capabilityId = entry.capability;
  const studioRuns = capabilityId
    ? (data?.runs ?? []).filter((run) => run.capability === capabilityId)
        .length
    : 0;
  const workflowUses = capabilityId
    ? (data?.workflows ?? []).filter(
        (workflow) => workflow.capability === capabilityId,
      ).length
    : 0;

  const loadLiveStatus = () => {
    setExpanded((current) => !current);
    if (expanded || health || evaluation || releases || loadingLive) return;
    setLoadingLive(true);
    setLiveError(null);
    void Promise.allSettled([
      getAgentHealth(entry.id),
      getAgentEvaluation(entry.id),
      getAgentReleases(entry.id),
    ])
      .then(([healthResult, evalResult, releasesResult]) => {
        if (healthResult.status === "fulfilled") {
          setHealth(healthResult.value);
        }
        if (evalResult.status === "fulfilled") {
          setEvaluation(evalResult.value);
        }
        if (releasesResult.status === "fulfilled") {
          setReleases(releasesResult.value);
        }
        const firstFailure = [healthResult, evalResult, releasesResult].find(
          (result): result is PromiseRejectedResult =>
            result.status === "rejected",
        );
        if (
          firstFailure &&
          healthResult.status === "rejected" &&
          evalResult.status === "rejected" &&
          releasesResult.status === "rejected"
        ) {
          setLiveError(classifyAsyncError(firstFailure.reason));
        }
      })
      .finally(() => setLoadingLive(false));
  };

  const runFork = () => {
    setForking(true);
    setForkResult(null);
    void forkAgent(entry.id)
      .then((result) =>
        setForkResult(`Fork created as draft ${result.draft_id}.`),
      )
      .catch((error: unknown) => {
        const classified = classifyAsyncError(error);
        setForkResult(classified.message);
      })
      .finally(() => setForking(false));
  };

  const publicBoundaryLabel =
    entry.public_boundary.mode === "public_online"
      ? "Public web: read-only"
      : entry.public_boundary.mode === "none"
        ? "Public web: none"
        : "Public web: not available yet";

  return (
    <article className="panel agent-registry-card">
      <button
        type="button"
        className="agent-registry-card-main"
        onClick={() => onOpenAgent(entry.id)}
      >
        <div className="agent-registry-card-heading">
          <span className="agent-registry-icon">
            <Bot size={18} />
          </span>
          <div>
            <strong>{entry.name}</strong>
            <span className="agent-registry-owner" data-owner={entry.owner_kind}>
              {entry.owner_kind === "platform" ? "Platform-owned" : "Researcher-owned"}
            </span>
          </div>
          <span
            className="agent-registry-lifecycle"
            data-lifecycle={entry.lifecycle}
          >
            {entry.lifecycle.replace("_", " ")}
          </span>
        </div>
        <p className="agent-registry-purpose">
          {entry.purpose ?? "Purpose: not available yet from the current data source."}
        </p>
        <p className="agent-registry-boundary">
          {entry.boundary ?? "Boundary: not available yet from the current data source."}
        </p>
        <div className="agent-registry-chips">
          {entry.source === "legacy_agents_endpoint" ? (
            <span className="subtle-chip" data-tone="unavailable">
              Legacy summary (from /agents)
            </span>
          ) : null}
          {capabilityId ? (
            <span className="subtle-chip">{capabilityTitle(capabilityId)}</span>
          ) : null}
          <span className="subtle-chip">
            <Cpu size={12} /> {live?.model_tier || "Model tier not discovered yet"}
          </span>
          <span className="subtle-chip">
            <Globe2 size={12} /> {publicBoundaryLabel}
          </span>
          {(live?.workflow_steps ?? []).map((step) => (
            <span className="subtle-chip" key={step}>
              {step}
            </span>
          ))}
        </div>
        <dl className="agent-registry-facts">
          <div>
            <dt>Discovered project model</dt>
            <dd>
              {live?.deployment || entry.discovered_project_model || "Not discovered yet"}
            </dd>
          </div>
          <div>
            <dt>Studio usage</dt>
            <dd>
              {studioRuns} run{studioRuns === 1 ? "" : "s"}
            </dd>
          </div>
          <div>
            <dt>Workflow usage</dt>
            <dd>
              {workflowUses} workflow{workflowUses === 1 ? "" : "s"}
            </dd>
          </div>
        </dl>
      </button>

      <div className="agent-registry-card-footer">
        <button
          type="button"
          className="agent-registry-disclosure"
          aria-expanded={expanded}
          onClick={loadLiveStatus}
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          Live evaluation, health & versions
        </button>
        {entry.owner_kind === "platform" ? (
          <button
            type="button"
            className="agent-registry-fork"
            disabled={forking}
            onClick={runFork}
            title="Researchers can inspect and fork platform agents; only platform owners can version a system agent."
          >
            {forking ? "Forking…" : "Fork for my workspace"}
          </button>
        ) : null}
      </div>

      {expanded ? (
        <div className="agent-registry-live">
          {loadingLive ? (
            <LoadingBlock label="Checking live evaluation, health, and version history…" />
          ) : liveError ? (
            <AsyncStateBanner
              kind={liveError.kind}
              message={liveError.message}
            />
          ) : (
            <div className="agent-registry-live-grid">
              <div>
                <strong>Health</strong>
                <span>{health ? health.state : "Not available yet"}</span>
              </div>
              <div>
                <strong>Advisory evaluation</strong>
                <span>
                  {evaluation
                    ? `${evaluation.citation_resolution ?? "—"}% citation resolution`
                    : "Not available yet"}
                </span>
              </div>
              <div>
                <strong>Versions</strong>
                <span>
                  {releases && releases.length > 0
                    ? `${releases.length} recorded`
                    : "Immutable baseline only — no version history yet"}
                </span>
              </div>
            </div>
          )}
        </div>
      ) : null}
      {forkResult ? <p className="agent-registry-fork-result">{forkResult}</p> : null}
    </article>
  );
}

function CreateAgentPanel({ onClose }: { onClose: () => void }) {
  const [source, setSource] = useState<"template" | "blank">("template");
  const [templateCapability, setTemplateCapability] =
    useState<CapabilityId>("literature");
  const [intent, setIntent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<
    | { kind: "success"; message: string }
    | { kind: "unavailable"; message: string }
    | null
  >(null);

  const submit = () => {
    setSubmitting(true);
    setResult(null);
    const payload: AgentDraftIntent =
      source === "template"
        ? {
            source: "template",
            template_capability: templateCapability,
            intent,
          }
        : { source: "blank", intent };
    void createAgentDraft(payload)
      .then((response) =>
        setResult({
          kind: "success",
          message: `Draft agent ${response.draft_id} created.`,
        }),
      )
      .catch((error: unknown) => {
        const classified = classifyAsyncError(error);
        setResult({
          kind: "unavailable",
          message:
            classified.kind === "unavailable"
              ? "Agent creation isn't available yet — the workspace API doesn't expose a create-agent endpoint. Your intent is saved below so you can resume once backend support ships."
              : classified.message,
        });
      })
      .finally(() => setSubmitting(false));
  };

  return (
    <section className="panel agent-create-panel" aria-label="Create agent">
      <div className="agent-create-heading">
        <div>
          <span className="eyebrow">New agent</span>
          <h2>Start from a task template or a blank intent</h2>
        </div>
        <button type="button" onClick={onClose} aria-label="Close">
          Close
        </button>
      </div>
      <div className="segmented-control" role="tablist">
        <button
          type="button"
          data-active={source === "template"}
          onClick={() => setSource("template")}
        >
          Task template
        </button>
        <button
          type="button"
          data-active={source === "blank"}
          onClick={() => setSource("blank")}
        >
          Blank conversational intent
        </button>
      </div>
      {source === "template" ? (
        <label className="field">
          <span>Task template</span>
          <select
            value={templateCapability}
            onChange={(event) =>
              setTemplateCapability(event.target.value as CapabilityId)
            }
          >
            {CAPABILITY_CARDS.map((card) => (
              <option key={card.id} value={card.id}>
                {card.title}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <label className="field">
        <span>
          {source === "template"
            ? "What should this agent do differently from the template?"
            : "Describe what you want this agent to do"}
        </span>
        <textarea
          rows={4}
          value={intent}
          onChange={(event) => setIntent(event.target.value)}
          placeholder="e.g. Summarize weekly IRB submissions and flag missing consent language."
        />
      </label>
      <div className="agent-create-actions">
        <button
          type="button"
          className="primary-button"
          disabled={submitting || intent.trim().length === 0}
          onClick={submit}
        >
          {submitting ? "Creating…" : "Create draft agent"}
        </button>
      </div>
      {result ? (
        result.kind === "success" ? (
          <div className="save-status success" role="status">
            {result.message}
          </div>
        ) : (
          <AsyncStateBanner kind="unavailable" message={result.message} />
        )
      ) : null}
    </section>
  );
}

export function AgentRegistryView({ data, onOpenAgent }: AgentRegistryProps) {
  const [query, setQuery] = useState("");
  const [ownerFilter, setOwnerFilter] = useState<OwnerFilter>("all");
  const [creating, setCreating] = useState(false);
  const [catalog, setCatalog] = useState<AgentSummary[] | null>(null);
  const [catalogError, setCatalogError] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentStudioCatalog()
      .then((next) => {
        if (!cancelled) {
          setCatalog(next);
          setCatalogError(null);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) setCatalogError(classifyAsyncError(error));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const agents = useMemo<AgentSummary[]>(() => {
    if (catalog) return catalog;
    if (data) return buildLegacyAgentSummaries(data.agents);
    return [];
  }, [catalog, data]);

  const systemAgents = useMemo(
    () => agents.filter((entry) => entry.owner_kind === "platform"),
    [agents],
  );
  const yourAgents = useMemo(
    () => agents.filter((entry) => entry.owner_kind === "researcher"),
    [agents],
  );

  const applyFilters = (list: AgentSummary[]) =>
    filterAgentSummaries(list, ownerFilter, query);

  const filteredSystemAgents = useMemo(
    () => filterAgentSummaries(systemAgents, ownerFilter, query),
    [systemAgents, ownerFilter, query],
  );

  return (
    <div className="operational-page agent-registry-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Agent Registry</span>
          <h1>Agent Registry</h1>
          <p>
            Every agent — platform-built and researcher-built — is shown with
            the same behavioral contract: purpose, boundary, owner, knowledge,
            tools, capabilities, lifecycle, advisory evaluation, health, and
            usage. Platform owners version system agents; researchers can
            inspect and fork them.
          </p>
        </div>
        <button
          type="button"
          className="primary-button"
          onClick={() => setCreating(true)}
        >
          <Plus size={16} /> New agent
        </button>
      </header>

      {catalogError ? (
        <AsyncStateBanner
          kind="unavailable"
          message="The Agent Studio catalog (/agent-studio/agents) isn't available yet — showing a legacy summary built from /agents. Purpose and boundary text will populate once that endpoint ships."
        />
      ) : null}

      {creating ? (
        <CreateAgentPanel onClose={() => setCreating(false)} />
      ) : null}

      <div className="connector-toolbar agent-registry-toolbar">
        <label className="search-field">
          <Sparkles size={16} />
          <span className="sr-only">Search agents</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search agents by name or purpose"
          />
        </label>
        <div className="filter-pills">
          {(["all", "platform", "researcher"] as OwnerFilter[]).map(
            (filter) => (
              <button
                key={filter}
                data-active={ownerFilter === filter}
                onClick={() => setOwnerFilter(filter)}
              >
                {filter === "all"
                  ? "All"
                  : filter === "platform"
                    ? "Platform"
                    : "Researcher"}
              </button>
            ),
          )}
        </div>
      </div>

      {!data ? (
        <LoadingBlock label="Loading agent registry…" />
      ) : (
        <>
          <section aria-label="System agents">
            <div className="settings-section-heading">
              <div>
                <h2>System agents</h2>
                <p>
                  {systemAgents.length} platform-owned Hosted Agent
                  deployments, shown equally regardless of capability.
                </p>
              </div>
            </div>
            {filteredSystemAgents.length === 0 ? (
              <EmptyBlock
                title="No agents match this filter"
                description="Clear the search or owner filter to see every agent."
              />
            ) : (
              <div className="agent-registry-grid">
                {filteredSystemAgents.map((entry) => (
                  <AgentRegistryCard
                    key={entry.id}
                    entry={entry}
                    data={data}
                    onOpenAgent={onOpenAgent}
                  />
                ))}
              </div>
            )}
          </section>

          <section aria-label="Your agents">
            <div className="settings-section-heading">
              <div>
                <h2>Your agents</h2>
                <p>Researcher-created agents forked or built from scratch.</p>
              </div>
            </div>
            {yourAgents.length === 0 ? (
              <EmptyBlock
                title="No custom agents yet"
                description="Start from a task template or a blank conversational intent above to create your first agent."
                action={
                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => setCreating(true)}
                  >
                    <Plus size={14} /> New agent
                  </button>
                }
              />
            ) : (
              <div className="agent-registry-grid">
                {applyFilters(yourAgents).map((entry) => (
                  <AgentRegistryCard
                    key={entry.id}
                    entry={entry}
                    data={data}
                    onOpenAgent={onOpenAgent}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

