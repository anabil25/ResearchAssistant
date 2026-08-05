"use client";

import {
  BookOpen,
  Bot,
  CircleDashed,
  ClipboardCheck,
  FileText,
  FlaskConical,
  Globe2,
  Paperclip,
  SendHorizontal,
  ShieldCheck,
  Sparkles,
  Users,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import {
  getChatThread,
  listChatAgents,
  openChatThread,
  sendChatMessage,
  uploadChatFile,
} from "@/lib/api";
import { classifyAsyncError } from "@/components/async-state";
import {
  agentSurface,
  isChatCapability as surfaceIsChat,
} from "@/lib/agent-surfaces";
import type {
  CapabilityId,
  ChatAgentChoice,
  ChatAttachment,
  ChatMessage,
  ChatThread,
} from "@/lib/types";

const ResearchMarkdown = lazy(async () => ({
  default: (await import("@/components/research-markdown")).ResearchMarkdown,
}));

/**
 * The four capabilities that render this chat surface. Institutional Q&A keeps
 * its version-and-abstain workflow and Workflow Automation keeps its DAG
 * editor, so neither appears here — the backend rejects them too.
 */
export const CHAT_CAPABILITIES = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "screening",
] as const;

export type ChatCapabilityId = (typeof CHAT_CAPABILITIES)[number];

/** Icons cannot cross the wire, so the server sends a name and this resolves it. */
const ICONS: Record<string, LucideIcon> = {
  BookOpen,
  ClipboardCheck,
  FileText,
  Users,
  FlaskConical,
};

export function isChatCapability(
  capability: CapabilityId,
): capability is ChatCapabilityId {
  return surfaceIsChat(capability);
}

interface CapabilityCopy {
  icon: LucideIcon;
  eyebrow: string;
  title: string;
  description: string;
  suggestions: string[];
}

