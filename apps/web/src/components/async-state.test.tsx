import { render, screen } from "@testing-library/react";

import {
  AsyncStateBanner,
  ToneBadge,
  classifyAsyncError,
  classifyBuilderMutationError,
} from "@/components/async-state";
import { ApiError } from "@/lib/api";

describe("classifyAsyncError", () => {
  it.each([
    [401, "unauthorized"],
    [403, "unauthorized"],
  ] as const)("maps %i to %s", (status, kind) => {
    expect(classifyAsyncError(new ApiError("nope", status)).kind).toBe(kind);
  });

  it.each([
    [404, "unavailable"],
    [501, "unavailable"],
  ] as const)("maps %i to %s with a not-implemented-yet prefix", (status, kind) => {
    const classified = classifyAsyncError(new ApiError("no route", status));
    expect(classified.kind).toBe(kind);
    expect(classified.message).toMatch(/isn't implemented yet/);
  });

  it.each([
    [424, "needs_connection"],
    [428, "needs_connection"],
  ] as const)("maps %i to %s", (status, kind) => {
    expect(classifyAsyncError(new ApiError("connect first", status)).kind).toBe(kind);
  });

  it("maps a generic 409 to needs_approval (a real governance hold, distinct from a builder-mutation etag conflict)", () => {
    const classified = classifyAsyncError(new ApiError("Approval required", 409));
    expect(classified.kind).toBe("needs_approval");
    expect(classified.message).toBe("Approval required");
  });

  it.each([
    [502, "degraded"],
    [503, "degraded"],
    [504, "degraded"],
  ] as const)("maps %i to %s", (status, kind) => {
    expect(classifyAsyncError(new ApiError("upstream down", status)).kind).toBe(kind);
  });

  it("falls back to a generic error for any other ApiError status", () => {
    expect(classifyAsyncError(new ApiError("weird", 418)).kind).toBe("error");
  });

  it("falls back to a generic error for a non-ApiError thrown value, using its message when it is an Error", () => {
    expect(classifyAsyncError(new Error("boom")).message).toBe("boom");
    expect(classifyAsyncError("not an error").message).toBe("Request failed.");
    expect(classifyAsyncError("not an error").kind).toBe("error");
  });
});

describe("classifyBuilderMutationError", () => {
  it("maps a 409 from a draft-mutation endpoint to conflict, never needs_approval, with an actionable reload message", () => {
    const classified = classifyBuilderMutationError(
      new ApiError("stale etag", 409),
    );
    expect(classified.kind).toBe("conflict");
    expect(classified.message).toMatch(/etag conflict/);
    expect(classified.message).toMatch(/Reload the draft/);
  });

  it("delegates every non-409 status to the generic classifier unchanged", () => {
    expect(classifyBuilderMutationError(new ApiError("nope", 404)).kind).toBe(
      "unavailable",
    );
    expect(classifyBuilderMutationError(new Error("boom")).kind).toBe("error");
  });
});

describe("AsyncStateBanner", () => {
  it("uses role=alert for error and conflict kinds, so screen readers interrupt for them", () => {
    render(<AsyncStateBanner kind="error" message="Something broke" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Something broke");
  });

  it("uses role=alert for conflict", () => {
    render(<AsyncStateBanner kind="conflict" message="Etag mismatch" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Etag mismatch");
  });

  it("uses role=status (not alert) for non-error/conflict kinds", () => {
    render(<AsyncStateBanner kind="unavailable" message="Not ready" />);
    expect(screen.getByRole("status")).toHaveTextContent("Not ready");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders a Retry button only when onRetry is provided, and invokes it on click", async () => {
    const { rerender } = render(
      <AsyncStateBanner kind="degraded" message="Down" />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    const onRetry = jest.fn();
    rerender(<AsyncStateBanner kind="degraded" message="Down" onRetry={onRetry} />);
    screen.getByRole("button", { name: "Retry" }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders the distinct title for every AsyncErrorKind", () => {
    const kinds = [
      ["unauthorized", "Not authorized"],
      ["unavailable", "Not available yet"],
      ["needs_connection", "Needs a connection"],
      ["needs_approval", "Needs approval"],
      ["degraded", "Degraded"],
      ["conflict", "Conflict"],
      ["error", "Something went wrong"],
    ] as const;
    for (const [kind, title] of kinds) {
      const { unmount } = render(<AsyncStateBanner kind={kind} message="x" />);
      expect(screen.getByText(title)).toBeInTheDocument();
      unmount();
    }
  });
});

describe("ToneBadge", () => {
  it("renders a distinct, non-color-only text label and data-tone for every BadgeTone", () => {
    const tones = [
      ["success", "Success"],
      ["unauthorized", "Not authorized"],
      ["unavailable", "Not available yet"],
      ["needs_connection", "Needs a connection"],
      ["needs_approval", "Needs approval"],
      ["degraded", "Degraded"],
      ["conflict", "Conflict"],
      ["error", "Something went wrong"],
    ] as const;
    for (const [tone, label] of tones) {
      const { unmount } = render(<ToneBadge kind={tone} />);
      const badge = screen.getByText(label);
      expect(badge).toBeInTheDocument();
      expect(badge.closest(".tone-badge")).toHaveAttribute("data-tone", tone);
      unmount();
    }
  });
});
