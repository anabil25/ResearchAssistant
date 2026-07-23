"use client";

import {
  AlertTriangle,
  Ban,
  CircleDashed,
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

const ICONS: Record<AsyncErrorKind, ReactNode> = {
  unauthorized: <Lock size={16} />,
  unavailable: <Ban size={16} />,
  needs_connection: <PlugZap size={16} />,
  needs_approval: <ShieldAlert size={16} />,
  degraded: <WifiOff size={16} />,
  error: <AlertTriangle size={16} />,
};

const TITLES: Record<AsyncErrorKind, string> = {
  unauthorized: "Not authorized",
  unavailable: "Not available yet",
  needs_connection: "Needs a connection",
  needs_approval: "Needs approval",
  degraded: "Degraded",
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
  return (
    <div className="async-state-banner" data-tone={kind} role="status">
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
