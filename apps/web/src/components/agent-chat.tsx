"use client";

import {
  BookOpen,
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  ChevronUp,
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
  Wrench,
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
  streamChatMessage,
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
  ChatActivity,
  ChatAttachment,
  ChatMessage,
  ChatStreamEvent,
  ChatThread,
} from "@/lib/types";

const ResearchMarkdown = lazy(async () => ({
  default: (await import("@/components/research-markdown")).ResearchMarkdown,
}));

/**
 * The capabilities that render this chat surface. Institutional Q&A keeps its
 * version-and-abstain workflow, so it does not appear here.
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

function formatDuration(value: number | null): string | null {
  if (value === null) return null;
  if (value < 1_000) return `${value} ms`;
  return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)} s`;
}

const LONG_RESPONSE_CHARACTERS = 900;

function ActivityStatusIcon({ status }: { status: string }) {
  if (status === "in_progress" || status === "running") {
    return <CircleDashed className="spin" size={12} aria-hidden="true" />;
  }
  if (status === "failed" || status === "incomplete") {
    return <X size={12} aria-hidden="true" />;
  }
  return <Check size={12} aria-hidden="true" />;
}

function AgentActivityPanel({
  message,
  live = false,
}: {
  message: ChatMessage;
  live?: boolean;
}) {
  const activity = message.activity ?? [];
  const toolCount = activity.filter((item) => item.kind === "tool").length;
  const duration = formatDuration(message.duration_ms ?? null);
  const facts = [
    toolCount
      ? `${toolCount} ${toolCount === 1 ? "tool" : "tools"}`
      : live
        ? "Starting"
        : "Direct response",
    message.source_count
      ? `${message.source_count} ${message.source_count === 1 ? "source" : "sources"}`
      : null,
    duration,
  ].filter(Boolean);

  if (!live && activity.length === 0 && message.duration_ms == null) return null;

  return (
    <details
      className="agent-chat-activity"
      data-live={live ? "true" : "false"}
      open={live ? true : undefined}
    >
      <summary>
        <span className="agent-chat-activity-title">
          <BrainCircuit size={15} aria-hidden="true" />
          Activity
        </span>
        <span className="agent-chat-activity-facts">{facts.join(" · ")}</span>
        <ChevronDown className="agent-chat-activity-chevron" size={15} aria-hidden="true" />
      </summary>
      <div className="agent-chat-activity-body">
        {activity.length > 0 ? (
          <ol>
            {activity.map((item, index) => (
              <li key={`${item.kind}-${item.label}-${index}`}>
                <span className="agent-chat-activity-icon" data-kind={item.kind}>
                  {item.kind === "tool" ? (
                    <Wrench size={14} aria-hidden="true" />
                  ) : (
                    <BrainCircuit size={14} aria-hidden="true" />
                  )}
                </span>
                <span className="agent-chat-activity-copy">
                  <strong>{item.label}</strong>
                  {item.detail ? <small>{item.detail}</small> : null}
                </span>
                <span className="agent-chat-activity-status" data-status={item.status}>
                  <ActivityStatusIcon status={item.status} />
                  {item.status.replaceAll("_", " ")}
                </span>
              </li>
            ))}
          </ol>
        ) : (
          <p>
            {live
              ? "Waiting for the first observable action..."
              : "No external tools were used for this response."}
          </p>
        )}
        <p className="agent-chat-activity-note">
          Shows observable actions and concise approach summaries. Private reasoning
          and tool payloads remain hidden.
        </p>
      </div>
    </details>
  );
}

function AgentAnswer({
  message,
  live = false,
}: {
  message: ChatMessage;
  live?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isLong = !live && message.content.length > LONG_RESPONSE_CHARACTERS;

  return (
    <>
      <div
        className="agent-chat-answer"
        data-collapsed={isLong && !expanded ? "true" : "false"}
      >
        {message.content ? (
          <Suspense fallback={<p>Rendering response...</p>}>
            <ResearchMarkdown
              content={message.content}
              label={`${message.agent_name ?? "Agent"} response`}
            />
          </Suspense>
        ) : live ? (
          <p className="agent-chat-live-answer">
            <CircleDashed className="spin" size={14} aria-hidden="true" />
            Preparing the response...
          </p>
        ) : null}
      </div>
      {isLong ? (
        <button
          type="button"
          className="agent-chat-answer-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          {expanded ? "Show less" : "Show full response"}
        </button>
      ) : null}
      <AgentActivityPanel message={message} live={live} />
    </>
  );
}

interface LiveChatActivity extends ChatActivity {
  streamId: string;
}

interface LiveChatMessage extends Omit<ChatMessage, "activity"> {
  activity: LiveChatActivity[];
}

function ChatMessageEntry({
  message,
  live = false,
}: {
  message: ChatMessage;
  live?: boolean;
}) {
  return (
    <article
      className={`agent-chat-message agent-chat-${message.role}`}
      data-live={live ? "true" : "false"}
    >
      <div className="agent-chat-message-meta">
        <span className="agent-chat-avatar" aria-hidden="true">
          {message.role === "assistant" ? <Bot size={15} /> : null}
        </span>
        <strong>
          {message.role === "assistant" ? (message.agent_name ?? "Agent") : "You"}
        </strong>
        <time dateTime={message.created_at}>{formatTimestamp(message.created_at)}</time>
        {live ? <em className="agent-chat-live-label">Live</em> : null}
      </div>
      {message.role === "assistant" ? (
        <AgentAnswer message={message} live={live} />
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
  );
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
  const [streamingMessage, setStreamingMessage] = useState<LiveChatMessage | null>(null);
  const [streamStartedAt, setStreamStartedAt] = useState<number | null>(null);
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
  }, [
    thread?.messages.length,
    sending,
    streamingMessage?.content.length,
    streamingMessage?.activity.length,
  ]);

  useEffect(() => {
    if (streamStartedAt == null) return;
    const updateDuration = () => {
      setStreamingMessage((current) =>
        current
          ? { ...current, duration_ms: Math.max(0, Date.now() - streamStartedAt) }
          : current,
      );
    };
    updateDuration();
    const interval = window.setInterval(updateDuration, 1_000);
    return () => window.clearInterval(interval);
  }, [streamStartedAt]);

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
      activity: [],
      duration_ms: null,
      source_count: 0,
    };
    setThread({ ...activeThread, messages: [...activeThread.messages, optimistic] });
    threadRef.current = { ...activeThread, messages: [...activeThread.messages, optimistic] };
    setPending([]);
    const liveStartedAt = Date.now();
    setStreamStartedAt(liveStartedAt);
    setStreamingMessage({
      id: `reply-${clientMessageId}`,
      role: "assistant",
      content: "",
      created_at: new Date(liveStartedAt).toISOString(),
      agent_name: boundAgent.name,
      attachments: [],
      activity: [],
      duration_ms: 0,
      source_count: 0,
    });
    try {
      const onStreamEvent = (event: ChatStreamEvent) => {
        if (event.type === "started") {
          setStreamingMessage((current) =>
            current
              ? {
                  ...current,
                  id: event.message_id,
                  agent_name: event.agent_name,
                  created_at: event.created_at,
                }
              : current,
          );
          return;
        }
        if (event.type === "activity") {
          setStreamingMessage((current) => {
            if (!current) return current;
            const next: LiveChatActivity = {
              ...event.activity,
              streamId: event.activity_id,
            };
            const index = current.activity.findIndex(
              (item) => item.streamId === event.activity_id,
            );
            const activity = [...current.activity];
            if (index >= 0) activity[index] = next;
            else activity.push(next);
            return { ...current, activity };
          });
          return;
        }
        if (event.type === "text_delta") {
          setStreamingMessage((current) =>
            current ? { ...current, content: `${current.content}${event.delta}` } : current,
          );
          return;
        }
        if (event.type === "completed") {
          setStreamingMessage({
            ...event.message,
            activity: (event.message.activity ?? []).map((item, index) => ({
              ...item,
              streamId: `final-${index}`,
            })),
          });
        }
      };
      await streamChatMessage(
        activeThread.id,
        text,
        clientMessageId,
        onStreamEvent,
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
      setStreamingMessage(null);
      setStreamStartedAt(null);
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
          <ChatMessageEntry key={message.id} message={message} />
        ))}

        {streamingMessage ? (
          <ChatMessageEntry message={streamingMessage} live />
        ) : null}

        {threadLoading ? (
          <p className="agent-chat-pending">
            <CircleDashed className="spin" size={16} aria-hidden="true" />
            <span>Connecting to {boundAgent?.name}...</span>
          </p>
        ) : null}
        {sending && !streamingMessage ? (
          <div className="agent-chat-pending">
            <CircleDashed className="spin" size={16} aria-hidden="true" />
            <span>
              <strong>{boundAgent?.name} is working...</strong>
              <small>Tools, sources, and timing will appear in Activity.</small>
            </span>
          </div>
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
