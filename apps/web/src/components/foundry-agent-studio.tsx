"use client";

import { Bot, Boxes, CheckCircle2, Code2, Database, Plus, RefreshCw, Search } from "lucide-react";
import { useEffect, useState } from "react";

import { AsyncStateBanner, EmptyBlock, LoadingBlock, classifyAsyncError } from "@/components/async-state";
import {
  attachPromptCapability,
  createPromptAgentDraft,
  getFoundryAgentInventory,
  getFoundryProjectContext,
  getFoundryProjectModels,
  getCapabilityDiscovery,
  savePromptAgentDraft,
} from "@/lib/api";
import {
  isCapabilityAttachable,
  type CapabilityDiscovery,
  type FoundryAgentInventoryItem,
  type FoundryModelDeployment,
  type FoundryProjectContext,
  type PromptAgentDraft,
} from "@/lib/types";

function statusLabel(value: string | null): string {
  return value ? value.replaceAll("_", " ") : "Unknown";
}

// The API reports "no Foundry project configured" with the same 503 it uses for a
// real outage. Retrying a missing deployment setting can never succeed, so match
// the server's fixed wording to tell a configuration state apart from a failure.
function isFoundryUnconfigured(message: string): boolean {
  return message.toLowerCase().includes("no foundry project endpoint is configured");
}

