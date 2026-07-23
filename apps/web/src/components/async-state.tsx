"use client";

import {
  AlertTriangle,
  Ban,
  CircleDashed,
  GitMerge,
  Lock,
  PlugZap,
  ShieldAlert,
  WifiOff,
} from "lucide-react";
import type { ReactNode } from "react";

import { ApiError } from "@/lib/api";

export type AsyncErrorKind =
  | "unauthorized"
  | "unavailable"
  | "needs_connection"
  | "needs_approval"
  | "degraded"
  | "conflict"
  | "error";

export interface ClassifiedAsyncError {
  kind: AsyncErrorKind;
  message: string;
}

/**
 * Turns a thrown value from a real fetch into an honest, specific state
 * instead of a single generic "error" bucket. Used across the Agent
 * Registry, Agent Workspace, and Connections views so proposed-but-not-yet-
 * implemented backend endpoints (404), auth failures (401/403), and
 * governance holds (409/424/428) render distinct, explicit UI.
 */
export function classifyAsyncError(error: unknown): ClassifiedAsyncError {
  const message = error instanceof Error ? error.message : "Request failed.";
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) {
      return { kind: "unauthorized", message };
    }
    if (error.status === 404 || error.status === 501) {
      return {
        kind: "unavailable",
        message:
          "This feature's backend endpoint isn't implemented yet. " +
          message,
      };
    }
    if (error.status === 424 || error.status === 428) {
      return { kind: "needs_connection", message };
    }
    if (error.status === 409) {
      return { kind: "needs_approval", message };
    }
    if (error.status === 502 || error.status === 503 || error.status === 504) {
      return { kind: "degraded", message };
    }
  }
  return { kind: "error", message };
}

/**
 * Builder draft-mutation endpoints (`postBuilderMessage`/
 * `applyBuilderProposal`) always send the client's last-observed
 * `base_etag`, so a 409 from *these specific* endpoints unambiguously means
 * an optimistic-concurrency conflict — the draft changed since this client
 * last read it — never the unrelated governance "needs approval" hold that
 * `classifyAsyncError` maps generic 409s to elsewhere (e.g. deploy gates).
 * There is no structured error-code field on the wire yet to distinguish
 * the two cases generically, so this is an explicit, interim, call-site-
 * scoped override: it must only ever be applied at the two draft-mutation
 * call sites, never treated as a replacement for `classifyAsyncError`
 * generally. Once the real backend returns a structured conflict code this
 * can be simplified to read it directly instead of inferring from status.
 */
export function classifyBuilderMutationError(
  error: unknown,
): ClassifiedAsyncError {
  if (error instanceof ApiError && error.status === 409) {
    return {
      kind: "conflict",
      message:
        "This draft changed since you last loaded it (etag conflict). " +
        "Reload the draft and reapply your change.",
    };
  }
  return classifyAsyncError(error);
}

const ICONS: Record<AsyncErrorKind, ReactNode> = {
  unauthorized: <Lock size={16} />,
  unavailable: <Ban size={16} />,
  needs_connection: <PlugZap size={16} />,
  needs_approval: <ShieldAlert size={16} />,
  degraded: <WifiOff size={16} />,
  conflict: <GitMerge size={16} />,
  error: <AlertTriangle size={16} />,
};

const TITLES: Record<AsyncErrorKind, string> = {
  unauthorized: "Not authorized",
  unavailable: "Not available yet",
  needs_connection: "Needs a connection",
  needs_approval: "Needs approval",
  degraded: "Degraded",
  conflict: "Conflict",
  error: "Something went wrong",
};

export function AsyncStateBanner({
  kind,
  message,
  onRetry,
}: {
  kind: AsyncErrorKind;
  message: string;
  onRetry?: () => void;
}) {
  const role = kind === "error" || kind === "conflict" ? "alert" : "status";
  return (
    <div className="async-state-banner" data-tone={kind} role={role}>
      <span className="async-state-icon">{ICONS[kind]}</span>
      <div>
        <strong>{TITLES[kind]}</strong>
        <p>{message}</p>
      </div>
      {onRetry ? (
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="loading-block" role="status">
      <CircleDashed size={16} className="spin" />
      {label}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-block">
      <strong>{title}</strong>
      <p>{description}</p>
      {action}
    </div>
  );
}
