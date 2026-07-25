/**
 * @jest-environment node
 */
// This proxy is a real security boundary: it decides which backend paths a
// browser-originated request may reach. These tests exercise the allowlist
// directly (not just "does it 200"), because a regression here means an
// unrelated or malicious backend path becomes reachable through the proxy.

import { NextRequest } from "next/server";

import { GET, POST, PUT } from "./route";

function makeRequest(
  url: string,
  init?: { method?: string; body?: string; headers?: Record<string, string> },
): NextRequest {
  return new NextRequest(new URL(url, "http://localhost:3000"), {
    method: init?.method ?? "GET",
    body: init?.body,
    headers: init?.headers,
  });
}

function paramsFor(path: string[]) {
  return { params: Promise.resolve({ path }) };
}

describe("backend proxy allowlist", () => {
  const originalFetch = global.fetch;
  const originalEnv = process.env.INTERNAL_API_URL;

  beforeEach(() => {
    process.env.INTERNAL_API_URL = "http://127.0.0.1:8100";
    global.fetch = jest.fn(async () =>
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;
  });

  afterEach(() => {
    global.fetch = originalFetch;
    process.env.INTERNAL_API_URL = originalEnv;
    jest.restoreAllMocks();
  });

  async function expectAllowlisted(path: string[], search = "") {
    const response = await GET(
      makeRequest(`http://localhost:3000/api/backend/${path.join("/")}${search}`),
      paramsFor(path),
    );
    expect(response.status).toBe(200);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    return (global.fetch as jest.Mock).mock.calls[0][0] as URL;
  }

  async function expectDenied(path: string[], search = "") {
    const response = await GET(
      makeRequest(`http://localhost:3000/api/backend/${path.join("/")}${search}`),
      paramsFor(path),
    );
    expect(response.status).toBe(502);
    expect(global.fetch).not.toHaveBeenCalled();
  }

  it("forwards the api/agent-studio namespace root via the existing api/ prefix", async () => {
    const url = await expectAllowlisted(["api", "agent-studio"]);
    expect(url.toString()).toBe("http://127.0.0.1:8100/api/agent-studio");
  });

  it("forwards real api/agent-studio subpaths", async () => {
    const url = await expectAllowlisted([
      "api",
      "agent-studio",
      "agents",
      "lit-1",
      "draft",
    ]);
    expect(url.toString()).toBe(
      "http://127.0.0.1:8100/api/agent-studio/agents/lit-1/draft",
    );
  });

  it("preserves the query string when forwarding an allowlisted path", async () => {
    const url = await expectAllowlisted(
      ["api", "agent-studio", "agents", "lit-1", "fork"],
      "?version=3",
    );
    expect(url.search).toBe("?version=3");
  });

  it("continues to forward the existing api/ router prefix for non-agent-studio features", async () => {
    const url = await expectAllowlisted(["api", "library"]);
    expect(url.toString()).toBe("http://127.0.0.1:8100/api/library");
  });

  it("continues to forward health checks", async () => {
    await expectAllowlisted(["health"]);
  });

  it("continues to forward ready checks", async () => {
    await expectAllowlisted(["ready"]);
  });

  it("denies an unrelated v1 router the backend has not been reviewed for", async () => {
    await expectDenied(["v1", "other"]);
  });

  it("denies the retired v1/agent-studio mount point now that agent-studio is mounted under api/", async () => {
    // Verified against the real backend (commit 5dab8b7): the agent-studio
    // router's prefix is `/api/agent-studio`, not a standalone `/v1/...`
    // mount. A stale `/v1/agent-studio` allowlist entry would keep open a
    // route nothing serves, so it must be denied like any other unreviewed
    // v1 path.
    await expectDenied(["v1", "agent-studio", "agents"]);
  });

  it("denies a bare v1 segment with no namespace", async () => {
    await expectDenied(["v1"]);
  });

  it("denies path traversal even when nested under an allowlisted prefix", async () => {
    await expectDenied(["api", "agent-studio", "..", "..", "etc", "passwd"]);
  });

  it("denies alternate casing of the allowlisted api/ prefix (fails closed, not bypassed)", async () => {
    await expectDenied(["Api", "Agent-Studio", "agents"]);
  });

  it("denies an alternate-separator segment instead of silently normalizing it", async () => {
    await expectDenied(["api\\agent-studio"]);
  });

  it("forwards method, body, and content-type header on POST", async () => {
    const response = await POST(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/drafts", {
        method: "POST",
        body: JSON.stringify({ intent: "test" }),
        headers: { "Content-Type": "application/json" },
      }),
      paramsFor(["api", "agent-studio", "drafts"]),
    );
    expect(response.status).toBe(200);
    const [, init] = (global.fetch as jest.Mock).mock.calls[0] as [
      URL,
      RequestInit,
    ];
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
  });

  it("returns 502 research_backend_unavailable when the upstream fetch rejects", async () => {
    global.fetch = jest.fn(async () => {
      throw new Error("connection refused");
    }) as unknown as typeof fetch;
    const response = await GET(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/agents"),
      paramsFor(["api", "agent-studio", "agents"]),
    );
    expect(response.status).toBe(502);
    const body = await response.json();
    expect(body.error).toBe("research_backend_unavailable");
  });

  it("returns 413 request_too_large when Content-Length exceeds the cap", async () => {
    const response = await POST(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/drafts", {
        method: "POST",
        body: "x",
        headers: { "Content-Length": "999999999" },
      }),
      paramsFor(["api", "agent-studio", "drafts"]),
    );
    expect(response.status).toBe(413);
    const body = await response.json();
    expect(body.error).toBe("request_too_large");
  });

  it("returns 413 request_too_large when a chunked body exceeds the cap without a declared Content-Length", async () => {
    // Content-Length is absent (as with chunked transfer-encoding), so the
    // gate must catch this via the streamed byte count instead, not the
    // header pre-check covered by the test above. The mock fetch below
    // drains the outgoing body the same way a real HTTP client would, so
    // the bounded transform stream's overflow error actually surfaces.
    global.fetch = jest.fn(async (_url, init?: RequestInit) => {
      const body = init?.body as ReadableStream<Uint8Array> | undefined;
      if (body) {
        const reader = body.getReader();
        for (;;) {
          const { done } = await reader.read();
          if (done) break;
        }
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as unknown as typeof fetch;

    const chunkSize = 11_000_000;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(chunkSize));
        controller.enqueue(new Uint8Array(chunkSize));
        controller.close();
      },
    });
    const request = new NextRequest(
      new URL("http://localhost:3000/api/backend/api/agent-studio/drafts"),
      {
        method: "POST",
        body: stream,
        duplex: "half",
      } as unknown as ConstructorParameters<typeof NextRequest>[1],
    );
    const response = await POST(
      request,
      paramsFor(["api", "agent-studio", "drafts"]),
    );
    expect(response.status).toBe(413);
    const body = await response.json();
    expect(body.error).toBe("request_too_large");
  });

  it("also exposes the PUT export forwarding to the same allowlisted proxy", async () => {
    const response = await PUT(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/drafts/d-1", {
        method: "PUT",
        body: JSON.stringify({ ok: true }),
        headers: { "Content-Type": "application/json" },
      }),
      paramsFor(["api", "agent-studio", "drafts", "d-1"]),
    );
    expect(response.status).toBe(200);
  });

  it("forwards a POST request that has no body", async () => {
    const response = await POST(
      makeRequest(
        "http://localhost:3000/api/backend/api/agent-studio/agents/lit-1/deploy",
        { method: "POST" },
      ),
      paramsFor(["api", "agent-studio", "agents", "lit-1", "deploy"]),
    );
    expect(response.status).toBe(200);
  });

  it("falls back to the default internal API base when INTERNAL_API_URL is unset", async () => {
    delete process.env.INTERNAL_API_URL;
    const url = await expectAllowlisted(["api", "agent-studio", "agents"]);
    expect(url.toString()).toBe(
      "http://127.0.0.1:8000/api/agent-studio/agents",
    );
  });

  it("forwards the platform identity principal header only when trusted", async () => {
    process.env.TRUST_PLATFORM_IDENTITY_HEADERS = "true";
    try {
      await GET(
        makeRequest(
          "http://localhost:3000/api/backend/api/agent-studio/agents",
          { headers: { "X-MS-CLIENT-PRINCIPAL": "trusted-principal" } },
        ),
        paramsFor(["api", "agent-studio", "agents"]),
      );
      const [, init] = (global.fetch as jest.Mock).mock.calls[0] as [
        URL,
        RequestInit,
      ];
      expect(
        (init.headers as Record<string, string>)["X-MS-CLIENT-PRINCIPAL"],
      ).toBe("trusted-principal");
    } finally {
      delete process.env.TRUST_PLATFORM_IDENTITY_HEADERS;
    }
  });

  it("falls back to application/json when the upstream response has no Content-Type", async () => {
    global.fetch = jest.fn(
      async () => new Response(new Uint8Array(Buffer.from("{}")), { status: 200 }),
    ) as unknown as typeof fetch;
    const response = await GET(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/agents"),
      paramsFor(["api", "agent-studio", "agents"]),
    );
    expect(response.headers.get("Content-Type")).toBe("application/json");
  });

  it("logs a stringified non-Error value when the upstream fetch throws a non-Error", async () => {
    global.fetch = jest.fn(async () => {
      throw "upstream exploded";
    }) as unknown as typeof fetch;
    const consoleSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    const response = await GET(
      makeRequest("http://localhost:3000/api/backend/api/agent-studio/agents"),
      paramsFor(["api", "agent-studio", "agents"]),
    );
    expect(response.status).toBe(502);
    expect(consoleSpy).toHaveBeenCalledWith(
      "Research backend proxy failed",
      expect.objectContaining({ error: "upstream exploded" }),
    );
  });
});
