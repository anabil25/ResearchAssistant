"use client";

import {
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  CircleDashed,
  FileText,
  Globe2,
  Lock,
  Search,
  Users,
  X,
} from "lucide-react";
import { useState } from "react";

import {
  testConnector,
  updateConnector,
  updateConnectorCredential,
  type WorkspaceData,
} from "@/lib/api";
import type { ConnectorSetting } from "@/lib/types";
import { formatTime } from "@/components/workspace-views";
import { EmptyBlock, LoadingBlock } from "@/components/async-state";
import {
  describeUrlPolicyRejection,
  evaluateExternalUrlPolicy,
} from "@/lib/url-policy";

const CONNECTOR_SPECIALISTS = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "institution",
  "screening",
] as const;

export function connectorStatusInfo(connector: ConnectorSetting): {
  label: string;
  detail: string;
  tone: string;
} {
  if (!connector.enabled) {
    return {
      label: "Disabled",
      detail:
        "This connector is intentionally disabled and will not be used by research runs.",
      tone: "disabled",
    };
  }
  if (connector.test_status === "configuration_required") {
    return {
      label: "Setup required",
      detail:
        "The provider is not down. An administrator must configure the connector gateway URL and managed identity before tests can reach it.",
      tone: "configuration-required",
    };
  }
  if (connector.test_status === "unavailable") {
    return {
      label: "Connection failed",
      detail:
        "The gateway is configured, but the latest bounded provider probe failed. Retry the test or inspect gateway logs before using this source.",
      tone: "unavailable",
    };
  }
  if (connector.test_status === "ready_with_key") {
    return {
      label: "Ready, key recommended",
      detail:
        "The connector is reachable with limited anonymous quota. Add the optional deployment-managed key for more reliable capacity.",
      tone: "warning",
    };
  }
  if (connector.test_status === "ready") {
    return {
      label: "Ready",
      detail:
        "The latest bounded probe succeeded and this connector can serve its assigned specialists.",
      tone: "ready",
    };
  }
  return {
    label: "Not tested",
    detail: "Run a bounded connection test before relying on this source.",
    tone: "untested",
  };
}

interface ConnectionsViewProps {
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
}

