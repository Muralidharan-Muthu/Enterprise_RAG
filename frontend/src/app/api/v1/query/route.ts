import { NextRequest, NextResponse } from "next/server";

const BACKEND =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

/**
 * Custom proxy for /api/v1/query that sets a generous timeout.
 *
 * The default Next.js rewrite proxy uses Node's default HTTP agent which
 * can drop long-running connections (~12-20 s for retrieve + rerank +
 * synthesise) with ECONNRESET / "socket hang up".  By handling the proxy
 * ourselves we control the AbortController timeout (120 s here, matching
 * the backend's GEMMA4_TIMEOUT_SECONDS).
 */
export async function POST(req: NextRequest) {
  const body = await req.text();

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120_000); // 120 s

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const auth = req.headers.get("authorization");
  if (auth) headers["authorization"] = auth;

  try {
    const upstream = await fetch(`${BACKEND}/api/v1/query`, {
      method: "POST",
      headers,
      body,
      signal: controller.signal,
    });

    clearTimeout(timer);

    const data = await upstream.text();
    return new NextResponse(data, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: unknown) {
    clearTimeout(timer);
    const message =
      err instanceof Error ? err.message : "Backend unreachable";
    console.error("[query proxy]", message);
    return NextResponse.json(
      { detail: `Query proxy error: ${message}` },
      { status: 502 }
    );
  }
}
