import { NextRequest, NextResponse } from "next/server";

const BACKEND = process.env.NEXT_PUBLIC_API_URL || "https://muralidharan007-multi-store-rag-backend.hf.space";

async function proxy(req: NextRequest, pathSegments?: string[]) {
  const subpath = pathSegments && pathSegments.length > 0 ? `/${pathSegments.join("/")}` : "";
  const search = req.nextUrl.search; // preserves ?limit=30 etc.
  const url = `${BACKEND}/api/v1/chats${subpath}${search}`;

  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
  }

  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const auth = req.headers.get("authorization");
  if (auth) headers["authorization"] = auth;

  try {
    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body,
      // See the ingest proxy for why: Next's server-side fetch() defaults to
      // caching GET requests, which would stale-lock chat history polling.
      cache: "no-store",
    });
    const data = await upstream.text();
    return new NextResponse(data, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "Backend unreachable";
    console.error("[chats proxy]", req.method, url, "→", message);
    return NextResponse.json({ detail: `Chats proxy error: ${message}` }, { status: 502 });
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
