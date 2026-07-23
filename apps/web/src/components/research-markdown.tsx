"use client";

import hardenReactMarkdown from "harden-react-markdown";
import { Children, isValidElement } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

import type { Citation } from "@/lib/types";

const HardenedMarkdown = hardenReactMarkdown(ReactMarkdown);
const MAX_CODE_CHARACTERS = 20_000;
const SAFE_CODE_LANGUAGES = new Set([
  "bash",
  "csharp",
  "css",
  "csv",
  "go",
  "html",
  "java",
  "javascript",
  "json",
  "jsx",
  "kql",
  "markdown",
  "plaintext",
  "powershell",
  "python",
  "sql",
  "text",
  "tsx",
  "typescript",
  "xml",
  "yaml",
]);

function codeLanguage(className: string | undefined): string {
  const candidate = className?.match(/language-([a-z0-9+#-]+)/i)?.[1]?.toLowerCase();
  return candidate && SAFE_CODE_LANGUAGES.has(candidate) ? candidate : "text";
}

function safeCitationUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

function accessibleText(children: React.ReactNode): string {
  let text = "";
  Children.forEach(children, (child) => {
    if (typeof child === "string" || typeof child === "number") {
      text += String(child);
    } else if (isValidElement<{ children?: React.ReactNode }>(child)) {
      text += accessibleText(child.props.children);
    }
  });
  return text;
}

export interface ResearchMarkdownProps {
  content: string;
  citations?: Citation[];
  unresolvedSourceIds?: string[];
  label?: string;
}

const researchMarkdownComponents = {
  a: ({
    children,
    href,
  }: {
    children?: React.ReactNode;
    href?: string;
  }) =>
    href ? (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`${accessibleText(children)} (opens in a new tab)`}
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  code: ({
    children,
    className,
  }: {
    children?: React.ReactNode;
    className?: string;
  }) => {
    const source = String(children);
    const truncated =
      source.length > MAX_CODE_CHARACTERS
        ? `${source.slice(0, MAX_CODE_CHARACTERS)}\n[code block truncated]`
        : source;
    return <code data-language={codeLanguage(className)}>{truncated}</code>;
  },
  pre: ({ children }: { children?: React.ReactNode }) => (
    <pre tabIndex={0}>{children}</pre>
  ),
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="research-table-scroll" tabIndex={0}>
      <table>{children}</table>
    </div>
  ),
  th: ({ children }: { children?: React.ReactNode }) => (
    <th scope="col">{children}</th>
  ),
};

export function ResearchMarkdown({
  content,
  citations = [],
  unresolvedSourceIds = [],
  label = "Research artifact",
}: ResearchMarkdownProps) {
  return (
    <section className="research-markdown" aria-label={label}>
      <HardenedMarkdown
        allowedImagePrefixes={[]}
        allowedLinkPrefixes={[]}
        allowDataImages={false}
        defaultOrigin="https://research-assistant.invalid"
        imageBlockPolicy="indicator"
        linkBlockPolicy="indicator"
        rehypePlugins={[rehypeSanitize]}
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={researchMarkdownComponents}
      >
        {content}
      </HardenedMarkdown>

      {citations.length ? (
        <section className="research-evidence-links" aria-label="Resolved evidence">
          <h3>Resolved evidence</h3>
          <ol>
            {citations.map((citation) => {
              const citationUrl = safeCitationUrl(citation.canonical_url);
              return (
                <li key={citation.id}>
                  {citationUrl ? (
                    <a
                      href={citationUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`${citation.title} (opens in a new tab)`}
                    >
                      {citation.title}
                    </a>
                  ) : (
                    <span>{citation.title}</span>
                  )}
                  <small>
                    {citation.section}
                    {citation.page_start ? `, page ${citation.page_start}` : ""}
                    {" - "}
                    {citation.source_id}
                  </small>
                </li>
              );
            })}
          </ol>
        </section>
      ) : null}

      {unresolvedSourceIds.length ? (
        <aside className="unsupported-evidence" role="note">
          <strong>Unsupported references</strong>
          <span>{unresolvedSourceIds.join(", ")}</span>
        </aside>
      ) : null}
    </section>
  );
}
