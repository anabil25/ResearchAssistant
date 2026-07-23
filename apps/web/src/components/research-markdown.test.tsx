import { render, screen, within } from "@testing-library/react";
import type { ComponentType, ReactNode } from "react";

type MarkdownRenderer = (props: Record<string, unknown>) => ReactNode;
let mockCapturedComponents: Record<string, MarkdownRenderer> = {};

jest.mock("harden-react-markdown", () => ({
  __esModule: true,
  default: (MarkdownComponent: ComponentType<Record<string, unknown>>) =>
    function HardenedMarkdown(props: Record<string, unknown>) {
      return <MarkdownComponent {...props} />;
    },
}));

jest.mock("remark-gfm", () => ({
  __esModule: true,
  default: jest.fn(),
}));

jest.mock("rehype-sanitize", () => ({
  __esModule: true,
  default: jest.fn(),
}));

jest.mock("react-markdown", () => {
  const React = jest.requireActual<typeof import("react")>("react");

  const defaultUrlTransform = (url: string) => url;

  function MockReactMarkdown({
    allowedImagePrefixes = [],
    allowedLinkPrefixes = [],
    children,
    components = {},
    skipHtml,
  }: {
    allowedImagePrefixes?: string[];
    allowedLinkPrefixes?: string[];
    children: string;
    components?: Record<string, (props: Record<string, unknown>) => ReactNode>;
    skipHtml?: boolean;
  }) {
    const nodes: ReactNode[] = [];
    const content = String(children);
    mockCapturedComponents = components;
    const linkMatch = content.match(/\[([^\]]+)\]\(([^)]+)\)/);
    const imageMatch = content.match(/!\[([^\]]*)\]\(([^)]+)\)/);
    const codeBlocks = [...content.matchAll(/```([a-z0-9+#-]*)\n([\s\S]*?)```/gi)];

    if (linkMatch) {
      const [, text, href] = linkMatch;
      const allowed =
        href.startsWith("#") ||
        allowedLinkPrefixes.includes("*") ||
        allowedLinkPrefixes.some((prefix) => href.startsWith(prefix));

      nodes.push(
        allowed
          ? components.a?.({ children: text, href })
          : React.createElement(
              "span",
              { key: "blocked-link", title: href },
              `${text} [blocked]`,
            ),
      );
    }

    if (imageMatch) {
      const [, alt, src] = imageMatch;
      const allowed =
        allowedImagePrefixes.includes("*") ||
        allowedImagePrefixes.some((prefix) => src.startsWith(prefix));

      nodes.push(
        allowed
          ? React.createElement("img", { key: "image", alt, src })
          : React.createElement(
              "span",
              { key: "blocked-image" },
              `[Image blocked: ${alt}]`,
            ),
      );
    }

    if (content.includes("| Study | Result |")) {
      nodes.push(
        React.cloneElement(
          components.table?.({
            children: [
              React.createElement(
                "thead",
                { key: "head" },
                React.createElement(
                  "tr",
                  null,
                  components.th?.({ children: "Study" }),
                  components.th?.({ children: "Result" }),
                ),
              ),
              React.createElement(
                "tbody",
                { key: "body" },
                React.createElement(
                  "tr",
                  null,
                  React.createElement("td", null, "Trial A"),
                  React.createElement("td", null, "Positive"),
                ),
              ),
            ],
          }) as React.ReactElement,
          { key: "table" },
        ),
      );
    }

    if (content.includes("- [x] audited")) {
      nodes.push(
        React.createElement(
          "ul",
          { key: "tasks" },
          React.createElement(
            "li",
            null,
            React.createElement("input", {
              type: "checkbox",
              checked: true,
              disabled: true,
              readOnly: true,
            }),
            "audited",
          ),
          React.createElement(
            "li",
            null,
            React.createElement("input", {
              type: "checkbox",
              checked: false,
              disabled: true,
              readOnly: true,
            }),
            "pending",
          ),
        ),
      );
    }

    codeBlocks.forEach(([, language, source], index) => {
      const codeNode = components.code?.({
        children: source,
        className: language ? `language-${language}` : undefined,
      });

      nodes.push(
        React.cloneElement(
          components.pre?.({ children: codeNode }) as React.ReactElement,
          { key: `code-${index}` },
        ),
      );
    });

    const sanitizedText = skipHtml
      ? content
          .replace(/<script[\s\S]*?<\/script>/gi, "")
          .replace(/<[^>]+>/g, "")
          .replace(/!\[[^\]]*]\([^)]+\)/g, "")
          .replace(/\[[^\]]+]\([^)]+\)/g, "")
          .replace(/```[\s\S]*?```/g, "")
      : content;

    const flattenedText = sanitizedText
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line) => !line.startsWith("|"))
      .filter((line) => !line.startsWith("- ["))
      .join(" ");

    if (flattenedText) {
      nodes.unshift(React.createElement("p", { key: "text" }, flattenedText));
    }

    return React.createElement(React.Fragment, null, ...nodes);
  }

  return {
    __esModule: true,
    default: MockReactMarkdown,
    defaultUrlTransform,
  };
});

