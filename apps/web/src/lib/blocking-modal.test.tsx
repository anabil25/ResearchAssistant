import { act, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";

import {
  isBlockingModalOpen,
  openBlockingModal,
  subscribeBlockingModal,
  useBlockingModalOpen,
} from "@/lib/blocking-modal";

function Probe() {
  const open = useBlockingModalOpen();
  return <span data-testid="probe">{open ? "blocked" : "free"}</span>;
}

describe("blocking modal registry", () => {
  // The counter is module-level state shared across tests in this file, so
  // each test releases everything it opens; this guards against one test's
  // leak silently satisfying the next one's assertion.
  afterEach(() => {
    expect(isBlockingModalOpen()).toBe(false);
  });

  it("reports no blocking modal until one is opened", () => {
    expect(isBlockingModalOpen()).toBe(false);
  });

  it("stays blocked until the last of several overlapping modals closes", () => {
    const releaseFirst = openBlockingModal();
    expect(isBlockingModalOpen()).toBe(true);

    const releaseSecond = openBlockingModal();
    expect(isBlockingModalOpen()).toBe(true);

    // The outer modal closing first must not un-suppress the shell while the
    // inner one is still on screen -- the reason this is a depth counter and
    // not a boolean.
    releaseFirst();
    expect(isBlockingModalOpen()).toBe(true);

    releaseSecond();
    expect(isBlockingModalOpen()).toBe(false);
  });

  it("ignores a repeated release instead of double-decrementing the counter", () => {
    const releaseOuter = openBlockingModal();
    const releaseInner = openBlockingModal();

    releaseInner();
    // A double-invoked effect cleanup (React StrictMode does exactly this)
    // must not decrement twice and drop the still-open outer modal's claim.
    releaseInner();
    expect(isBlockingModalOpen()).toBe(true);

    releaseOuter();
    expect(isBlockingModalOpen()).toBe(false);
  });

  it("notifies subscribers on open and close, and stops after unsubscribe", () => {
    const listener = jest.fn();
    const unsubscribe = subscribeBlockingModal(listener);

    const release = openBlockingModal();
    expect(listener).toHaveBeenCalledTimes(1);

    release();
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    const releaseAgain = openBlockingModal();
    expect(listener).toHaveBeenCalledTimes(2);
    releaseAgain();
  });

  it("re-renders a subscribed component as modals open and close", () => {
    render(<Probe />);
    expect(screen.getByTestId("probe")).toHaveTextContent("free");

    let release!: () => void;
    act(() => {
      release = openBlockingModal();
    });
    expect(screen.getByTestId("probe")).toHaveTextContent("blocked");

    act(() => {
      release();
    });
    expect(screen.getByTestId("probe")).toHaveTextContent("free");
  });

  it("renders as unblocked on the server even while a modal is open on the client", () => {
    // Server rendering has no modals, and reading the live client counter
    // during SSR would produce markup that disagrees with the first client
    // render. Exercised through a real server render so the SSR snapshot path
    // is genuinely taken rather than asserted about.
    const release = openBlockingModal();
    try {
      expect(isBlockingModalOpen()).toBe(true);
      expect(renderToString(<Probe />)).toContain("free");
    } finally {
      release();
    }
  });
});
