import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  AutomationStudio,
  InstitutionalStudio,
  StudioForCapability,
} from "@/components/studio-components";
import { uploadLibraryItem } from "@/lib/api";
import type { WorkspaceData } from "@/lib/api";
import { isBlockingModalOpen } from "@/lib/blocking-modal";

/**
 * `userEvent.setup` with the artificial inter-event delay removed.
 *
 * userEvent v14 defaults to `delay: 0`, which is not "no delay" -- it awaits a
 * real `setTimeout(..., 0)` between *every* dispatched event. An omnibus test
 * here performs 25+ interactions plus multi-character `type()` calls, so it
 * accumulates well over a hundred of those hops. In isolation each costs
 * roughly nothing; under the full `--runInBand` suite, with a large
 * accumulated heap and a busy event loop, each hop costs far more and varies
 * run to run. That is what made this file's slowest tests sit at ~3.0s and
 * ~4.0s against Jest's 5s default -- ~80% of the budget consumed by waiting
 * that serves no purpose -- and flake intermittently only when the whole
 * suite is loaded.
 *
 * `delay: null` removes the waiting and nothing else: every event userEvent
 * would dispatch is still dispatched, in the same order, through the same
 * code paths. It is safe here specifically because `studio-components.tsx`
 * contains no `setTimeout`/`setInterval`/`requestAnimationFrame` and no
 * debounce, so no assertion in this file depends on time passing between
 * events.
 *
 * Deliberately not fixed by raising the Jest timeout: that hides the
 * contention instead of removing it, and a flake at 5s becomes the same flake
 * at 10s.
 */
function setupUser(
  options: Parameters<typeof userEvent.setup>[0] = {},
): ReturnType<typeof userEvent.setup> {
  return userEvent.setup({ delay: null, ...options });
}
import type {
  AutomationStudioResult,
  StudioRun,
} from "@/lib/types";

jest.mock("@/lib/api", () => ({
  uploadLibraryItem: jest.fn(),
}));

jest.mock("@/components/agent-chat", () => ({
  AgentChat: ({ capability }: { capability: string }) => (
    <div data-testid="agent-chat">{capability}</div>
  ),
  isChatCapability: (capability: string) =>
    ["literature", "grant", "matching", "dataset"].includes(capability),
}));

jest.mock("@/components/research-markdown", () => ({
  ResearchMarkdown: ({
    content,
    label,
    unresolvedSourceIds = [],
  }: {
    content: string;
    label?: string;
    unresolvedSourceIds?: string[];
  }) => (
    <div data-testid="research-markdown">
      <strong>{label}</strong>
      <p>{content}</p>
      <span>{unresolvedSourceIds.join(",")}</span>
    </div>
  ),
}));

const mockedUploadLibraryItem = jest.mocked(uploadLibraryItem);

afterEach(() => {
  jest.restoreAllMocks();
  mockedUploadLibraryItem.mockReset();
});

/**
 * Forces a genuine `click` event through to a React `onClick` handler on an
 * element that React currently considers `disabled`, to test a handler-level
 * guard independent of (i.e. not merely relying on) the disabled attribute
 * blocking the click. A disabled form control never dispatches a real click
 * in either a browser or jsdom, and merely mutating the DOM `disabled`
 * property is *not* sufficient to bypass this: React's own event
 * delegation (`getListener`) additionally checks its own internally
 * recorded props snapshot for the element (not the live DOM attribute)
 * before invoking `onClick` on a `button`/`input`/`select`/`textarea`, and
 * refuses to deliver the event at all if that snapshot says `disabled`.
 * This clears both the DOM property and React's internal snapshot so the
 * click genuinely reaches the production handler, which must then apply
 * its own (independent) guard.
 */
function forceClickBypassingReactDisabled(element: HTMLElement): void {
  (element as HTMLButtonElement).disabled = false;
  const propsKey = Object.keys(element).find((key) =>
    key.startsWith("__reactProps$"),
  );
  if (propsKey) {
    const target = element as unknown as Record<string, Record<string, unknown>>;
    target[propsKey] = { ...target[propsKey], disabled: false };
  }
  fireEvent.click(element);
}

