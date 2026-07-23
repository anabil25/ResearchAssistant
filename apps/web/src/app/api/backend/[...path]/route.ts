import { NextRequest, NextResponse } from "next/server";

const ALLOWED_PREFIXES = ["api/", "health", "ready"];
const MAX_PROXY_BODY_BYTES = 21_000_000;

class PayloadTooLargeError extends Error {}

function boundedBody(
  body: ReadableStream<Uint8Array> | null,
): ReadableStream<Uint8Array> | undefined {
  if (!body) return undefined;
  let received = 0;
  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        received += chunk.byteLength;
        if (received > MAX_PROXY_BODY_BYTES) {
          throw new PayloadTooLargeError(
            `Request body exceeds ${MAX_PROXY_BODY_BYTES} bytes`,
          );
        }
        controller.enqueue(chunk);
      },
    }),
  );
}

function resolveBackendUrl(path: string[]): URL {
  const joined = path.join("/");
  if (
    joined.includes("..") ||
    !ALLOWED_PREFIXES.some((prefix) => joined.startsWith(prefix))
  ) {
    throw new Error("Backend route is not allowlisted");
  }
  const base = process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000";
  return new URL(joined, `${base.replace(/\/$/, "")}/`);
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const requestId =
    request.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const { path } = await context.params;
    const url = resolveBackendUrl(path);
    const contentLength = Number(request.headers.get("Content-Length") ?? "0");
    if (contentLength > MAX_PROXY_BODY_BYTES) {
      throw new PayloadTooLargeError(
        `Request body exceeds ${MAX_PROXY_BODY_BYTES} bytes`,
      );
    }
    const body =
      request.method === "GET" || request.method === "HEAD"
        ? undefined
        : boundedBody(request.body);
    const principal =
      process.env.TRUST_PLATFORM_IDENTITY_HEADERS === "true"
        ? request.headers.get("X-MS-CLIENT-PRINCIPAL")
        : null;
    const upstream = await fetch(url, {
      method: request.method,
      headers: {
        "Content-Type": request.headers.get("Content-Type") ?? "application/json",
        "X-Request-ID": requestId,
        ...(principal ? { "X-MS-CLIENT-PRINCIPAL": principal } : {}),
      },
      body,
      duplex: "half",
      signal: AbortSignal.timeout(240_000),
      cache: "no-store",
    } as RequestInit & { duplex: "half" });
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("Content-Type") ?? "application/json",
        "X-Request-ID": upstream.headers.get("X-Request-ID") ?? "",
      },
    });
  } catch (error) {
    if (error instanceof PayloadTooLargeError) {
      return NextResponse.json(
        {
          error: "request_too_large",
          detail: error.message,
        },
        { status: 413 },
      );
    }
    console.error("Research backend proxy failed", {
      requestId,
      error: error instanceof Error ? error.message : String(error),
    });
    return NextResponse.json(
      {
        error: "research_backend_unavailable",
        detail: "The research backend is unavailable.",
      },
      {
        status: 502,
        headers: { "X-Request-ID": requestId },
      },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