function capabilityCopy(capability: CapabilityId): CapabilityCopy {
  const surface = agentSurface(capability);
  return {
    icon: ICONS[surface?.icon ?? ""] ?? BookOpen,
    eyebrow: surface?.eyebrow ?? "Agent",
    title: surface?.chat_title ?? "Agent",
    description: surface?.chat_description ?? "",
    suggestions: surface?.suggestions ?? [],
  };
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

interface PendingAttachment {
  key: string;
  name: string;
  size: number;
  state: "uploading" | "ready" | "failed";
  attachment?: ChatAttachment;
  error?: string;
}

function attachmentKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

export function AgentChat({
  capability,
  projectId,
}: {
  capability: ChatCapabilityId;
  projectId?: string | null;
}) {
  const copy = capabilityCopy(capability);
  const [boundAgent, setBoundAgent] = useState<ChatAgentChoice | null>(null);
  const [agentLoading, setAgentLoading] = useState(true);
  const [thread, setThread] = useState<ChatThread | null>(null);
  const [threadLoading, setThreadLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const threadRef = useRef<ChatThread | null>(null);

  const ensureThread = useCallback(async (): Promise<ChatThread> => {
      if (threadRef.current) return threadRef.current;
      if (!boundAgent) {
        throw new Error(`No deployed agent is bound to ${copy.title}.`);
      }
      setThreadLoading(true);
      setError(null);
      try {
        const opened = await openChatThread(
          capability,
          boundAgent.name,
          projectId ?? undefined,
        );
        threadRef.current = opened;
        setThread(opened);
        return opened;
      } finally {
        setThreadLoading(false);
      }
    },
    [boundAgent, capability, copy.title, projectId],
  );

  useEffect(() => {
    let cancelled = false;
    threadRef.current = null;
    setThread(null);
    setPending([]);
    setBoundAgent(null);
    setAgentLoading(true);
    setError(null);
    void listChatAgents(capability, projectId ?? undefined)
      .then((choices) => {
        if (cancelled) return;
        if (choices.length !== 1) {
          throw new Error(
            `${copy.title} requires exactly one deployed agent; the server returned ${choices.length}.`,
          );
        }
        setBoundAgent(choices[0]);
        setAgentLoading(false);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setAgentLoading(false);
        setError(classifyAsyncError(caught).message);
      });
    return () => {
      cancelled = true;
    };
  }, [capability, copy.title, projectId]);

  useEffect(() => {
    // jsdom and older engines omit scrollIntoView; autoscroll is cosmetic.
    const end = transcriptEndRef.current;
    if (typeof end?.scrollIntoView === "function") {
      end.scrollIntoView({ block: "end" });
    }
  }, [thread?.messages.length, sending]);

  const busy = agentLoading || sending || threadLoading;

  const attachFiles = useCallback(
    async (files: File[]) => {
      if (!boundAgent || files.length === 0) return;
      let activeThread: ChatThread;
      try {
        activeThread = await ensureThread();
      } catch (caught) {
        setError(classifyAsyncError(caught).message);
        return;
      }
      const queued = files.map<PendingAttachment>((file) => ({
        key: attachmentKey(file),
        name: file.name,
        size: file.size,
        state: "uploading",
      }));
      setPending((current) => [
        ...current.filter(
          (item) => !queued.some((entry) => entry.key === item.key),
        ),
        ...queued,
      ]);
      await Promise.all(
        files.map(async (file) => {
          const key = attachmentKey(file);
          try {
            const uploaded = await uploadChatFile(
              activeThread.id,
              file,
              projectId ?? undefined,
            );
            setPending((current) =>
              current.map((item) =>
                item.key === key
                  ? { ...item, state: "ready", attachment: uploaded }
                  : item,
              ),
            );
          } catch (caught) {
            setPending((current) =>
              current.map((item) =>
                item.key === key
                  ? {
                      ...item,
                      state: "failed",
                      error: classifyAsyncError(caught).message,
                    }
                  : item,
              ),
            );
          }
        }),
      );
    },
    [boundAgent, ensureThread, projectId],
  );

  const onFileInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    void attachFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    void attachFiles(Array.from(event.dataTransfer.files));
  };

  const send = async () => {
    const text = draft.trim();
    if (!boundAgent || !text || sending) return;
    setSending(true);
    setError(null);
    setDraft("");
    let activeThread: ChatThread;
    try {
      activeThread = await ensureThread();
    } catch (caught) {
      setError(classifyAsyncError(caught).message);
      setDraft(text);
      setSending(false);
      return;
    }
    const sentAttachments = pending
      .filter((item) => item.state === "ready" && item.attachment)
      .map((item) => item.attachment as ChatAttachment);
    const clientMessageId = crypto.randomUUID();
    const optimistic: ChatMessage = {
      id: clientMessageId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
      agent_name: null,
      attachments: sentAttachments,
    };
    setThread({ ...activeThread, messages: [...activeThread.messages, optimistic] });
    threadRef.current = { ...activeThread, messages: [...activeThread.messages, optimistic] };
    setPending([]);
    try {
      await sendChatMessage(
        activeThread.id,
        text,
        clientMessageId,
        projectId ?? undefined,
      );
      const refreshed = await getChatThread(activeThread.id, projectId ?? undefined);
      threadRef.current = refreshed;
      setThread(refreshed);
    } catch (caught) {
      setError(classifyAsyncError(caught).message);
      setThread((current) =>
        current
          ? { ...current, messages: current.messages.filter((m) => m.id !== optimistic.id) }
          : current,
      );
      threadRef.current = activeThread;
      setDraft(text);
    } finally {
      setSending(false);
    }
  };

  const onComposerKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  const applySuggestion = (suggestion: string) => {
    setDraft(suggestion);
    composerRef.current?.focus();
  };

  const messages = thread?.messages ?? [];
  const Icon = copy.icon;
  const uploading = pending.some((item) => item.state === "uploading");

  return (
    <div
      className={`agent-chat${dragging ? " agent-chat-dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <header className="agent-chat-header">
        <span className="agent-chat-icon" aria-hidden="true">
          <Icon size={20} />
        </span>
        <div className="agent-chat-heading">
          <span className="eyebrow">{copy.eyebrow}</span>
          <h1>{copy.title}</h1>
          <p>{copy.description}</p>
        </div>
        {boundAgent ? (
          <div
            className="agent-chat-binding"
            aria-label={`Deployed agent ${boundAgent.name}`}
          >
            <Bot size={16} aria-hidden="true" />
            <span>
              <small>Deployed agent</small>
              <strong>{boundAgent.name}</strong>
            </span>
          </div>
        ) : null}
      </header>

      {boundAgent ? (
        <p className="agent-chat-agent-note">
          {boundAgent.online ? <Globe2 size={15} /> : <ShieldCheck size={15} />}
          <span>{boundAgent.description}</span>
        </p>
      ) : null}

      {error ? (
        <div className="error-banner" role="alert">
          <ShieldCheck size={17} />
          <span>{error}</span>
        </div>
      ) : null}

      <div
        className="agent-chat-transcript"
        role="log"
        aria-live="polite"
        aria-label={`${copy.title} conversation`}
      >
        {messages.length === 0 && !sending ? (
          <div className="agent-chat-empty">
            <Sparkles size={20} aria-hidden="true" />
            <p>
              Describe what you need in your own words. The agent already carries
              its instructions, tools, and evidence boundary — you do not need to
              configure a workflow.
            </p>
            <ul>
              {copy.suggestions.map((suggestion) => (
                <li key={suggestion}>
                  <button type="button" onClick={() => applySuggestion(suggestion)}>
                    {suggestion}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {messages.map((message) => (
          <article
            key={message.id}
            className={`agent-chat-message agent-chat-${message.role}`}
          >
            <div className="agent-chat-message-meta">
              <span className="agent-chat-avatar" aria-hidden="true">
                {message.role === "assistant" ? <Bot size={15} /> : null}
              </span>
              <strong>
                {message.role === "assistant"
                  ? (message.agent_name ?? "Agent")
                  : "You"}
              </strong>
              <time dateTime={message.created_at}>
                {formatTimestamp(message.created_at)}
              </time>
            </div>
            {message.role === "assistant" ? (
              <Suspense fallback={<p>Rendering response...</p>}>
                <ResearchMarkdown
                  content={message.content}
                  label={`${message.agent_name ?? "Agent"} response`}
                />
              </Suspense>
            ) : (
              <p className="agent-chat-text">{message.content}</p>
            )}
            {message.attachments.length > 0 ? (
              <ul className="agent-chat-attachments">
                {message.attachments.map((attachment) => (
                  <li key={attachment.path}>
                    <Paperclip size={13} aria-hidden="true" />
                    <span>{attachment.path}</span>
                    <em>{formatBytes(attachment.size_bytes)}</em>
                  </li>
                ))}
              </ul>
            ) : null}
          </article>
        ))}

        {threadLoading ? (
          <p className="agent-chat-pending">
            <CircleDashed className="spin" size={16} aria-hidden="true" />
            <span>Connecting to {boundAgent?.name}...</span>
          </p>
        ) : null}
        {sending ? (
          <p className="agent-chat-pending">
            <CircleDashed className="spin" size={16} aria-hidden="true" />
            <span>{boundAgent?.name} is working...</span>
          </p>
        ) : null}
        <div ref={transcriptEndRef} />
      </div>

      <form
        className="agent-chat-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        {pending.length > 0 ? (
          <ul className="agent-chat-pending-files">
            {pending.map((item) => (
              <li key={item.key} data-state={item.state}>
                <Paperclip size={13} aria-hidden="true" />
                <span>{item.name}</span>
                <em>
                  {item.state === "uploading"
                    ? "Uploading..."
                    : item.state === "failed"
                      ? (item.error ?? "Upload failed")
                      : formatBytes(item.attachment?.size_bytes ?? item.size)}
                </em>
                <button
                  type="button"
                  aria-label={`Remove ${item.name}`}
                  onClick={() =>
                    setPending((current) =>
                      current.filter((entry) => entry.key !== item.key),
                    )
                  }
                >
                  <X size={13} />
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="agent-chat-composer-row">
          <button
            type="button"
            className="agent-chat-attach"
            aria-label="Attach files"
            disabled={!boundAgent || busy}
            onClick={() => fileInputRef.current?.click()}
          >
            <Paperclip size={17} />
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            accept=".pdf,.txt,.md,.csv,.json"
            onChange={onFileInputChange}
            data-testid="agent-chat-file-input"
          />
          <textarea
            ref={composerRef}
            value={draft}
            rows={1}
            placeholder="Ask the agent anything, or drop a file here"
            aria-label="Message"
            disabled={!boundAgent || busy}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onComposerKeyDown}
          />
          <button
            type="submit"
            className="primary-button agent-chat-send"
            aria-label="Send"
            disabled={!boundAgent || busy || uploading || draft.trim().length === 0}
          >
            {sending ? (
              <CircleDashed className="spin" size={16} />
            ) : (
              <SendHorizontal size={16} />
            )}
            <span>Send</span>
          </button>
        </div>
        <p className="agent-chat-boundary">
          Agent replies are supplemental analysis. They cannot grant permissions,
          approve actions, or promote unresolved claims to verified evidence.
        </p>
      </form>
    </div>
  );
}
