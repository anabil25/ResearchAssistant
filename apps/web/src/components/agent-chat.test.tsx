import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";

import {
  AgentChat,
  CHAT_CAPABILITIES,
  isChatCapability,
} from "@/components/agent-chat";
import {
  ApiError,
  getChatThread,
  listChatAgents,
  openChatThread,
  sendChatMessage,
  uploadChatFile,
} from "@/lib/api";
import type { ChatAgentChoice, ChatThread } from "@/lib/types";

jest.mock("@/lib/api", () => ({
  // `classifyAsyncError` narrows on `instanceof ApiError`; keeping the real
  // class means status-specific copy is exercised instead of throwing.
  ApiError: jest.requireActual<typeof import("@/lib/api")>("@/lib/api").ApiError,
  listChatAgents: jest.fn(),
  openChatThread: jest.fn(),
  getChatThread: jest.fn(),
  sendChatMessage: jest.fn(),
  uploadChatFile: jest.fn(),
}));

jest.mock("@/components/research-markdown", () => ({
  ResearchMarkdown: ({ content, label }: { content: string; label: string }) => (
    <div data-testid="research-markdown" aria-label={label}>
      {content}
    </div>
  ),
}));

function setupUser() {
  return userEvent.setup({ delay: null });
}

/**
 * The file input is `hidden` and gated on `accept`, so `userEvent.upload`
 * either skips it or filters the file out. Dispatching `change` directly is
 * what the paperclip button ultimately triggers anyway.
 */
function dropFiles(...files: File[]) {
  const input = screen.getByTestId("agent-chat-file-input");
  fireEvent.change(input, { target: { files } });
}

const AGENTS: ChatAgentChoice[] = [
  {
    name: "literature-agent",
    label: "Literature agent",
    description: "Grounded synthesis over stored evidence.",
    online: false,
  },
  {
    name: "literature-online-agent",
    label: "Literature agent (public research)",
    description: "Allowlisted public metadata sources only.",
    online: true,
  },
];

function emptyThread(overrides: Partial<ChatThread> = {}): ChatThread {
  return {
    id: "thread-1",
    capability: "literature",
    agent_name: "literature-agent",
    created_at: "2026-07-30T09:00:00Z",
    updated_at: "2026-07-30T09:00:00Z",
    messages: [],
    attachments: [],
    ...overrides,
  };
}

/** Renders and waits until the thread is open and the composer is usable. */
async function renderReady(projectId = "demo-project") {
  const view = render(
    <AgentChat capability="literature" projectId={projectId} />,
  );
  const composer = await screen.findByRole("textbox", { name: "Message" });
  await waitFor(() => expect(composer).toBeEnabled());
  return { ...view, composer: composer as HTMLTextAreaElement };
}

