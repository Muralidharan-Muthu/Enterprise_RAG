import { NextRequest, NextResponse } from "next/server";

const BACKEND =
  process.env.NEXT_PUBLIC_API_URL ||
  "https://multi-store-rag-chatbot-production.up.railway.app";

export const dynamic = "force-dynamic";

async function handleProxy(req: NextRequest, context: { params: Promise<{ path: string[] }> | { path: string[] } }) {
  const params = await context.params;
  const path = params.path ? params.path.join("/") : "";
  const search = req.nextUrl.search;
  const targetUrl = `${BACKEND}/api/v1/${path}${search}`;

  const headers: Record<string, string> = {
    "Accept": "application/json",
  };
  req.headers.forEach((value, key) => {
    const k = key.toLowerCase();
    if (!["host", "connection", "content-length", "content-encoding"].includes(k)) {
      headers[key] = value;
    }
  });

  const isBodyless = req.method === "GET" || req.method === "HEAD";
  const body = isBodyless ? undefined : await req.arrayBuffer();

  try {
    const upstreamRes = await fetch(targetUrl, {
      method: req.method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
    });

    const contentType = upstreamRes.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      const data = await upstreamRes.json();
      return NextResponse.json(data, {
        status: upstreamRes.status,
        headers: {
          "Cache-Control": "no-store, max-age=0",
        },
      });
    }

    const data = await upstreamRes.arrayBuffer();
    const resHeaders = new Headers();
    upstreamRes.headers.forEach((val, key) => {
      if (!["content-encoding", "transfer-encoding"].includes(key.toLowerCase())) {
        resHeaders.set(key, val);
      }
    });

    return new NextResponse(data, {
      status: upstreamRes.status,
      statusText: upstreamRes.statusText,
      headers: resHeaders,
    });
  } catch (err: any) {
    return NextResponse.json(
      { error: "Backend proxy error", detail: err?.message || String(err) },
      { status: 502 }
    );
  }
}

export const GET = handleProxy;
export const POST = handleProxy;
export const PUT = handleProxy;
export const DELETE = handleProxy;
export const PATCH = handleProxy;
