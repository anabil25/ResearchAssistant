"use client";

import {
  CheckCircle2,
  CircleDashed,
  Globe2,
  Settings,
  X,
} from "lucide-react";
import { useState } from "react";

import { ConnectionsView } from "@/components/connections-view";
import { updateSettings, type WorkspaceData } from "@/lib/api";
import type { ProjectSettings } from "@/lib/types";

interface ProjectSettingsViewProps {
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
}

const SETTINGS_SECTIONS = ["General", "Connections"] as const;
type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export function ProjectSettingsView({
  data,
  onRefresh,
}: ProjectSettingsViewProps) {
  const [section, setSection] = useState<SettingsSection>("General");
  const [draft, setDraft] = useState<ProjectSettings | null>(
    data?.settings ?? null,
  );
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{
    message: string;
    tone: "success" | "error";
  } | null>(null);

  if (!data) {
    return (
      <div className="operational-page settings-page">
        <header className="operational-header">
          <div>
            <span className="eyebrow">Project control plane</span>
            <h1>Project Settings</h1>
            <p>Loading project settings and deployment connections.</p>
          </div>
        </header>
        <div className="loading-block">Loading project settings...</div>
      </div>
    );
  }

  return (
    <div className="operational-page settings-page">
      <header className="operational-header">
        <div>
          <span className="eyebrow">Project control plane</span>
          <h1>Project Settings</h1>
          <p>
            Manage project defaults and configure research connections without
            exposing stored secret values.
          </p>
        </div>
      </header>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SETTINGS_SECTIONS.map((item) => (
            <button
              type="button"
              aria-current={section === item ? "page" : undefined}
              data-active={section === item}
              key={item}
              onClick={() => {
                setSection(item);
                setStatus(null);
              }}
            >
              {item === "General" ? (
                <Settings size={16} />
              ) : (
                <Globe2 size={16} />
              )}
              {item}
              {item === "Connections" ? (
                <em>{data?.connectors.length ?? 0}</em>
              ) : null}
            </button>
          ))}
        </nav>

        <main className="settings-content">
          {status ? (
            <div className={`save-status ${status.tone}`} role="status">
              {status.tone === "success" ? (
                <CheckCircle2 size={16} />
              ) : status.tone === "error" ? (
                <X size={16} />
              ) : (
                <CircleDashed size={16} />
              )}
              {status.message}
            </div>
          ) : null}

          {section === "General" ? (
            <section className="settings-section">
              <div className="settings-section-heading">
                <div>
                  <h2>Workspace profile</h2>
                  <p>
                    Project identity and behavior-safe defaults for new work.
                  </p>
                </div>
              </div>
              {draft ? (
                <form
                  className="settings-form panel"
                  onSubmit={(event) => {
                    event.preventDefault();
                    setSaving(true);
                    setStatus(null);
                    void updateSettings(draft)
                      .then(async (saved) => {
                        setDraft(saved);
                        await onRefresh();
                        setStatus({
                          message: "Project settings saved.",
                          tone: "success",
                        });
                      })
                      .catch((error: unknown) =>
                        setStatus({
                          message:
                            error instanceof Error
                              ? error.message
                              : "Settings could not be saved.",
                          tone: "error",
                        }),
                      )
                      .finally(() => setSaving(false));
                  }}
                >
                  <label className="field">
                    <span>Project name</span>
                    <input
                      required
                      minLength={3}
                      maxLength={120}
                      value={draft.name}
                      onChange={(event) =>
                        setDraft({ ...draft, name: event.target.value })
                      }
                    />
                  </label>
                  <label className="field">
                    <span>Description</span>
                    <textarea
                      required
                      minLength={3}
                      maxLength={1000}
                      rows={4}
                      value={draft.description}
                      onChange={(event) =>
                        setDraft({ ...draft, description: event.target.value })
                      }
                    />
                  </label>
                  <div className="field-row">
                    <label className="field">
                      <span>Default classification</span>
                      <select
                        value={draft.default_classification}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            default_classification: event.target.value,
                          })
                        }
                      >
                        <option>public</option>
                        <option>internal</option>
                        <option>confidential</option>
                        <option>restricted</option>
                      </select>
                    </label>
                    <label className="field">
                      <span>Retention (days)</span>
                      <input
                        type="number"
                        min={30}
                        max={3650}
                        value={draft.retention_days}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            retention_days: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                  </div>
                  <div className="locked-setting">
                    <Globe2 size={18} />
                    <div>
                      <strong>Online research is opt-in per run</strong>
                      <span>A project default cannot enable public web tools.</span>
                    </div>
                    <span>Off</span>
                  </div>
                  <div className="settings-actions">
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={saving}
                    >
                      {saving ? "Saving..." : "Save project settings"}
                    </button>
                  </div>
                </form>
              ) : (
                <div className="loading-block">Loading project settings...</div>
              )}
            </section>
          ) : null}

          {section === "Connections" ? (
            <ConnectionsView data={data} onRefresh={onRefresh} />
          ) : null}
        </main>
      </div>
    </div>
  );
}
