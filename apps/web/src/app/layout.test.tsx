import { renderToStaticMarkup } from "react-dom/server";

jest.mock("next/font/google", () => ({
  Geist: jest.fn(() => ({ variable: "font-geist-sans" })),
  Geist_Mono: jest.fn(() => ({ variable: "font-geist-mono" })),
  Lora: jest.fn(() => ({ variable: "font-editorial" })),
}));

import RootLayout, { metadata } from "./layout";

describe("root layout", () => {
  it("exports stable metadata for the evidence workbench", () => {
    expect(metadata).toMatchObject({
      title: "Research Assistant | Evidence workbench",
      description:
        "An evidence-governed research workbench for literature, grants, institutional guidance, matching, datasets, and durable workflows.",
    });
  });

  it("renders the required document shell and app providers", () => {
    const markup = renderToStaticMarkup(
      <RootLayout>
        <span>Workbench content</span>
      </RootLayout>,
    );
    const documentNode = new DOMParser().parseFromString(markup, "text/html");

    expect(documentNode.documentElement.getAttribute("lang")).toBe("en");
    expect(documentNode.documentElement.className).toContain("font-geist-sans");
    expect(documentNode.documentElement.className).toContain("font-geist-mono");
    expect(documentNode.documentElement.className).toContain("font-editorial");
    expect(documentNode.documentElement.className).toContain("h-full");
    expect(documentNode.documentElement.className).toContain("antialiased");
    expect(documentNode.querySelector("body")).not.toBeNull();

    const provider = documentNode.querySelector(".app-fluent-provider");
    expect(provider).not.toBeNull();
    expect(provider?.textContent).toContain("Workbench content");
  });
});
