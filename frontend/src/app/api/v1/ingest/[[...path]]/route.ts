import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

// Uploads can be large (PDFs) and slow (Supabase Storage upload + DB insert).
// The default Next.js rewrite proxy uses Node's default HTTP agent which drops
// long-running / large multipart connections with ECONNRESET / "socket hang
// up", surfacing to the browser as an intermittent 502. We proxy ingest calls
// ourselves with a generous timeout and faithful header/body forwarding.
const TIMEOUT_MS = 300_000; // 5 min — covers big uploads on a slow link

async function proxy(req: NextRequest, pathSegments?: string[]) {
  const subpath =
    pathSegments && pathSegments.length > 0 ? `/${pathSegments.join("/")}` : "";
  const search = req.nextUrl.search; // preserves ?page=1&limit=20 etc.
  const url = `${BACKEND}/api/v1/ingest${subpath}${search}`;

  // Forward the raw body bytes for non-GET. For multipart uploads we MUST keep
  // the original Content-Type (it carries the boundary) and not coerce to JSON.
  const isBodyless = req.method === "GET" || req.method === "HEAD";
  const body = isBodyless ? undefined : await req.arrayBuffer();

  const headers: Record<string, string> = {};
  const ct = req.headers.get("content-type");
  if (ct) headers["content-type"] = ct;
  const auth = req.headers.get("authorization");
  if (auth) headers["authorization"] = auth;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      signal: controller.signal,
      // This proxies live status polling (pipelines/{run_id}, status/{job_id}).
      // Next's server-side fetch() defaults to caching GET requests; without
      // this, repeated polls to the same URL during a run can be served a
      // stale cached body instead of reaching the backend.
      cache: "no-store",
    });
    clearTimeout(timer);

    const data = await upstream.text();
    return new NextResponse(data, {
      status: upstream.status,
      headers: {
        "Content-Type":
          upstream.headers.get("content-type") || "application/json",
      },
    });
  } catch (err: unknown) {
    clearTimeout(timer);
    const message = err instanceof Error ? err.message : "Backend unreachable";
    console.error("[ingest proxy]", req.method, url, "→", message);
    return NextResponse.json(
      { detail: `Ingest proxy error: ${message}` },
      { status: 502 }
    );
  }
}

export async function GET(req: NextRequest, { params }: { params: { path?: string[] } }) {
  return proxy(req, params.path);
}

export async function POST(req: NextRequest, { params }: { params: { path?: string[] } }) {
  return proxy(req, params.path);
}

export async function PATCH(req: NextRequest, { params }: { params: { path?: string[] } }) {
  return proxy(req, params.path);
}

export async function DELETE(req: NextRequest, { params }: { params: { path?: string[] } }) {
  return proxy(req, params.path);
}

// Next.js App Router buffers request bodies by default; raise the limit so
// large PDFs are not rejected before they reach this handler.
export const maxDuration = 300;
