import { NextRequest, NextResponse } from "next/server";

const BACKEND =
  process.env.NEXT_PUBLIC_API_URL ||
  "https://multi-store-rag-chatbot-production.up.railway.app";

export const dynamic = "force-dynamic";

async function handleProxy(req: NextRequest, { params }: { params: { path: string[] } }) {
  const path = params.path ? params.path.join("/") : "";
  const search = req.nextUrl.search;
  const targetUrl = `${BACKEND}/api/${path}${search}`;

  const headers: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (!["host", "connection", "content-length"].includes(key.toLowerCase())) {
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
    });

    const resHeaders = new Headers();
    upstreamRes.headers.forEach((val, key) => {
      resHeaders.set(key, val);
    });

    return new NextResponse(upstreamRes.body, {
      status: upstreamRes.status,
      statusText: upstreamRes.statusText,
      headers: resHeaders,
    });
  } catch (err: any) {
    return NextResponse.json(
      { error: "Backend connection error", detail: err?.message || String(err) },
      { status: 502 }
    );
  }
}

export const GET = handleProxy;
export const POST = handleProxy;
export const PUT = handleProxy;
export const DELETE = handleProxy;
export const PATCH = handleProxy;
