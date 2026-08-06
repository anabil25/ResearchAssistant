import { NextRequest, NextResponse } from "next/server";

// Only "api/" (every feature router, including agent-studio — see below)
// and the two health-check paths may reach the backend.
//
// The agent-studio router's mount point moved during Phase2: earlier
// backend checkpoints (`d6df0fe`) mounted it at a standalone `/v1/agent-studio`
// prefix, which is why an earlier round of this file carried a dedicated
// `AGENT_STUDIO_PATH` allowlist entry alongside the generic "api/" prefix.
// Verified directly against the current backend commit `5dab8b7`
// (`services/api/src/research_assistant_api/agent_studio/router.py`):
// `router = APIRouter(prefix="/api/agent-studio", ...)`, mounted via
// `app.include_router(agent_studio_router)` with no extra prefix — so the
// real, final mount point is `/api/agent-studio`, already inside the
// existing "api/" prefix. The standalone `/v1/agent-studio` entry is now
// removed: it is no longer a real backend route, and leaving it allowlisted
// would keep open a namespace nothing serves. See the matching history
// entry in `lib/api.ts` for the `AGENT_STUDIO_BASE` retarget.
const EXACT_ALLOWED_PATHS = ["health", "ready"];
const ALLOWED_PREFIXES = ["api/"];
const MAX_PROXY_BODY_BYTES = 21_000_000;

class PayloadTooLargeError extends Error {}

function isAllowedPath(joined: string): boolean {
  if (EXACT_ALLOWED_PATHS.includes(joined)) return true;
  return ALLOWED_PREFIXES.some((prefix) => joined.startsWith(prefix));
}

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

function resolveBackendUrl(path: string[], search: string): URL {
  const joined = path.join("/");
  if (joined.includes("..") || !isAllowedPath(joined)) {
    throw new Error("Backend route is not allowlisted");
  }
  const base = process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000";
  return new URL(`${joined}${search}`, `${base.replace(/\/$/, "")}/`);
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const requestId =
    request.headers.get("X-Request-ID") ?? crypto.randomUUID();
  try {
    const { path } = await context.params;
    const url = resolveBackendUrl(path, request.nextUrl.search);
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
    const projectId = request.headers.get("X-Research-Project-ID");
    const upstream = await fetch(url, {
      method: request.method,
      headers: {
        "Content-Type": request.headers.get("Content-Type") ?? "application/json",
        "X-Request-ID": requestId,
        ...(principal ? { "X-MS-CLIENT-PRINCIPAL": principal } : {}),
        ...(projectId ? { "X-Research-Project-ID": projectId } : {}),
      },
      body,
      duplex: "half",
      signal: AbortSignal.timeout(900_000),
      cache: "no-store",
    } as RequestInit & { duplex: "half" });
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("Content-Type") ?? "application/json",
        "Cache-Control":
          upstream.headers.get("Cache-Control") ?? "no-cache, no-transform",
        "X-Accel-Buffering": upstream.headers.get("X-Accel-Buffering") ?? "no",
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
