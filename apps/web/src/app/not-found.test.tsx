import { render, screen } from "@testing-library/react";

import NotFound from "./not-found";

describe("not found page", () => {
  it("renders the bounded 404 guidance back to the workbench", () => {
    render(<NotFound />);

    expect(
      screen.getByRole("heading", {
        name: "Research workspace page not found",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The requested page is not part of this accelerator."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Return to the research workbench" }),
    ).toHaveAttribute("href", "/");
  });
});