export function FoundryAgentCatalog({
  onCreatePrompt,
}: {
  onCreatePrompt: () => void;
}) {
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
          <p>Hosted services and published prompt agents available in this project.</p>
        </div>
        <button type="button" className="primary-button" onClick={onCreatePrompt}>
          <Plus size={16} /> New prompt agent
        </button>
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
            message="No Foundry project is configured for this environment, so there is no agent inventory to list. Local development runs offline against a synthetic corpus; deployed environments receive FOUNDRY_PROJECT_ENDPOINT from infrastructure."
          />
        ) : (
          <AsyncStateBanner kind={error.kind} message={error.message} onRetry={load} />
        )
      ) : agents === null ? (
        <LoadingBlock label="Loading Foundry agent inventory..." />
      ) : visibleAgents.length === 0 ? (
        <EmptyBlock
          title={query ? "No agents match this search" : "No agents found"}
          description={query ? "Clear the search to see the project inventory." : "Create a prompt agent to add one to this project."}
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
                    {agent.agent_type === "hosted" ? "Hosted" : agent.agent_type === "prompt" ? "Prompt" : "Unknown type"}
                  </span>
                </div>
                <span className="agent-registry-lifecycle">{statusLabel(agent.status)}</span>
              </div>
              <p className="agent-registry-purpose">{agent.description ?? "No description was returned by Foundry."}</p>
              <dl className="agent-registry-facts">
                <div><dt>Model</dt><dd>{agent.model ?? "Not reported"}</dd></div>
                <div><dt>Version</dt><dd>{agent.version ?? "Not reported"}</dd></div>
              </dl>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

export function PromptAgentBuilder({
  onViewAgents,
}: {
  onViewAgents: () => void;
}) {
  const [context, setContext] = useState<FoundryProjectContext | null>(null);
  const [models, setModels] = useState<FoundryModelDeployment[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityDiscovery | null>(null);
  const [model, setModel] = useState("");
  const [agentId, setAgentId] = useState("agent-");
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [codeInterpreter, setCodeInterpreter] = useState(false);
  const [draft, setDraft] = useState<PromptAgentDraft | null>(null);
  const [loadError, setLoadError] = useState<ReturnType<typeof classifyAsyncError> | null>(null);
  const [saveError, setSaveError] = useState<ReturnType<typeof classifyAsyncError> | null>(null);
  const [toolError, setToolError] = useState<ReturnType<typeof classifyAsyncError> | null>(null);
  const [saved, setSaved] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getFoundryProjectContext()
      .then(async (nextContext) => {
        const [modelResult, capabilityResult] = await Promise.allSettled([
          getFoundryProjectModels(),
          getCapabilityDiscovery(nextContext.project_id),
        ]);
        if (cancelled) return;
        setContext(nextContext);
        if (modelResult.status === "fulfilled") {
          setModels(modelResult.value);
          setModel(modelResult.value[0]?.deployment_name ?? "");
        } else {
          setLoadError(classifyAsyncError(modelResult.reason));
        }
        if (capabilityResult.status === "fulfilled") {
          setCapabilities(capabilityResult.value);
        } else {
          setToolError(classifyAsyncError(capabilityResult.reason));
        }
      })
      .catch((next: unknown) => {
        if (!cancelled) setLoadError(classifyAsyncError(next));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const codeInterpreterOperation = capabilities?.descriptors
    .find((descriptor) => descriptor.id === "foundry.code_interpreter")
    ?.operations.find((operation) => operation.name === "run");
  const codeInterpreterAvailable = isCapabilityAttachable(codeInterpreterOperation, null);
  const configuredToolNames = (capabilities?.descriptors ?? [])
    .filter((descriptor) => descriptor.managed_foundry_native)
    .flatMap((descriptor) =>
      descriptor.operations.map((operation) => ({ descriptor, operation })),
    )
    .filter(({ operation }) => operation.maturity === "ga" && operation.lifecycle === "active")
    .filter(({ descriptor }) => descriptor.id !== "foundry.code_interpreter")
    .map(({ descriptor }) => descriptor.title);
  const agentIdError =
    agentId.trim().length > 0 && !/^agent-[a-z0-9-]{3,80}$/.test(agentId.trim())
      ? "Use agent- followed by 3 to 80 lowercase letters, digits, or hyphens."
      : null;
  const draftAlreadyHasCodeInterpreter = (draft?.manifest.capabilities ?? []).some((binding) => {
    if (!binding || typeof binding !== "object") return false;
    const descriptor = (binding as { descriptor_ref?: { id?: unknown } }).descriptor_ref;
    return descriptor?.id === "foundry.code_interpreter";
  });

  const submit = async () => {
    const selectedModel = models.find((item) => item.deployment_name === model);
    if (!context || !selectedModel || !displayName.trim() || !instructions.trim() || agentIdError) return;
    setSubmitting(true);
    setSaveError(null);
    setSaved(false);
    try {
      let workingDraft = draft;
      if (!workingDraft) {
        workingDraft = await createPromptAgentDraft({
          logical_agent_id: agentId.trim(),
          project_id: context.project_id,
          display_name: displayName.trim(),
          description: description.trim(),
        });
        setDraft(workingDraft);
      }
      let nextCapabilities = workingDraft.manifest.capabilities;
      if (codeInterpreter && !draftAlreadyHasCodeInterpreter) {
        nextCapabilities = [
          ...nextCapabilities,
          await attachPromptCapability({
            descriptor_id: "foundry.code_interpreter",
            operation: "run",
          }),
        ];
      }
      const savedDraft = await savePromptAgentDraft({
        ...workingDraft,
        manifest: {
          ...workingDraft.manifest,
          instructions: instructions.trim(),
          model_deployment: selectedModel,
          capabilities: nextCapabilities,
        },
      });
      setDraft(savedDraft);
      setSaved(true);
    } catch (next: unknown) {
      setSaveError(classifyAsyncError(next));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="operational-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Foundry prompt agent</span>
          <h1>Prompt builder</h1>
          <p>Create a governed draft from a project-deployed model and approved tool configuration.</p>
        </div>
      </header>
      {loadError ? <AsyncStateBanner kind={loadError.kind} message={loadError.message} /> : null}
      {!loadError && !context ? <LoadingBlock label="Loading Foundry project context..." /> : null}
      {context ? (
        <>
          {saveError ? <AsyncStateBanner kind={saveError.kind} message={saveError.message} /> : null}
          {saved ? (
            <div className="save-status success" role="status">
              <CheckCircle2 size={16} /> Draft saved. Run release gates before publishing.
              <button type="button" className="secondary-button" onClick={onViewAgents}>View agents</button>
            </div>
          ) : null}
          <form
        className="panel agent-create-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {draft ? <p className="agent-builder-draft-note">Draft {draft.logical_agent_id} is retained while you save changes.</p> : null}
        <label className="field" htmlFor="prompt-agent-id">
          <span>Agent ID</span>
          <input
            id="prompt-agent-id"
            aria-label="Agent ID"
            value={agentId}
            disabled={Boolean(draft)}
            aria-invalid={Boolean(agentIdError)}
            aria-describedby={agentIdError ? "agent-id-error" : undefined}
            onChange={(event) => setAgentId(event.target.value)}
          />
          {agentIdError ? <small id="agent-id-error" className="field-error">{agentIdError}</small> : null}
        </label>
        <label className="field" htmlFor="prompt-agent-display-name"><span>Display name</span><input id="prompt-agent-display-name" aria-label="Display name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label>
        <label className="field" htmlFor="prompt-agent-description"><span>Description</span><textarea id="prompt-agent-description" aria-label="Description" rows={2} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
        <label className="field" htmlFor="prompt-agent-instructions"><span>Instructions</span><textarea id="prompt-agent-instructions" aria-label="Instructions" rows={6} value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label>
        <label className="field" htmlFor="prompt-agent-model">
          <span>Project model</span>
          <select id="prompt-agent-model" aria-label="Project model" value={model} onChange={(event) => setModel(event.target.value)}>
            {models.map((item) => <option value={item.deployment_name} key={item.deployment_name}>{item.deployment_name} ({item.model_name})</option>)}
          </select>
        </label>
        <fieldset className="agent-tool-picker">
          <legend>Approved tools</legend>
          {toolError ? <AsyncStateBanner kind={toolError.kind} message={toolError.message} /> : null}
          {capabilities === null && !toolError ? <LoadingBlock label="Checking approved tools..." /> : null}
          {capabilities !== null ? (
            <>
              <label className="agent-tool-option">
                <input
                  type="checkbox"
                  aria-label="Code Interpreter"
                  checked={draftAlreadyHasCodeInterpreter || codeInterpreter}
                  disabled={!codeInterpreterAvailable || draftAlreadyHasCodeInterpreter}
                  onChange={(event) => setCodeInterpreter(event.target.checked)}
                />
                <span><Code2 size={15} /><strong>Code Interpreter</strong><small>{codeInterpreterAvailable ? "Approved and ready to attach." : codeInterpreterOperation?.reason ?? "Not approved for this project."}</small></span>
              </label>
              {configuredToolNames.length > 0 ? (
                <p className="agent-tool-picker-note"><Database size={15} /> Other discovered tools require an approved instance and dedicated configuration before they can be attached: {configuredToolNames.join(", ")}.</p>
              ) : null}
            </>
          ) : null}
        </fieldset>
        <div className="agent-create-actions">
          <button type="submit" className="primary-button" disabled={submitting || !context || !model || !displayName.trim() || !instructions.trim() || Boolean(agentIdError)}>
            {submitting ? "Saving..." : draft ? "Save changes" : "Save prompt draft"}
          </button>
        </div>
          </form>
        </>
      ) : null}
    </div>
  );
}