describe("AgentChat", () => {
  beforeEach(() => {
    jest.mocked(listChatAgents).mockReset().mockResolvedValue(AGENTS);
    jest.mocked(openChatThread).mockReset().mockResolvedValue(emptyThread());
    jest.mocked(getChatThread).mockReset();
    jest.mocked(sendChatMessage).mockReset();
    jest.mocked(uploadChatFile).mockReset();
  });

  it("treats exactly the four evidence capabilities as chat surfaces", () => {
    expect([...CHAT_CAPABILITIES]).toEqual([
      "literature",
      "grant",
      "matching",
      "dataset",
    ]);
    expect(isChatCapability("dataset")).toBe(true);
    // Institutional Q&A and orchestration keep their own workflows.
    expect(isChatCapability("institutional_qa")).toBe(false);
    expect(isChatCapability("orchestration")).toBe(false);
  });

  it("opens a thread for the first agent and scopes every call to the project", async () => {
    await renderReady();

    expect(listChatAgents).toHaveBeenCalledWith("literature", "demo-project");
    expect(openChatThread).toHaveBeenCalledWith(
      "literature",
      "literature-agent",
      "demo-project",
    );
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveValue(
      "literature-agent",
    );
    expect(
      screen.getByText(/Grounded synthesis over stored evidence/),
    ).toBeInTheDocument();
  });

  it("renders the four capability headings without any workflow form", async () => {
    for (const capability of CHAT_CAPABILITIES) {
      jest
        .mocked(openChatThread)
        .mockResolvedValue(emptyThread({ capability }));
      const { unmount } = render(<AgentChat capability={capability} />);
      expect(
        await screen.findByRole("textbox", { name: "Message" }),
      ).toBeInTheDocument();
      expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
      // The old studios drove runs from stage forms; the chat has none.
      expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
      unmount();
    }
  });

  it("prefills the composer from a suggestion without sending it", async () => {
    const user = setupUser();
    const { composer } = await renderReady();

    const suggestion = screen.getAllByRole("button", { name: /^Compare / })[0];
    await user.click(suggestion);

    expect(composer.value).toBe(suggestion.textContent);
    expect(sendChatMessage).not.toHaveBeenCalled();
  });

  it("sends a turn, shows it optimistically, and adopts the server transcript", async () => {
    const user = setupUser();
    const answered = emptyThread({
      messages: [
        {
          id: "m1",
          role: "user",
          content: "Summarise the attached trials",
          created_at: "2026-07-30T09:01:00Z",
          agent_name: null,
          attachments: [],
        },
        {
          id: "m2",
          role: "assistant",
          content: "Three trials, two agree on the primary endpoint.",
          created_at: "2026-07-30T09:01:20Z",
          agent_name: "literature-agent",
          attachments: [],
        },
      ],
    });
    jest.mocked(sendChatMessage).mockResolvedValue(answered.messages[1]);
    jest.mocked(getChatThread).mockResolvedValue(answered);

    const { composer } = await renderReady();
    await user.type(composer, "Summarise the attached trials");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() =>
      expect(sendChatMessage).toHaveBeenCalledWith(
        "thread-1",
        "Summarise the attached trials",
        "demo-project",
      ),
    );
    expect(
      await screen.findByText("Three trials, two agree on the primary endpoint."),
    ).toBeInTheDocument();
    const transcript = within(screen.getByRole("log"));
    expect(
      transcript.getByText("Summarise the attached trials"),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("");
  });

  it("sends on Enter and inserts a newline on Shift+Enter", async () => {
    const user = setupUser();
    jest.mocked(getChatThread).mockResolvedValue(emptyThread());
    jest.mocked(sendChatMessage).mockResolvedValue({
      id: "m2",
      role: "assistant",
      content: "ok",
      created_at: "2026-07-30T09:01:20Z",
      agent_name: "literature-agent",
      attachments: [],
    });

    const { composer } = await renderReady();
    await user.type(composer, "first line{Shift>}{Enter}{/Shift}second line");
    expect(composer.value).toContain("\n");
    expect(sendChatMessage).not.toHaveBeenCalled();

    await user.type(composer, "{Enter}");
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledTimes(1));
  });

  it("never sends an empty or whitespace-only turn", async () => {
    const user = setupUser();
    const { composer } = await renderReady();

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(composer, "   ");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.type(composer, "{Enter}");
    expect(sendChatMessage).not.toHaveBeenCalled();
  });

  it("uploads attachments, lists them, and allows removing one before sending", async () => {
    const user = setupUser();
    jest.mocked(uploadChatFile).mockResolvedValue({
      path: "outcomes.csv",
      size_bytes: 2048,
      content_type: "text/csv",
      uploaded_at: "2026-07-30T09:00:30Z",
    });

    await renderReady();
    const file = new File(["a,b\n1,2\n"], "outcomes.csv", { type: "text/csv" });
    dropFiles(file);

    await waitFor(() =>
      expect(uploadChatFile).toHaveBeenCalledWith(
        "thread-1",
        file,
        "demo-project",
      ),
    );
    expect(await screen.findByText("outcomes.csv")).toBeInTheDocument();
    expect(await screen.findByText("2 KB")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Remove outcomes.csv" }),
    );
    expect(screen.queryByText("outcomes.csv")).not.toBeInTheDocument();
  });

  it("keeps a failed upload visible with its reason instead of silently dropping it", async () => {
    jest
      .mocked(uploadChatFile)
      .mockRejectedValue(
        new ApiError("Supported uploads are PDF, text, Markdown, CSV, and JSON.", 415),
      );

    await renderReady();
    dropFiles(new File(["x"], "notes.exe", { type: "application/octet-stream" }));

    expect(await screen.findByText("notes.exe")).toBeInTheDocument();
    expect(
      await screen.findByText(/Supported uploads are PDF/),
    ).toBeInTheDocument();
  });

  it("rolls the failed turn back and restores the draft so it can be retried", async () => {
    const user = setupUser();
    jest
      .mocked(sendChatMessage)
      .mockRejectedValue(new ApiError("The agent is still starting.", 503));

    const { composer } = await renderReady();
    await user.type(composer, "Profile the dataset");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The agent is still starting.",
    );
    await waitFor(() => expect(composer).toHaveValue("Profile the dataset"));
    expect(
      within(screen.getByRole("log")).queryByText("Profile the dataset"),
    ).not.toBeInTheDocument();
  });

  it("starts a fresh thread when the agent is switched", async () => {
    const user = setupUser();
    await renderReady();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Agent" }),
      "literature-online-agent",
    );

    await waitFor(() =>
      expect(openChatThread).toHaveBeenLastCalledWith(
        "literature",
        "literature-online-agent",
        "demo-project",
      ),
    );
    expect(
      await screen.findByText(/Allowlisted public metadata sources only/),
    ).toBeInTheDocument();
  });

  it("ignores a stale thread response that lands after a newer agent was chosen", async () => {
    const user = setupUser();
    let releaseStale: (thread: ChatThread) => void = () => undefined;
    const stale = new Promise<ChatThread>((resolve) => {
      releaseStale = resolve;
    });
    jest
      .mocked(openChatThread)
      .mockResolvedValueOnce(emptyThread())
      .mockReturnValueOnce(stale)
      .mockResolvedValueOnce(emptyThread({ id: "thread-3" }));

    await renderReady();
    const picker = screen.getByRole("combobox", { name: "Agent" });
    await user.selectOptions(picker, "literature-online-agent");
    await user.selectOptions(picker, "literature-agent");
    await waitFor(() => expect(openChatThread).toHaveBeenCalledTimes(3));

    releaseStale(emptyThread({ id: "thread-2" }));
    jest.mocked(getChatThread).mockResolvedValue(emptyThread({ id: "thread-3" }));
    jest.mocked(sendChatMessage).mockResolvedValue({
      id: "m2",
      role: "assistant",
      content: "ok",
      created_at: "2026-07-30T09:02:00Z",
      agent_name: "literature-agent",
      attachments: [],
    });

    const composer = screen.getByRole("textbox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "hello");
    await user.click(screen.getByRole("button", { name: "Send" }));

    // The abandoned thread-2 must never become the thread turns are sent to.
    await waitFor(() =>
      expect(sendChatMessage).toHaveBeenCalledWith(
        "thread-3",
        "hello",
        "demo-project",
      ),
    );
  });

  it("surfaces an unreachable agent catalog and leaves the composer disabled", async () => {
    jest
      .mocked(listChatAgents)
      .mockRejectedValue(new ApiError("Agent chat is unavailable.", 503));

    render(<AgentChat capability="grant" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Agent chat is unavailable.",
    );
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Attach files" })).toBeDisabled();
  });

  it("states the agent boundary next to the composer", async () => {
    await renderReady();
    expect(
      screen.getByText(/cannot grant permissions, approve actions/i),
    ).toBeInTheDocument();
  });

  it("has no automated accessibility violations", async () => {
    const { container } = await renderReady();
    expect(await axe(container)).toHaveNoViolations();
  });
});
