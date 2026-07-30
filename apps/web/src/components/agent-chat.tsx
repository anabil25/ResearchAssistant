"use client";

import {
  BookOpen,
  Bot,
  CircleDashed,
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
  useMemo,
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
] as const;

export type ChatCapabilityId = (typeof CHAT_CAPABILITIES)[number];

export function isChatCapability(
  capability: CapabilityId,
): capability is ChatCapabilityId {
  return (CHAT_CAPABILITIES as readonly string[]).includes(capability);
}

interface CapabilityCopy {
  icon: LucideIcon;
  eyebrow: string;
  title: string;
  description: string;
  suggestions: string[];
}

const CAPABILITY_COPY: Record<ChatCapabilityId, CapabilityCopy> = {
  literature: {
    icon: BookOpen,
    eyebrow: "Evidence review",
    title: "Literature Studio",
    description:
      "Ask for a synthesis, a screening decision, or an extraction. Attach the papers you want it to work from.",
    suggestions: [
      "Compare the methods used across the papers I attached and flag where they disagree.",
      "Screen these abstracts against an inclusion criterion of randomised trials since 2020.",
      "Build an extraction matrix of population, method, outcome, and limitation.",
    ],
  },
  grant: {
    icon: FileText,
    eyebrow: "Application lifecycle",
    title: "Grant Studio",
    description:
      "Attach the funding notice and your project facts, then ask for a requirement matrix, a draft, or a red-team review.",
    suggestions: [
      "Turn the attached notice into a requirement matrix with owners and evidence gaps.",
      "Draft the specific aims section from the attached project facts.",
      "Red-team this draft against the sponsor's review criteria.",
    ],
  },
  matching: {
    icon: Users,
    eyebrow: "Discovery",
    title: "Matching Explorer",
    description:
      "Describe the eligibility bar and what you need. Attach a roster or facility list to search within it.",
    suggestions: [
      "Shortlist investigators with wet-lab capacity and prior NIH funding.",
      "Resolve duplicate entries in the attached roster before ranking.",
      "Explain which stored factors drove the top three matches.",
    ],
  },
  dataset: {
    icon: FlaskConical,
    eyebrow: "Data analysis",
    title: "Dataset Lab",
    description:
      "Attach a CSV or notebook output and ask what you want computed. Compute stays inside the approved sandbox.",
    suggestions: [
      "Profile the attached CSV: schema, missingness, and obvious quality problems.",
      "Propose an analysis plan for the outcome column and say what it cannot support.",
      "Compute descriptive statistics per group and show the code you ran.",
    ],
  },
};

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
  const copy = CAPABILITY_COPY[capability];
  const [agents, setAgents] = useState<ChatAgentChoice[]>([]);
  const [agentName, setAgentName] = useState<string | null>(null);
  const [thread, setThread] = useState<ChatThread | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  // Switching agents starts a fresh thread while an older thread's request may
  // still be in flight. Without a monotonic guard, that stale response would
  // land afterwards and replace the new thread with the abandoned one.
  const threadSequenceRef = useRef(0);

  const startThread = useCallback(
    async (nextAgent: string) => {
      const requestId = (threadSequenceRef.current += 1);
      setError(null);
      setThread(null);
      setPending([]);
      try {
        const opened = await openChatThread(
          capability,
          nextAgent,
          projectId ?? undefined,
        );
        if (threadSequenceRef.current !== requestId) return;
        setThread(opened);
      } catch (caught) {
        if (threadSequenceRef.current !== requestId) return;
        setError(classifyAsyncError(caught).message);
      }
    },
    [capability, projectId],
  );

  useEffect(() => {
    let cancelled = false;
    void listChatAgents(capability, projectId ?? undefined)
      .then((choices) => {
        if (cancelled) return;
        setAgents(choices);
        const first = choices[0]?.name ?? null;
        setAgentName(first);
        if (first) void startThread(first);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(classifyAsyncError(caught).message);
      });
    return () => {
      cancelled = true;
    };
  }, [capability, projectId, startThread]);

  useEffect(() => {
    // jsdom and older engines omit scrollIntoView; autoscroll is cosmetic.
    const end = transcriptEndRef.current;
    if (typeof end?.scrollIntoView === "function") {
      end.scrollIntoView({ block: "end" });
    }
  }, [thread?.messages.length, sending]);

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.name === agentName) ?? null,
    [agents, agentName],
  );

  const attachFiles = useCallback(
    async (files: File[]) => {
      if (!thread || files.length === 0) return;
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
              thread.id,
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
    [thread, projectId],
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
    if (!thread || !text || sending) return;
    setSending(true);
    setError(null);
    const sentAttachments = pending
      .filter((item) => item.state === "ready" && item.attachment)
      .map((item) => item.attachment as ChatAttachment);
    const optimistic: ChatMessage = {
      id: `local-${Date.now()}`,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
      agent_name: null,
      attachments: sentAttachments,
    };
    setThread({ ...thread, messages: [...thread.messages, optimistic] });
    setDraft("");
    setPending([]);
    try {
      await sendChatMessage(thread.id, text, projectId ?? undefined);
      // The server owns the transcript: refetching keeps the optimistic turn
      // from drifting from what was actually recorded against the conversation.
      setThread(await getChatThread(thread.id, projectId ?? undefined));
    } catch (caught) {
      setError(classifyAsyncError(caught).message);
      setThread((current) =>
        current
          ? {
              ...current,
              messages: current.messages.filter(
                (message) => message.id !== optimistic.id,
              ),
            }
          : current,
      );
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
        <label className="agent-chat-picker">
          <span>Agent</span>
          <select
            value={agentName ?? ""}
            disabled={agents.length === 0 || sending}
            onChange={(event) => {
              setAgentName(event.target.value);
              void startThread(event.target.value);
            }}
          >
            {agents.map((agent) => (
              <option key={agent.name} value={agent.name}>
                {agent.label}
              </option>
            ))}
          </select>
        </label>
      </header>

      {selectedAgent ? (
        <p className="agent-chat-agent-note">
          {selectedAgent.online ? <Globe2 size={15} /> : <ShieldCheck size={15} />}
          <span>
            <strong>{selectedAgent.name}</strong> — {selectedAgent.description}
          </span>
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

        {sending ? (
          <p className="agent-chat-pending">
            <CircleDashed className="spin" size={16} aria-hidden="true" />
            <span>{agentName} is working...</span>
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
            disabled={!thread || sending}
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
            placeholder={
              thread
                ? "Ask the agent anything, or drop a file here"
                : "Connecting to the agent..."
            }
            aria-label="Message"
            disabled={!thread || sending}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onComposerKeyDown}
          />
          <button
            type="submit"
            className="primary-button agent-chat-send"
            disabled={!thread || sending || uploading || draft.trim().length === 0}
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
