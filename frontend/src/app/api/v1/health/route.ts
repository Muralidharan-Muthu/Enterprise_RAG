import { NextRequest, NextResponse } from "next/server";

const BACKEND =
  process.env.NEXT_PUBLIC_API_URL ||
  "https://multi-store-rag-chatbot-production.up.railway.app";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  try {
    const upstreamRes = await fetch(`${BACKEND}/api/v1/health`, {
      method: "GET",
      headers: {
        "Accept": "application/json",
      },
      cache: "no-store",
    });

    const data = await upstreamRes.json();
    return NextResponse.json(data, {
      status: upstreamRes.status,
      headers: {
        "Cache-Control": "no-store, max-age=0",
      },
    });
  } catch (err: any) {
    return NextResponse.json(
      {
        status: "unhealthy",
        api: "unreachable",
        error: "Failed to connect to Railway backend",
        detail: err?.message || String(err),
      },
      { status: 502 }
    );
  }
}
