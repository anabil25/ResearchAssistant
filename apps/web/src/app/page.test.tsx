import { render, screen } from "@testing-library/react";

jest.mock("@/components/research-workbench", () => ({
  ResearchWorkbench: () => (
    <section aria-label="Research workbench" data-testid="research-workbench">
      Workbench shell
    </section>
  ),
}));

import Home from "./page";

describe("home page", () => {
  it("renders the research workbench entry point", () => {
    render(<Home />);

    expect(screen.getByTestId("research-workbench")).toHaveTextContent(
      "Workbench shell",
    );
    expect(
      screen.getByRole("region", { name: "Research workbench" }),
    ).toBeInTheDocument();
  });
});
