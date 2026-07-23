import { render, screen } from "@testing-library/react";

import { AppProviders } from "./app-providers";

describe("AppProviders", () => {
  it("wraps children in the Fluent provider shell", () => {
    const { container } = render(
      <AppProviders>
        <span>Evidence-first UI</span>
      </AppProviders>,
    );

    expect(screen.getByText("Evidence-first UI")).toBeInTheDocument();
    const provider = container.querySelector(".app-fluent-provider");
    expect(provider).not.toBeNull();
    expect(provider).toHaveTextContent("Evidence-first UI");
  });
});