export function ConnectionsView({ data, onRefresh }: ConnectionsViewProps) {
  const [connectorQuery, setConnectorQuery] = useState("");
  const [connectorCategory, setConnectorCategory] = useState("All");
  const [busyConnector, setBusyConnector] = useState<string | null>(null);
  const [credentialDraft, setCredentialDraft] = useState("");
  const [credentialBusy, setCredentialBusy] = useState(false);
  const [managedConnectorId, setManagedConnectorId] = useState(
    data?.connectors[0]?.id ?? "",
  );
  const [status, setStatus] = useState<{
    message: string;
    tone: "success" | "warning" | "error";
  } | null>(null);
  const [connectorDrafts, setConnectorDrafts] = useState<
    Record<string, { enabled: boolean; assigned_agents: string[] }>
  >({});

  const managedConnector =
    (data?.connectors ?? []).find(
      (connector) => connector.id === managedConnectorId,
    ) ??
    data?.connectors[0] ??
    null;
  const connectorDraft = managedConnector
    ? (connectorDrafts[managedConnector.id] ?? {
        enabled: managedConnector.enabled,
        assigned_agents: managedConnector.assigned_agents,
      })
    : null;

  const connectorCategories = [
    "All",
    ...new Set((data?.connectors ?? []).map((item) => item.category)),
  ];
  const visibleConnectors = (data?.connectors ?? []).filter(
    (connector) =>
      (connectorCategory === "All" ||
        connector.category === connectorCategory) &&
      `${connector.name} ${connector.description}`
        .toLowerCase()
        .includes(connectorQuery.toLowerCase()),
  );
  const managedConnectorStatus = managedConnector
    ? connectorStatusInfo(managedConnector)
    : null;

  const mutateConnector = (
    connector: ConnectorSetting,
    update: Partial<ConnectorSetting>,
  ) => {
    setBusyConnector(connector.id);
    setStatus(null);
    void updateConnector(
      { ...connector, ...update },
      data?.settings.project_id,
    )
      .then(async () => {
        await onRefresh();
        setConnectorDrafts((current) => {
          const next = { ...current };
          delete next[connector.id];
          return next;
        });
        setStatus({
          message: `${connector.name} configuration saved.`,
          tone: "success",
        });
      })
      .catch((error: unknown) =>
        setStatus({
          message:
            error instanceof Error ? error.message : "Connector update failed.",
          tone: "error",
        }),
      )
      .finally(() => setBusyConnector(null));
  };

  const saveCredential = async (connectorId: string, apiKey: string | null) => {
    setCredentialBusy(true);
    setStatus(null);
    try {
      await updateConnectorCredential(
        connectorId,
        apiKey,
        data?.settings.project_id,
      );
      await onRefresh();
      setCredentialDraft("");
      setStatus({
        message: apiKey
          ? "API key saved to the gateway."
          : "API key cleared; the connector now uses anonymous quota.",
        tone: "success",
      });
    } catch (error: unknown) {
      setStatus({
        message:
          error instanceof Error ? error.message : "Credential update failed.",
        tone: "error",
      });
    } finally {
      setCredentialBusy(false);
    }
  };

  const runConnectorTest = (connector: ConnectorSetting) => {
    setBusyConnector(connector.id);
    setStatus(null);
    void testConnector(connector.id, data?.settings.project_id)
      .then(async (updated) => {
        await onRefresh();
        const updatedStatus = connectorStatusInfo(updated);
        setStatus({
          message: `${updated.name}: ${updatedStatus.label}. ${updatedStatus.detail}`,
          tone:
            updatedStatus.tone === "unavailable"
              ? "error"
              : ["configuration-required", "warning", "untested"].includes(
                    updatedStatus.tone,
                  )
                ? "warning"
                : "success",
        });
      })
      .catch((error: unknown) =>
        setStatus({
          message:
            error instanceof Error ? error.message : "Connector test failed.",
          tone: "error",
        }),
      )
      .finally(() => setBusyConnector(null));
  };

  if (!data) {
    return (
      <div className="connections-page">
        <header className="settings-section-heading">
          <div>
            <h2>Research connections</h2>
            <p>
              Loading deployment connections and project assignments.
            </p>
          </div>
        </header>
        <LoadingBlock label="Loading connections…" />
      </div>
    );
  }

  return (
    <div className="connections-page">
      <header className="settings-section-heading">
        <div>
          <h2>Research connections</h2>
          <p>
            Assign allowlisted public metadata sources to specific agents,
            inspect terms, and run bounded health tests. Enablement and agent
            assignments apply to this project. API keys are deployment-wide
            gateway secrets and affect every project that uses the provider.
          </p>
        </div>
        <span className="subtle-chip">
          {data.summary.connector_ready}/{data.summary.connector_total} ready
        </span>
      </header>

      {status ? (
        <div className={`save-status ${status.tone}`} role="status">
          {status.tone === "success" ? (
            <CheckCircle2 size={16} />
          ) : status.tone === "warning" ? (
            <CircleDashed size={16} />
          ) : (
            <X size={16} />
          )}
          {status.message}
        </div>
      ) : null}

      <section className="settings-section connector-settings">
        <div className="connector-toolbar">
          <label className="search-field">
            <Search size={16} />
            <span className="sr-only">Search connections</span>
            <input
              value={connectorQuery}
              onChange={(event) => setConnectorQuery(event.target.value)}
              placeholder="Search connections"
            />
          </label>
          <div className="filter-pills">
            {connectorCategories.map((category) => (
              <button
                data-active={connectorCategory === category}
                key={category}
                onClick={() => setConnectorCategory(category)}
              >
                {category}
              </button>
            ))}
          </div>
        </div>
        <div
          className="connector-management-widget panel"
          aria-labelledby="connector-manager-title"
        >
          <aside className="connector-catalog">
            <div className="connector-catalog-heading">
              <div>
                <strong>Connection catalog</strong>
                <span>Select a source to inspect and manage.</span>
              </div>
              <span>{visibleConnectors.length}</span>
            </div>
            <div className="connector-grid">
              {visibleConnectors.map((connector) => {
                const connectorStatus = connectorStatusInfo(connector);
                return (
                  <button
                    type="button"
                    className="connector-card"
                    data-selected={managedConnector?.id === connector.id}
                    key={connector.id}
                    onClick={() => setManagedConnectorId(connector.id)}
                  >
                    <span className="connector-logo">
                      {connector.category === "Funding" ? (
                        <FileText size={18} />
                      ) : connector.category === "Identity" ? (
                        <Users size={18} />
                      ) : connector.category === "Datasets" ? (
                        <BarChart3 size={18} />
                      ) : (
                        <Globe2 size={18} />
                      )}
                    </span>
                    <span>
                      <strong>{connector.name}</strong>
                      <small>{connector.category}</small>
                    </span>
                    <span
                      className="connector-state"
                      data-tone={connectorStatus.tone}
                    >
                      <span />
                      {connectorStatus.label}
                    </span>
                  </button>
                );
              })}
            </div>
            {visibleConnectors.length === 0 ? (
              <EmptyBlock
                title="No connections match this filter"
                description="Try a different search term or category, or clear the filter to see every connection."
              />
            ) : null}
          </aside>

          {managedConnector && connectorDraft && managedConnectorStatus ? (
            <form
              className="connector-manager"
              onSubmit={(event) => {
                event.preventDefault();
                mutateConnector(managedConnector, connectorDraft);
              }}
            >
              <div className="connector-manager-heading">
                <div>
                  <span className="eyebrow">Configuration widget</span>
                  <h2 id="connector-manager-title">Connection manager</h2>
                </div>
                <label className="field connector-selector">
                  <span>Connection</span>
                  <select
                    aria-label="Connection to manage"
                    value={managedConnector.id}
                    onChange={(event) =>
                      setManagedConnectorId(event.target.value)
                    }
                  >
                    {data.connectors.map((connector) => (
                      <option value={connector.id} key={connector.id}>
                        {connector.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="managed-connector-title">
                <span className="connector-logo">
                  {managedConnector.category === "Funding" ? (
                    <FileText size={20} />
                  ) : managedConnector.category === "Identity" ? (
                    <Users size={20} />
                  ) : managedConnector.category === "Datasets" ? (
                    <BarChart3 size={20} />
                  ) : (
                    <Globe2 size={20} />
                  )}
                </span>
                <div>
                  <strong>{managedConnector.name}</strong>
                  <span>
                    {managedConnector.category} · {managedConnector.auth_kind}
                  </span>
                </div>
                <span
                  className="connector-health-badge"
                  data-tone={managedConnectorStatus.tone}
                >
                  {managedConnectorStatus.label}
                </span>
              </div>

              <div
                className="connector-diagnostic"
                data-tone={managedConnectorStatus.tone}
              >
                <div>
                  <strong>{managedConnectorStatus.label}</strong>
                  <p>{managedConnectorStatus.detail}</p>
                </div>
                <dl>
                  <div>
                    <dt>Credential</dt>
                    <dd>{managedConnector.secret_status}</dd>
                  </div>
                  <div>
                    <dt>Last tested</dt>
                    <dd>{formatTime(managedConnector.last_tested_at)}</dd>
                  </div>
                </dl>
              </div>

              <fieldset className="connector-credential">
                <legend>Authentication</legend>
                <dl>
                  <div>
                    <dt>Protocol</dt>
                    <dd>{managedConnector.auth_kind}</dd>
                  </div>
                  <div>
                    <dt>Requirement</dt>
                    <dd>
                      {managedConnector.credential_kind === "none"
                        ? "No credential required"
                        : managedConnector.credential_required
                          ? "API key required"
                          : "API key optional (raises quota)"}
                    </dd>
                  </div>
                </dl>
                {managedConnector.credential_kind === "none" ? (
                  <p className="connector-credential-note">
                    This provider serves public metadata anonymously, so there
                    is nothing to configure.
                  </p>
                ) : (
                  <>
                    <label className="field">
                      <span>API key</span>
                      <input
                        type="password"
                        autoComplete="off"
                        placeholder="Enter a new key to replace the stored one"
                        aria-label={`API key for ${managedConnector.name}`}
                        value={credentialDraft}
                        disabled={credentialBusy}
                        onChange={(event) =>
                          setCredentialDraft(event.target.value)
                        }
                      />
                    </label>
                    <p className="connector-credential-note">
                      Stored once as a deployment-wide API gateway secret and
                      never shown again. Replacing or clearing it affects every
                      project that uses this provider; clearing it returns the
                      connector to anonymous quota.
                      {managedConnector.credential_help_url ? (
                        <>
                          {" "}
                          <a
                            href={managedConnector.credential_help_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            Where do I get a key?
                          </a>
                        </>
                      ) : null}
                    </p>
                    <div className="connector-credential-actions">
                      <button
                        type="button"
                        className="secondary"
                        disabled={credentialBusy || !credentialDraft.trim()}
                        onClick={() => {
                          void saveCredential(
                            managedConnector.id,
                            credentialDraft.trim(),
                          );
                        }}
                      >
                        {credentialBusy ? "Saving…" : "Save key"}
                      </button>
                      <button
                        type="button"
                        className="ghost"
                        disabled={credentialBusy}
                        onClick={() => {
                          void saveCredential(managedConnector.id, null);
                        }}
                      >
                        Clear key
                      </button>
                    </div>
                  </>
                )}
              </fieldset>

              <label className="connector-enable-row">
                <span>
                  <strong>Enable connection</strong>
                  <small>
                    {managedConnector.required
                      ? "Required baseline connections cannot be disabled."
                      : "Disabled connections are excluded from research runs."}
                  </small>
                </span>
                <input
                  type="checkbox"
                  aria-label={`Enable ${managedConnector.name}`}
                  checked={connectorDraft.enabled}
                  disabled={
                    busyConnector === managedConnector.id ||
                    managedConnector.required
                  }
                  onChange={(event) =>
                    setConnectorDrafts((current) => ({
                      ...current,
                      [managedConnector.id]: {
                        ...connectorDraft,
                        enabled: event.target.checked,
                      },
                    }))
                  }
                />
              </label>

              <fieldset className="agent-assignments connector-manager-agents">
                <legend>Assigned agents</legend>
                <p>
                  Only selected agents can use this connection for source retrieval.
                </p>
                <div>
                  {CONNECTOR_SPECIALISTS.map((agent) => (
                    <label key={agent}>
                      <input
                        type="checkbox"
                        aria-label={`Assign ${agent} to ${managedConnector.name}`}
                        checked={connectorDraft.assigned_agents.includes(
                          agent,
                        )}
                        disabled={
                          busyConnector === managedConnector.id ||
                          managedConnector.required
                        }
                        onChange={(event) => {
                          const assigned = event.target.checked
                            ? [...connectorDraft.assigned_agents, agent]
                            : connectorDraft.assigned_agents.filter(
                                (item) => item !== agent,
                              );
                          setConnectorDrafts((current) => ({
                            ...current,
                            [managedConnector.id]: {
                              ...connectorDraft,
                              assigned_agents: assigned,
                            },
                          }));
                        }}
                      />
                      <span>{agent}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <div className="connector-manager-details">
                <div>
                  <strong>Capabilities</strong>
                  <span>{managedConnector.capabilities.join(" · ")}</span>
                </div>
                <div>
                  <strong>Data boundary</strong>
                  <span>{managedConnector.data_boundary}</span>
                </div>
              </div>

              <div className="connector-manager-actions">
                {(() => {
                  // `managedConnector` is narrowed non-null in this scope, so
                  // evaluating the policy here (rather than hoisting it above)
                  // guarantees a real decision and avoids an unreachable null
                  // branch.
                  const termsPolicy = evaluateExternalUrlPolicy(
                    managedConnector.terms_url,
                  );
                  return termsPolicy.allowed ? (
                    <a
                      href={termsPolicy.url}
                      target="_blank"
                      rel="noreferrer"
                      data-terms-state="ready"
                    >
                      Provider terms <ArrowUpRight size={13} />
                    </a>
                  ) : (
                    <span
                      className="connector-terms-blocked"
                      role="status"
                      data-terms-state="blocked-url"
                      aria-label={describeUrlPolicyRejection(
                        termsPolicy.reason,
                      )}
                    >
                      <Lock size={13} aria-hidden="true" />
                      {describeUrlPolicyRejection(termsPolicy.reason)}
                    </span>
                  );
                })()}
                <div>
                  <button
                    type="button"
                    disabled={busyConnector === managedConnector.id}
                    onClick={() => runConnectorTest(managedConnector)}
                  >
                    {busyConnector === managedConnector.id
                      ? "Testing…"
                      : "Test connection"}
                  </button>
                  <button
                    className="primary-button"
                    type="submit"
                    disabled={busyConnector === managedConnector.id}
                  >
                    Save configuration
                  </button>
                </div>
              </div>
            </form>
          ) : (
            <EmptyBlock
              title="No connection selected"
              description="Select a connection from the catalog to configure it."
            />
          )}
        </div>
        <article className="web-search-policy panel">
          <span className="connector-logo">
            <Search size={18} />
          </span>
          <div>
            <strong>Foundry Web Search</strong>
            <p>
              Separate from the metadata connections above. Available only to
              literature, grant, and matching runs when the user opts in and
              the context is public.
            </p>
          </div>
          <span className="subtle-chip">Per-run only</span>
        </article>
      </section>
    </div>
  );
}
