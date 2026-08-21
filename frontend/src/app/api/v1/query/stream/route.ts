import { NextRequest } from "next/server";

const BACKEND =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

/**
 * Streaming SSE proxy for /api/v1/query/stream.
 *
 * Unlike the buffering query proxy, this route pipes the upstream
 * ReadableStream directly to the client without ever calling .text() or
 * .json() — doing so would buffer the entire body and defeat streaming.
 *
 * IMPORTANT: The backend's FastAPI StreamingResponse does NOT send HTTP
 * headers until the async generator yields its first byte. The full
 * retrieve → graph-expand → rerank pipeline runs *before* the first yield,
 * so fetch() here will block until all that work is done (≈15-60 s depending
 * on graph expansion and LLM queue depth).
 *
 * The AbortController timeout must therefore cover the entire pre-stream
 * work, not just a TCP connection handshake.  We use 180 s (3× the backend's
 * GROQ_TIMEOUT_SECONDS=120) so even a slow retrieval + queued LLM call
 * never hits this deadline under normal conditions.
 */
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const body = await req.text();

  const controller = new AbortController();
  // 180 s — covers retrieval (≤30 s) + LLM synthesis (≤120 s) with margin.
  const timer = setTimeout(() => controller.abort(), 180_000);

  try {
    const upstream = await fetch(`${BACKEND}/api/v1/query/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Keep-alive so Node reuses the TCP connection and doesn't reset it
        // while the backend is doing retrieval before sending the first byte.
        Connection: "keep-alive",
      },
      body,
      signal: controller.signal,
      keepalive: true,
    });

    clearTimeout(timer); // HTTP headers received — stream flows on its own schedule

    if (!upstream.ok || !upstream.body) {
      const text = await upstream.text();
      return new Response(text, { status: upstream.status });
    }

    return new Response(upstream.body, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (err: unknown) {
    clearTimeout(timer);
    const message = err instanceof Error ? err.message : "Backend unreachable";
    console.error("[query/stream proxy]", message);
    return Response.json(
      { detail: `Stream proxy error: ${message}` },
      { status: 502 }
    );
  }
}