import { ResearchMarkdown } from "./research-markdown";

describe("ResearchMarkdown", () => {
  it("renders bounded markdown, evidence citations, and unsupported references", () => {
    const longSnippet = "x".repeat(20_005);

    const { container } = render(
      <ResearchMarkdown
        content={`
Blocked [external link](https://example.com/docs) and ![Tracking pixel](https://example.com/pixel.png)

| Study | Result |
| --- | --- |
| Trial A | Positive |

- [x] audited
- [ ] pending

\`\`\`typescript
const evidence = 42;
\`\`\`

\`\`\`brainfuck
${longSnippet}
\`\`\`

Before <script>alert("xss")</script><div>unsafe html</div> After
`}
        citations={[
          {
            canonical_url: "https://evidence.example.org/report",
            checksum: "sha256:1",
            chunk_id: "chunk-1",
            id: "citation-1",
            license: "CC BY 4.0",
            page_start: 7,
            quote: "Bounded quote",
            section: "Methods",
            source_id: "src-1",
            title: "Primary evidence",
          },
          {
            canonical_url: "not a url",
            checksum: "sha256:2",
            chunk_id: "chunk-2",
            id: "citation-2",
            license: "Internal",
            quote: "Malformed URL",
            section: "Appendix",
            source_id: "src-2",
            title: "Malformed evidence",
          },
          {
            canonical_url: "ftp://archive.example.org/report",
            checksum: "sha256:3",
            chunk_id: "chunk-3",
            id: "citation-3",
            license: "Public domain",
            quote: "FTP URL",
            section: "Archive",
            source_id: "src-3",
            title: "Protocol-blocked evidence",
          },
          {
            canonical_url: "http://archive.example.org/report",
            checksum: "sha256:4",
            chunk_id: "chunk-4",
            id: "citation-4",
            license: "Public domain",
            quote: "HTTP URL",
            section: "Archive",
            source_id: "src-4",
            title: "Archived evidence",
          },
          {
            canonical_url: null,
            checksum: "sha256:5",
            chunk_id: "chunk-5",
            id: "citation-5",
            license: "Internal",
            quote: "Missing URL",
            section: "Notes",
            source_id: "src-5",
            title: "Unlinked evidence",
          },
        ]}
        unresolvedSourceIds={["missing-7", "missing-9"]}
      />,
    );

    const markdownRegion = screen.getByRole("region", {
      name: "Research artifact",
    });
    expect(markdownRegion).toBeInTheDocument();
    expect(screen.getByText(/external link \[blocked]/i)).toBeInTheDocument();
    expect(
      screen.getByText("[Image blocked: Tracking pixel]"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("unsafe html")).not.toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();

    const tableWrapper = container.querySelector(".research-table-scroll");
    expect(tableWrapper).toHaveAttribute("tabindex", "0");
    expect(
      within(tableWrapper as HTMLElement).getByRole("table"),
    ).toBeInTheDocument();
    expect(
      within(tableWrapper as HTMLElement).getByRole("columnheader", {
        name: "Study",
      }),
    ).toHaveAttribute("scope", "col");

    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[0]).toBeDisabled();
    expect(checkboxes[1]).not.toBeChecked();
    expect(checkboxes[1]).toBeDisabled();

    const codeBlocks = container.querySelectorAll("pre code");
    expect(codeBlocks[0]).toHaveAttribute("data-language", "typescript");
    expect(codeBlocks[0]).toHaveTextContent("const evidence = 42;");
    expect(codeBlocks[1]).toHaveAttribute("data-language", "text");
    expect(codeBlocks[1]).toHaveTextContent("[code block truncated]");
    expect(codeBlocks[1].textContent).toHaveLength(20_023);
    expect(container.querySelectorAll("pre[tabindex='0']")).toHaveLength(2);

    const evidenceSection = screen.getByRole("region", {
      name: "Resolved evidence",
    });
    const evidenceItems = within(evidenceSection).getAllByRole("listitem");
    expect(
      within(evidenceItems[0]).getByRole("link", {
        name: "Primary evidence (opens in a new tab)",
      }),
    ).toHaveAttribute("href", "https://evidence.example.org/report");
    expect(evidenceItems[0]).toHaveTextContent("Methods, page 7 - src-1");
    expect(within(evidenceItems[1]).queryByRole("link")).toBeNull();
    expect(evidenceItems[1]).toHaveTextContent("Malformed evidence");
    expect(evidenceItems[1]).toHaveTextContent("Appendix - src-2");
    expect(within(evidenceItems[2]).queryByRole("link")).toBeNull();
    expect(evidenceItems[2]).toHaveTextContent("Protocol-blocked evidence");
    expect(evidenceItems[2]).toHaveTextContent("Archive - src-3");
    expect(
      within(evidenceItems[3]).getByRole("link", {
        name: "Archived evidence (opens in a new tab)",
      }),
    ).toHaveAttribute("href", "http://archive.example.org/report");
    expect(evidenceItems[3]).toHaveTextContent("Archive - src-4");
    expect(within(evidenceItems[4]).queryByRole("link")).toBeNull();
    expect(evidenceItems[4]).toHaveTextContent("Unlinked evidence");
    expect(evidenceItems[4]).toHaveTextContent("Notes - src-5");

    expect(screen.getByRole("note")).toHaveTextContent(
      "Unsupported referencesmissing-7, missing-9",
    );
  });

  it("supports custom labels", () => {
    render(
      <ResearchMarkdown content="Plain text" label="Custom evidence panel" />,
    );

    expect(
      screen.getByRole("region", { name: "Custom evidence panel" }),
    ).toHaveTextContent("Plain text");
  });

  it("renders allowed hash links securely and keeps href-less text noninteractive", () => {
    const { rerender } = render(
      <ResearchMarkdown content="[Methods](#methods)" />,
    );

    const hashLink = screen.getByRole("link", {
      name: "Methods (opens in a new tab)",
    });
    expect(hashLink).toHaveAttribute("href", "#methods");
    expect(hashLink).toHaveAttribute("target", "_blank");
    expect(hashLink).toHaveAttribute("rel", "noopener noreferrer");

    const anchorRenderer = mockCapturedComponents.a;
    expect(anchorRenderer).toBeDefined();
    rerender(
      <>
        {anchorRenderer({
          children: (
            <>
              <strong>Formatted methods</strong>
              {false}
            </>
          ),
          href: "#formatted-methods",
        })}
      </>,
    );

    expect(
      screen.getByRole("link", {
        name: "Formatted methods (opens in a new tab)",
      }),
    ).toHaveAttribute("href", "#formatted-methods");

    rerender(<>{anchorRenderer({ children: "Missing destination" })}</>);

    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Missing destination").tagName).toBe("SPAN");
  });
});
