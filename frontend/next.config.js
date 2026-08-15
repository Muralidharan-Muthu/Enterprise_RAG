/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return {
      // `beforeFiles` is empty — nothing needs to be rewritten before
      // Next.js checks app-router API routes.
      beforeFiles: [],
      // `afterFiles` is empty — no rewrites needed after file checks.
      afterFiles: [],
      // `fallback` rewrites are checked ONLY when no page/API route
      // matches. /api/v1/query is handled by our custom API route
      // (src/app/api/v1/query/route.ts) so it never reaches fallback.
      // All other /api/* paths fall through here to the FastAPI backend.
      fallback: [
        {
          source: "/api/:path*",
          destination: `${process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000"}/api/:path*`,
        },
      ],
    };
  },
};

module.exports = nextConfig;
