import { render, screen } from "@testing-library/react";

import { PolicyGatedExternalLink } from "./policy-gated-external-link";

describe("PolicyGatedExternalLink", () => {
  it("renders an accessible external link for an approved URL", () => {
    render(
      <PolicyGatedExternalLink url="https://www.crossref.org/terms/">
        Provider terms
      </PolicyGatedExternalLink>,
    );

    const link = screen.getByRole("link", { name: /provider terms/i });
    expect(link).toHaveAttribute("href", "https://www.crossref.org/terms/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAttribute("data-terms-state", "ready");
  });

  it("applies an optional className to the allowed link", () => {
    render(
      <PolicyGatedExternalLink
        url="https://www.crossref.org/terms/"
        className="custom-link"
      >
        Provider terms
      </PolicyGatedExternalLink>,
    );

    expect(screen.getByRole("link", { name: /provider terms/i })).toHaveClass(
      "custom-link",
    );
  });

  it("renders a visible blocked status for a disallowed URL, never a raw link", () => {
    render(
      <PolicyGatedExternalLink url="https://evil.example.com/terms">
        Provider terms
      </PolicyGatedExternalLink>,
    );

    expect(
      screen.queryByRole("link", { name: /provider terms/i }),
    ).not.toBeInTheDocument();

    const blocked = screen.getByRole("status");
    expect(blocked).toHaveAttribute("data-terms-state", "blocked-url");
    expect(blocked).toHaveAttribute(
      "aria-label",
      "This link targets a host that is not on the approved list.",
    );
  });

  it("applies an optional className to the blocked status alongside the base class", () => {
    render(
      <PolicyGatedExternalLink url={null} className="custom-link">
        Provider terms
      </PolicyGatedExternalLink>,
    );

    const blocked = screen.getByRole("status");
    expect(blocked).toHaveClass("connector-terms-blocked");
    expect(blocked).toHaveClass("custom-link");
  });

  it("supports a surface-owned allowlist without weakening the default policy", () => {
    render(
      <PolicyGatedExternalLink
        url="https://connections.example.org/terms"
        policy={{ allowedHosts: new Set(["connections.example.org"]) }}
      >
        Connection terms
      </PolicyGatedExternalLink>,
    );

    expect(screen.getByRole("link", { name: "Connection terms" })).toHaveAttribute(
      "href",
      "https://connections.example.org/terms",
    );
  });
});