function baseRun(overrides: Partial<StudioRun> = {}): StudioRun {
  return {
    capability: "literature",
    current_stage: "Complete",
    durable_instance_id: "research-run-test",
    id: "run-test",
    owner: "Dr. Maya Chen",
    progress: 100,
    started_at: "2026-07-16T12:00:00Z",
    status: "completed",
    title: "Test run",
    ...overrides,
  };
}

/**
 * Inline run evidence lists the citations that backed the artifact, so a
 * source title legitimately renders twice once a run resolves: once in the
 * studio's own output, once in the provenance list. Assertions about the
 * studio output must say which one they mean rather than relying on the title
 * being globally unique.
 */
describe("InstitutionalStudio", () => {
  it("replaces the institutional workflow with a Work IQ coming-soon page", () => {
    const onRun = jest.fn();
    render(
      <InstitutionalStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Work IQ", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("Plugin coming soon")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Resolve policy answer" }),
    ).not.toBeInTheDocument();
    expect(onRun).not.toHaveBeenCalled();
  });
});

describe("AutomationStudio", () => {
  const automationResult: AutomationStudioResult = {
    run: baseRun({ capability: "orchestration" }),
    template_id: "evidence-review-v2",
    trigger: "Manual",
    // Matches AUTOMATION_TEMPLATES[0] ("evidence-review-v2")'s full,
    // unedited default step graph exactly, so this fixture represents a
    // genuine passing dry run *for the graph currently on screen* rather
    // than for some other, smaller graph -- see the "gates activation"
    // test below for why that distinction matters.
    steps: [
      {
        id: "ingest",
        label: "Ingest & verify",
        kind: "activity",
        depends_on: [],
        retry_limit: 3,
        approval_required: false,
      },
      {
        id: "retrieve",
        label: "Retrieve evidence",
        kind: "fan_out",
        depends_on: ["ingest"],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "synthesize",
        label: "Synthesize",
        kind: "agent",
        depends_on: ["retrieve"],
        retry_limit: 1,
        approval_required: false,
      },
      {
        id: "review",
        label: "Human review",
        kind: "approval",
        depends_on: ["synthesize"],
        retry_limit: 0,
        approval_required: true,
      },
      {
        id: "export",
        label: "Export",
        kind: "external_action",
        depends_on: ["review"],
        retry_limit: 2,
        approval_required: false,
      },
    ],
    validation_errors: [],
    dry_run_status: "passed",
    graph_version: "2.0",
    graph_hash: "abcdef1234567890",
    citations: [],
  };

  it("zooms the workflow graph within bounds", async () => {
    const user = setupUser();
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
      />,
    );

    expect(screen.getByText("100%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByText("110%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    await user.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(screen.getByText("90%")).toBeInTheDocument();
  });

  it("adds, configures, and removes a bounded workflow step and sends edits to dry run", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Notify reviewer");
    await user.keyboard("{Enter}");
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(within(stepEditor).getByText("Notify reviewer")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Configure Ingest & verify" }),
    );
    const retryInput = screen.getByLabelText("Retry limit (0-5)");
    await user.clear(retryInput);
    await user.type(retryInput, "2");
    await user.keyboard("{Enter}");

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    const steps = onRun.mock.calls[0][2].inputs.steps;
    expect(
      steps.some((step: { label: string }) => step.label === "Notify reviewer"),
    ).toBe(true);
    const ingestStep = steps.find((step: { id: string }) => step.id === "ingest");
    expect(ingestStep.retry_limit).toBe(2);
  });

  it("gates activation behind a passing dry run and an explicit confirmation", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    // A passed result the parent already happens to be holding at mount
    // does not by itself authorize activation: nothing in *this* session
    // has confirmed it corresponds to the currently displayed graph, so it
    // starts disabled until an explicit dry run is (re-)run here.
    expect(activateButton).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm activation" }),
    );
    expect(
      screen.getByRole("button", { name: /activated \(draft workspace\)/i }),
    ).toBeDisabled();
  });

  it("moves focus into the dialog on open, contains Tab within it, restores focus to the trigger on close, and closes (without activating) on Escape", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    const closeButton = within(dialog).getByLabelText(
      "Close activation dialog",
    );
    // Focus enters the dialog as soon as it opens, landing on its close
    // button -- a keyboard/screen-reader user is never left stranded with
    // focus on a background element the dialog now visually covers.
    expect(closeButton).toHaveFocus();

    const cancelButton = within(dialog).getByRole("button", {
      name: "Cancel",
    });
    const confirmButton = within(dialog).getByRole("button", {
      name: "Confirm activation",
    });

    // Shift+Tab from the first focusable element (close button) wraps
    // around to the last (Confirm activation) instead of escaping the
    // dialog into the (inert) background page.
    await user.tab({ shift: true });
    expect(confirmButton).toHaveFocus();

    // Tab from the last focusable element wraps back to the first (close
    // button), keeping keyboard focus fully contained within the dialog.
    await user.tab();
    expect(closeButton).toHaveFocus();

    // Ordinary, non-wrapping Tab navigation between the wrap points still
    // works exactly as expected.
    await user.tab();
    expect(cancelButton).toHaveFocus();
    await user.tab();
    expect(confirmButton).toHaveFocus();

    // Escape behaves exactly like Cancel/the close button: it dismisses the
    // dialog but never activates.
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /activated \(draft workspace\)/i }),
    ).not.toBeInTheDocument();
    // Focus is restored to the exact trigger element that opened the
    // dialog, not merely somewhere on the page.
    expect(activateButton).toHaveFocus();
  });

  it("moves focus to the activation status instead of the now-disabled trigger after a successful activation", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    await user.click(activateButton);

    const dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm activation" }),
    );

    // A *successful* activation disables the trigger that opened the dialog.
    // Restoring focus to it would be a no-op in a real browser (disabled
    // elements refuse focus), silently dumping the keyboard user back on
    // document.body with nothing announced. Focus must land on a real,
    // focusable, relevant element instead.
    const disabledTrigger = screen.getByRole("button", {
      name: /activated \(draft workspace\)/i,
    });
    expect(disabledTrigger).toBeDisabled();
    expect(disabledTrigger).not.toHaveFocus();
    expect(document.body).not.toHaveFocus();

    const status = screen.getByTestId("workflow-activation-status");
    expect(status).toHaveFocus();
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent(/workflow activated for this draft workspace/i);
  });

  it("suppresses global shell shortcuts while the activation dialog is open and releases them when it closes", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    // Stands in for research-workbench.tsx's `window` keydown listener, which
    // is what turns Ctrl/Cmd+K into a command palette. It is registered on
    // `window`, above the portalled dialog in the DOM, so it would fire for
    // keystrokes made inside the dialog unless the dialog stops them.
    const shellShortcut = jest.fn();
    window.addEventListener("keydown", shellShortcut);

    try {
      render(
        <AutomationStudio
          result={automationResult}
          running={false}
          error={null}
          onRun={onRun}
        />,
      );

      expect(isBlockingModalOpen()).toBe(false);

      await user.click(
        screen.getByRole("button", { name: "Validate & dry run" }),
      );
      await user.click(
        screen.getByRole("button", { name: "Activate after approval" }),
      );

      // The shell is told to suppress itself for as long as the dialog lives.
      expect(isBlockingModalOpen()).toBe(true);

      shellShortcut.mockClear();
      await user.keyboard("{Control>}k{/Control}");
      // Independent of the shell's own guard: the keystroke never reaches
      // `window` at all, so a command palette cannot open on top of this
      // dialog even if the shell forgot to check.
      expect(shellShortcut).not.toHaveBeenCalled();
      expect(
        screen.getByRole("dialog", { name: /activate graph/i }),
      ).toBeInTheDocument();

      await user.keyboard("{Escape}");
      expect(
        screen.queryByRole("dialog", { name: /activate graph/i }),
      ).not.toBeInTheDocument();
      // Suppression is scoped to the dialog's lifetime, not left latched on.
      expect(isBlockingModalOpen()).toBe(false);

      shellShortcut.mockClear();
      await user.keyboard("{Control>}k{/Control}");
      expect(shellShortcut).toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", shellShortcut);
    }
  });

  it("invalidates a passing dry run after edits and while revalidation is pending or errored", async () => {
    const user = setupUser();
    let resolveRun!: () => void;
    const onRun = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const { rerender } = render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    // A passed result the parent already holds at mount does not by
    // itself authorize activation until this session runs its own dry run.
    expect(activateButton).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(
      screen.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    expect(activateButton).toBeDisabled();
    await act(async () => resolveRun());
    expect(activateButton).toBeEnabled();

    // Editing the configuration after a pass immediately invalidates it.
    await user.selectOptions(screen.getByLabelText("Trigger"), "GitHub");
    expect(activateButton).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeDisabled();
    await act(async () => resolveRun());
    // The parent hasn't applied an updated result yet -- the studio still
    // only has the stale, pre-edit "Manual" result -- so a resolved dry
    // run with no matching server-echoed content still does not enable
    // activation. A mismatched/stale result stays disabled.
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    // Once the parent applies the exact server-echoed result for the
    // configuration currently on screen, activation is enabled again.
    expect(activateButton).toBeEnabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running={false}
        error="Validation transport failed"
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{ ...automationResult, trigger: "GitHub" }}
        running
        error={null}
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();

    rerender(
      <AutomationStudio
        result={{
          ...automationResult,
          trigger: "GitHub",
          validation_errors: ["Approval policy is incomplete."],
        }}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    expect(activateButton).toBeDisabled();
  });

  it("does not re-enable activation when an edit is reverted back to the last-validated content without a fresh dry run", async () => {
    // Regression for an edit-away-then-edit-back fingerprint bypass: the
    // activation gate must track *which draft version* was actually dry
    // run, not just whether the current content happens to match the last
    // validated content again. An edit that is undone (reverted to
    // byte-identical configuration) must still require a new dry run
    // before activation is allowed.
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();

    // Edit away from the validated configuration...
    await user.selectOptions(screen.getByLabelText("Trigger"), "GitHub");
    expect(activateButton).toBeDisabled();

    // ...then edit back to byte-identical content ("Manual", matching the
    // still-current `automationResult` prop) without ever running a new
    // dry run. Content equality alone must not be enough to re-enable
    // activation.
    await user.selectOptions(screen.getByLabelText("Trigger"), "Manual");
    expect(activateButton).toBeDisabled();
  });

  it("cannot activate through a stale-open confirmation dialog after an edit invalidates the gate while it is open", async () => {
    // Regression: the "Confirm activation" button must recheck the gate at
    // confirm time, not just trust that it was valid when the dialog was
    // opened. Opening the dialog and then invalidating the draft (an edit,
    // here removing a step) before pressing "Confirm activation" must not
    // activate.
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );
    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });

    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);
    const dialog = screen.getByRole("dialog", { name: /activate graph/i });

    // Invalidate the gate while the confirmation dialog is still open.
    await user.click(
      screen.getByRole("button", { name: "Remove Export" }),
    );

    const confirmButton = within(dialog).getByRole("button", {
      name: "Confirm activation",
    });
    expect(confirmButton).toBeDisabled();

    // A disabled button never dispatches a real click (both in real
    // browsers and jsdom), so merely confirming the button stays disabled
    // does not by itself prove the handler's own `if (!canActivate) return`
    // recheck actually blocks activation -- it would pass identically if
    // that guard were deleted. Force a genuine `click` event through
    // (bypassing both the DOM `disabled` property and React's own internal
    // disabled bookkeeping) so the click is actually dispatched and reaches
    // the production handler, which still holds the real, now-invalidated
    // `canActivate = false` in its last-committed closure. If the
    // handler-level guard were removed, this would activate; because it is
    // present, it must still refuse.
    forceClickBypassingReactDisabled(confirmButton);

    expect(
      screen.queryByRole("button", { name: /activated \(draft workspace\)/i }),
    ).not.toBeInTheDocument();
    expect(activateButton).toBeDisabled();
  });

  it("adds only an authorized capability catalog entry to the graph and blocks an unauthorized one", async () => {
    const user = setupUser();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [
        {
          id: "literature-agent",
          name: "Literature synthesis",
          model_tier: "Primary",
          status: "Active",
          web_access: "Opt-in public only",
          workflow_steps: ["Protocol", "Search"],
          deployment: "Foundry Hosted Agent",
        },
        {
          id: "grant-agent",
          name: "Grant drafting",
          model_tier: "Primary",
          status: "Disabled",
          web_access: "Opt-in public only",
          workflow_steps: [],
          deployment: "Foundry Hosted Agent",
        },
      ],
      connectors: [],
      runs: [],
    };
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
      />,
    );

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const literatureRow = within(catalog)
      .getByText("Literature synthesis")
      .closest(".step-editor-row") as HTMLElement;
    await user.click(
      within(literatureRow).getByRole("button", { name: "Add to graph" }),
    );
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(
      within(stepEditor).getByText("Literature synthesis"),
    ).toBeInTheDocument();

    const grantRow = within(catalog)
      .getByText("Grant drafting")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(grantRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
  });

  it("closes a manual draft when catalog additions reach the workflow step limit", async () => {
    const user = setupUser();
    const data = {
      agents: [
        {
          id: "literature-agent",
          name: "Literature synthesis",
          model_tier: "Primary",
          status: "Active",
          web_access: "Opt-in public only",
          workflow_steps: ["Protocol", "Search"],
          deployment: "Foundry Hosted Agent",
        },
      ],
      connectors: [],
      runs: [],
    } as unknown as WorkspaceData;

    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Stale ninth step");
    const staleCommit = screen.getByRole("button", { name: "Add" });
    expect(staleCommit).toBeEnabled();

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    for (const label of [
      "Literature synthesis",
      "Literature Studio",
      "Grant Studio",
    ]) {
      const catalogRow = within(catalog)
        .getByText(label)
        .closest(".step-editor-row") as HTMLElement;
      await user.click(
        within(catalogRow).getByRole("button", { name: "Add to graph" }),
      );
    }

    expect(screen.getByRole("heading", { name: "Steps (8/8)" })).toBeInTheDocument();
    expect(staleCommit).not.toBeInTheDocument();
    expect(screen.queryByText("Stale ninth step")).not.toBeInTheDocument();
  });

  it("manages workflow runs by inspecting via existing Runs state and cloning a fresh draft", async () => {
    const user = setupUser();
    const onNavigateToRun = jest.fn();
    const data: Pick<WorkspaceData, "runs"> = {
      runs: [
        {
          id: "run-orc-1",
          durable_instance_id: "research-run-orc-1",
          project_id: "demo-project",
          capability: "orchestration",
          title: "Evidence review graph",
          status: "waiting_for_approval",
          progress: 60,
          current_stage: "Human review",
          owner: "Dr. Maya Chen",
          started_at: "2026-07-16T12:00:00Z",
          completed_at: null,
          artifact_count: 1,
          estimated_cost_usd: 0,
          scheduler_managed: false,
          scheduling_state: "not_managed",
          orchestration_input: null,
          stages: [],
        },
      ],
    };
    render(
      <AutomationStudio
        result={null}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
        onNavigateToRun={onNavigateToRun}
      />,
    );

    const runManager = screen.getByRole("region", {
      name: "Workflow run management",
    });
    expect(
      within(runManager).getByText(/waiting for approval/i),
    ).toBeInTheDocument();

    expect(within(runManager).getByRole("button", { name: "Pause" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Resume" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(within(runManager).getByRole("button", { name: "Cancel" })).toBeDisabled();

    await user.click(
      within(runManager).getByRole("button", { name: "Inspect" }),
    );
    expect(onNavigateToRun).toHaveBeenCalledWith("run-orc-1");

    await user.click(within(runManager).getByRole("button", { name: "Clone" }));
    expect(
      within(runManager).getByText(/cloned evidence review graph into a new draft/i),
    ).toBeInTheDocument();
  });

  it("submits updated templates, toggles catalog previews, and shows validation failures", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    const failedResult = {
      ...automationResult,
      dry_run_status: "failed",
      validation_errors: ["Review step depends on missing evidence output."],
      insight: {
        agent_name: "Workflow automation",
        content: "Dry run failed before any external action was enabled.",
        evidence_state: "verified",
        online_research_used: false,
        referenced_source_ids: [],
        unresolved_source_ids: [],
      },
    } as AutomationStudioResult;
    render(
      <AutomationStudio
        result={failedResult}
        running={false}
        error="Dry run failed"
        onRun={onRun}
        data={{ agents: [], connectors: [], runs: [] } as unknown as WorkspaceData}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Dry run failed");
    await user.click(screen.getByRole("button", { name: /Grant red team/i }));
    expect(screen.getAllByText("Parse notice")).toHaveLength(2);
    expect(screen.queryByText("Ingest & verify")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Trigger"), "Schedule");
    await user.click(
      screen.getByRole("button", { name: "Preview Literature Studio" }),
    );
    expect(
      screen.getByText("Search, screen, extract, and synthesize evidence."),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Preview Literature Studio" }),
    );
    expect(
      screen.queryByText("Search, screen, extract, and synthesize evidence."),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );

    expect(onRun.mock.calls[0][2].inputs).toMatchObject({
      template_id: "grant-review-v2",
      trigger: "Schedule",
    });
    expect(onRun.mock.calls[0][2].inputs.steps).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "parse-notice", label: "Parse notice" }),
        expect.objectContaining({
          id: "approve-submission",
          approval_required: true,
        }),
      ]),
    );
    expect(
      screen.getByText("Review step depends on missing evidence output."),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("research-markdown")).toHaveTextContent(
      "Dry run failed before any external action was enabled.",
    );
  });

  it("dismisses the activation dialog via both the close button and Cancel", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    const activateButton = screen.getByRole("button", {
      name: "Activate after approval",
    });
    // Establish a genuine local pass (matching content, matching draft
    // version) before exercising the activation dialog -- this test isn't
    // about the fingerprint/version gate itself, just the dialog UI, so
    // get past the gate the same way a real user would.
    await user.click(
      screen.getByRole("button", { name: "Validate & dry run" }),
    );
    expect(activateButton).toBeEnabled();
    await user.click(activateButton);
    let dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(within(dialog).getByLabelText("Close activation dialog"));
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();

    await user.click(activateButton);
    dialog = screen.getByRole("dialog", { name: /activate graph/i });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(
      screen.queryByRole("dialog", { name: /activate graph/i }),
    ).not.toBeInTheDocument();
  });

  it("keeps a step draft unsubmittable while its label is empty and discards it on cancel", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    const stepEditor = screen.getByRole("region", {
      name: "Workflow step editor",
    });
    expect(
      within(stepEditor).getByRole("button", { name: "Add" }),
    ).toBeDisabled();
    await user.click(screen.getByLabelText("Step label"));
    await user.keyboard("{Enter}");
    expect(
      within(stepEditor).getByRole("button", { name: "Add" }),
    ).toBeDisabled();
    await user.click(within(stepEditor).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: /^Add$/ })).not.toBeInTheDocument();
  });

  it("adds a configured step, discards an edit on cancel, and removes the step", async () => {
    const user = setupUser();
    const onRun = jest.fn().mockResolvedValue(undefined);
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={onRun}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add step" }));
    await user.type(screen.getByLabelText("Step label"), "Temporary export");
    await user.selectOptions(screen.getByLabelText("Kind"), "external_action");
    await user.click(screen.getByRole("checkbox", { name: "Ingest & verify" }));
    await user.click(screen.getByRole("checkbox", { name: "Ingest & verify" }));
    await user.click(screen.getByRole("checkbox", { name: "Approval required" }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    expect(
      screen.getByRole("button", { name: "Configure Temporary export" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/external action · depends on none · 1 retries · approval gate/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Configure Temporary export" }),
    );
    await user.selectOptions(screen.getByLabelText("Kind"), "agent");
    await user.click(screen.getByRole("checkbox", { name: "Approval required" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(
      screen.getByText(/external action · depends on none · 1 retries · approval gate/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Remove Temporary export" }),
    );
    expect(screen.queryByText("Temporary export")).not.toBeInTheDocument();
  });

  it("catalogs connector tools and shows the active graph version for current runs", async () => {
    const user = setupUser();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [],
      connectors: [
        {
          id: "onedrive_export",
          name: "OneDrive Export",
          category: "Storage",
          description: "Export validated artifacts.",
          auth_kind: "OAuth",
          credential_kind: "none",
          credential_required: false,
          secret_status: "Configured",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/export",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "unready_export",
          name: "Unready Export",
          category: "Storage",
          description: "Not yet proven ready.",
          auth_kind: "OAuth",
          credential_kind: "none",
          credential_required: false,
          secret_status: "Configured",
          enabled: true,
          test_status: "error",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/unready",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "grant_only_export",
          name: "Grant-only Export",
          category: "Storage",
          description: "Ready but assigned outside orchestration.",
          auth_kind: "OAuth",
          credential_kind: "none",
          credential_required: false,
          secret_status: "Configured",
          enabled: true,
          test_status: "ready",
          last_tested_at: null,
          assigned_agents: ["grant"],
          terms_url: "https://example.test/grant-only",
          data_boundary: "Grant outputs only.",
          capabilities: ["Export"],
        },
        {
          id: "ready_with_key_export",
          name: "Ready-with-key Export",
          category: "Storage",
          description: "Runnable using a provided API key.",
          auth_kind: "ApiKey",
          credential_kind: "api_key",
          credential_required: false,
          secret_status: "Configured",
          enabled: true,
          test_status: "ready_with_key",
          last_tested_at: null,
          assigned_agents: ["orchestration"],
          terms_url: "https://example.test/ready-with-key",
          data_boundary: "Project outputs only.",
          capabilities: ["Export"],
        },
      ],
      runs: [
        {
          ...automationResult.run,
          artifact_count: 0,
          capability: "orchestration",
          estimated_cost_usd: 0,
          project_id: "demo-project",
          scheduler_managed: false,
          scheduling_state: "not_managed",
          started_at: "2026-07-16T12:00:00Z",
          title: "Validated graph",
        },
      ],
    };
    render(
      <AutomationStudio
        result={automationResult}
        running={false}
        error={null}
        onRun={jest.fn()}
        data={data as unknown as WorkspaceData}
      />,
    );

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const toolRow = within(catalog)
      .getByText("OneDrive Export")
      .closest(".step-editor-row") as HTMLElement;
    await user.click(
      within(toolRow).getByRole("button", { name: "Preview OneDrive Export" }),
    );
    expect(
      within(toolRow).getByText("Export validated artifacts."),
    ).toBeInTheDocument();
    await user.click(within(toolRow).getByRole("button", { name: "Add to graph" }));
    expect(
      screen.getByRole("button", { name: "Remove OneDrive Export" }),
    ).toBeInTheDocument();
    const unreadyRow = within(catalog)
      .getByText("Unready Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(unreadyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
    const grantOnlyRow = within(catalog)
      .getByText("Grant-only Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(grantOnlyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
    // Regression: a "ready_with_key" connector (API-key-backed, not OAuth) is
    // a genuinely runnable status per the shared isConnectorRunnable/
    // connectorAvailability helper, not just "ready" — the catalog's
    // authorization check must recognize it, not silently exclude it via an
    // inline `test_status === "ready"` comparison.
    const readyWithKeyRow = within(catalog)
      .getByText("Ready-with-key Export")
      .closest(".step-editor-row") as HTMLElement;
    expect(
      within(readyWithKeyRow).getByRole("button", { name: "Add to graph" }),
    ).toBeEnabled();

    const runManager = screen.getByRole("region", {
      name: "Workflow run management",
    });
    expect(within(runManager).getByText("Validated graph")).toBeInTheDocument();
    expect(within(runManager).getByText(/Graph 2\.0/)).toBeInTheDocument();
  });

  // Deliberately one test despite covering three concerns: each phase below
  // consumes the graph state the previous phase produced. The single-step
  // assertions are only reachable after the removal sequence, and the
  // activation-fallback assertions need the local draft to match that reduced
  // graph -- neither state can be constructed from props, so splitting would
  // mean re-performing the removals in each test and doing strictly more
  // total work. The 15s budget reflects genuine sequential UI work, not a
  // timeout raised to paper over contention; this is not the test that
  // flaked (that one ran on the 5s default and has been split).
  it(
    "surfaces capacity, one-step, and activation fallback states without weakening guards",
    async () => {
    const user = setupUser();
    const data: Pick<WorkspaceData, "agents" | "connectors" | "runs"> = {
      agents: [
        {
          id: "literature-agent",
          name: "Literature synthesis",
          model_tier: "Primary",
          status: "Active",
          web_access: "Opt-in public only",
          workflow_steps: ["Protocol"],
          deployment: "Foundry Hosted Agent",
        },
      ],
      connectors: [],
      runs: [],
    };
    let resolveRun!: () => void;
    const onRun = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRun = resolve;
        }),
    );
    const { rerender } = render(
      <AutomationStudio
        result={{
          ...automationResult,
          graph_version: undefined,
          graph_hash: "",
        } as unknown as AutomationStudioResult}
        running={false}
        error={null}
        onRun={onRun}
        data={data as unknown as WorkspaceData}
      />,
    );

    for (const label of ["Stage six", "Stage seven", "Stage eight"]) {
      await user.click(screen.getByRole("button", { name: "Add step" }));
      await user.type(screen.getByLabelText("Step label"), label);
      await user.click(screen.getByRole("button", { name: "Add" }));
    }

    const catalog = screen.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const cappedAddButton = within(catalog).getAllByRole("button", {
      name: "Add to graph",
    })[0];
    expect(cappedAddButton).toBeDisabled();
    expect(cappedAddButton).toHaveAttribute(
      "title",
      "Workflow already has the maximum of 8 steps.",
    );
    fireEvent.click(cappedAddButton);
    expect(screen.getByText("Steps (8/8)")).toBeInTheDocument();

    for (const label of [
      "Stage eight",
      "Stage seven",
      "Stage six",
      "Export",
      "Human review",
      "Synthesize",
      "Retrieve evidence",
    ]) {
      await user.click(
        screen.getByRole("button", {
          name: `Remove ${label}`,
        }),
      );
    }

    const finalRemove = screen.getByRole("button", {
      name: "Remove Ingest & verify",
    });
    expect(finalRemove).toBeDisabled();
    expect(finalRemove).toHaveAttribute(
      "title",
      "A workflow needs at least one step.",
    );
    fireEvent.click(finalRemove);
    expect(screen.getByText("Steps (1/8)")).toBeInTheDocument();

      await user.click(
        screen.getByRole("button", { name: "Configure Ingest & verify" }),
      );
      expect(screen.queryByText("Depends on")).not.toBeInTheDocument();
      await user.clear(screen.getByLabelText("Step label"));
      await user.type(screen.getByLabelText("Step label"), "   ");
      await user.click(screen.getByRole("button", { name: "Save" }));
      expect(screen.getAllByText("Ingest & verify").length).toBeGreaterThan(0);

      const activateButton = screen.getByRole("button", {
        name: "Activate after approval",
      });
      expect(activateButton).toBeDisabled();
      await user.click(screen.getByRole("button", { name: "Validate & dry run" }));
      await act(async () => resolveRun());
      // The parent hasn't applied a result matching this reduced,
      // single-step graph yet, so activation stays gated even though a
      // dry run just resolved.
      expect(activateButton).toBeDisabled();
      rerender(
        <AutomationStudio
          result={{
            ...automationResult,
            trigger: "Manual",
            steps: [automationResult.steps[0]],
            validation_errors: [],
            dry_run_status: "passed",
            graph_version: undefined,
            graph_hash: "",
          } as unknown as AutomationStudioResult}
          running={false}
          error={null}
          onRun={onRun}
          data={data as unknown as WorkspaceData}
        />,
      );
      expect(activateButton).toBeEnabled();
      await user.click(activateButton);
      const dialog = screen.getByRole("dialog", { name: /activate graph 2\.0/i });
      expect(dialog).toHaveTextContent("Activate graph 2.0");
      expect(dialog).not.toHaveTextContent("(hash");
    },
    15000,
  );
});


describe("StudioForCapability", () => {
  it.each([
    ["literature"],
    ["grant"],
    ["matching"],
    ["dataset"],
  ] as const)("routes %s to the shared agent chat", (capability) => {
    const view = render(
      <StudioForCapability
        capability={capability}
        result={null}
        running={false}
        error={null}
        onRun={jest.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByTestId("agent-chat")).toHaveTextContent(capability);
    view.unmount();
  });

  it.each([
    ["institutional_qa", "Work IQ"],
    ["orchestration", "Workflow Automation"],
  ] as const)(
    "keeps %s on its own non-conversational surface",
    (capability, heading) => {
      const view = render(
        <StudioForCapability
          capability={capability}
          result={null}
          running={false}
          error={null}
          onRun={jest.fn().mockResolvedValue(undefined)}
        />,
      );

      expect(screen.getByRole("heading", { name: heading })).toBeInTheDocument();
      expect(screen.queryByTestId("agent-chat")).not.toBeInTheDocument();
      view.unmount();
    },
  );
});
