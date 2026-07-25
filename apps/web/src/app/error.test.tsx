import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ErrorBoundary from "./error";

describe("app error boundary", () => {
  it("logs the rendering failure and lets the user retry", async () => {
    const reset = jest.fn();
    const consoleError = jest
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const user = userEvent.setup();
    const firstError = Object.assign(new Error("primary failure"), {
      digest: "digest-1",
    });
    const nextError = new Error("secondary failure");
    const { rerender } = render(
      <ErrorBoundary error={firstError} reset={reset} />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The research workbench could not load",
    );
    expect(
      screen.getByText(
        "The failure was recorded without exposing internal service details.",
      ),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(consoleError).toHaveBeenCalledWith(
        "Research workbench render failed",
        firstError,
      ),
    );

    rerender(<ErrorBoundary error={nextError} reset={reset} />);
    await waitFor(() =>
      expect(consoleError).toHaveBeenLastCalledWith(
        "Research workbench render failed",
        nextError,
      ),
    );

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(reset).toHaveBeenCalledTimes(1);
    consoleError.mockRestore();
  });
});
