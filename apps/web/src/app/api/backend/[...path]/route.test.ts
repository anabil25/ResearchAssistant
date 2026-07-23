/** @jest-environment node */

import { NextRequest } from "next/server";

import { GET, POST, PUT } from "./route";

const fetchMock = jest.fn<ReturnType<typeof fetch>, Parameters<typeof fetch>>();

function makeRequest(
  path: string,
  init?: ConstructorParameters<typeof NextRequest>[1],
) {
  return new NextRequest(`http://localhost${path}`, init);
}

describe("backend proxy route", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
    fetchMock.mockReset();
    global.fetch = fetchMock;
  });

  afterAll(() => {
    process.env = originalEnv;
  });

  it("forwards allowlisted GET requests without client query data and preserves upstream responses", async () => {
    process.env.INTERNAL_API_URL = "https://backend.example.internal/root/";
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "Temporarily unavailable" }), {
        status: 503,
        headers: {
          "Content-Type": "application/problem+json",
          "X-Request-ID": "upstream-request-id",
        },
      }),
    );

    const response = await GET(
      makeRequest("/api/backend/api/workspace?mode=full&mode=delta", {
        headers: {
          Cookie: "session=secret",
          "X-Request-ID": "client-request-id",
        },
      }),
      { params: Promise.resolve({ path: ["api", "workspace"] }) },
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe(
      "https://backend.example.internal/root/api/workspace",
    );
    expect(init).toMatchObject({
      method: "GET",
      body: undefined,
      cache: "no-store",
      duplex: "half",
    });
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    expect(init?.headers).toEqual({
      "Content-Type": "application/json",
      "X-Request-ID": "client-request-id",
    });

    expect(response.status).toBe(503);
    expect(response.headers.get("content-type")).toContain(
      "application/problem+json",
    );
    expect(response.headers.get("x-request-id")).toBe("upstream-request-id");
    await expect(response.json()).resolves.toEqual({
      detail: "Temporarily unavailable",
    });
  });

  it("forwards methods, bodies, and trusted principal headers for writes", async () => {
    process.env.TRUST_PLATFORM_IDENTITY_HEADERS = "true";
    fetchMock.mockImplementation(async (_url, init) => {
      const forwardedBody = init?.body
        ? await new Response(init.body as BodyInit).text()
        : "";

      expect(forwardedBody).toBe('{"objective":"evidence"}');
      expect(init?.headers).toEqual({
        "Content-Type": "application/json",
        "X-MS-CLIENT-PRINCIPAL": "principal-token",
        "X-Request-ID": "client-request-id",
      });

      return new Response('{"accepted":true}', {
        status: 202,
        headers: {
          "Content-Type": "application/json",
          "X-Request-ID": "upstream-write-id",
        },
      });
    });

    const response = await POST(
      makeRequest("/api/backend/api/runs", {
        method: "POST",
        body: '{"objective":"evidence"}',
        headers: {
          "Content-Type": "application/json",
          Cookie: "session=secret",
          "X-MS-CLIENT-PRINCIPAL": "principal-token",
          "X-Request-ID": "client-request-id",
        },
      }),
      { params: Promise.resolve({ path: ["api", "runs"] }) },
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(202);
    expect(response.headers.get("x-request-id")).toBe("upstream-write-id");
    await expect(response.json()).resolves.toEqual({ accepted: true });
  });

  it("allows bodyless writes and falls back to default response headers", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    const response = await POST(
      makeRequest("/api/backend/ready", {
        method: "POST",
        headers: {
          "Content-Type": "text/plain",
        },
      }),
      { params: Promise.resolve({ path: ["ready"] }) },
    );

    const [, init] = fetchMock.mock.calls[0];
    expect(init?.body).toBeUndefined();
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.get("x-request-id")).toBe("");
    await expect(response.text()).resolves.toBe("");
  });

  it("rejects oversized request bodies using content-length and streamed limits", async () => {
    const tooLargeByHeader = await POST(
      makeRequest("/api/backend/api/library/upload", {
        method: "POST",
        body: "body",
        headers: {
          "Content-Length": "21000001",
          "Content-Type": "text/plain",
        },
      }),
      { params: Promise.resolve({ path: ["api", "library", "upload"] }) },
    );

    expect(tooLargeByHeader.status).toBe(413);
    await expect(tooLargeByHeader.json()).resolves.toMatchObject({
      error: "request_too_large",
      detail: "Request body exceeds 21000000 bytes",
    });

    fetchMock.mockImplementation(async (_url, init) => {
      await new Response(init?.body as BodyInit).arrayBuffer();
      return new Response("ok");
    });

    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(20_000_000));
        controller.enqueue(new Uint8Array(1_500_001));
        controller.close();
      },
    });

    const tooLargeByStream = await PUT(
      makeRequest("/api/backend/api/library/upload", {
        method: "PUT",
        body: stream as BodyInit,
        headers: {
          "Content-Length": "1",
          "Content-Type": "application/octet-stream",
        },
      }),
      { params: Promise.resolve({ path: ["api", "library", "upload"] }) },
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(tooLargeByStream.status).toBe(413);
    await expect(tooLargeByStream.json()).resolves.toMatchObject({
      error: "request_too_large",
      detail: "Request body exceeds 21000000 bytes",
    });
  });

  it("returns a bounded 502 response for disallowed prefixes, traversal, and fetch failures", async () => {
    const consoleError = jest
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    const randomUuid = jest
      .spyOn(global.crypto, "randomUUID")
      .mockReturnValue("generated-request-id");

    const disallowedPrefix = await GET(
      makeRequest("/api/backend/admin"),
      { params: Promise.resolve({ path: ["admin"] }) },
    );
    await expect(disallowedPrefix.json()).resolves.toEqual({
      error: "research_backend_unavailable",
      detail: "The research backend is unavailable.",
    });
    expect(disallowedPrefix.status).toBe(502);
    expect(disallowedPrefix.headers.get("x-request-id")).toBe(
      "generated-request-id",
    );

    const traversal = await GET(
      makeRequest("/api/backend/api/../secrets"),
      { params: Promise.resolve({ path: ["api", "..", "secrets"] }) },
    );
    expect(traversal.status).toBe(502);

    fetchMock.mockRejectedValueOnce("socket hang up");

    const upstreamFailure = await GET(
      makeRequest("/api/backend/health"),
      { params: Promise.resolve({ path: ["health"] }) },
    );

    expect(upstreamFailure.status).toBe(502);
    expect(upstreamFailure.headers.get("x-request-id")).toBe(
      "generated-request-id",
    );
    await expect(upstreamFailure.json()).resolves.toEqual({
      error: "research_backend_unavailable",
      detail: "The research backend is unavailable.",
    });
    expect(consoleError).toHaveBeenCalledTimes(3);
    expect(consoleError).toHaveBeenNthCalledWith(
      1,
      "Research backend proxy failed",
      expect.objectContaining({
        requestId: "generated-request-id",
        error: "Backend route is not allowlisted",
      }),
    );
    expect(consoleError).toHaveBeenNthCalledWith(
      2,
      "Research backend proxy failed",
      expect.objectContaining({
        requestId: "generated-request-id",
        error: "Backend route is not allowlisted",
      }),
    );
    expect(consoleError).toHaveBeenNthCalledWith(
      3,
      "Research backend proxy failed",
      expect.objectContaining({
        requestId: "generated-request-id",
        error: "socket hang up",
      }),
    );

    randomUuid.mockRestore();
    consoleError.mockRestore();
  });
});
