// Bypass system HTTP proxy for localhost API rewrites
if (!process.env.NO_PROXY) {
  process.env.NO_PROXY = "localhost,127.0.0.1";
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Enable static HTML export so the build can be served by the Python backend.
  // When set, `next build` writes to apps/web/out/ instead of apps/web/.next/.
  output: "export",
  // Suppress trailing-slash warnings during static export (all routes get index.html).
  trailingSlash: true,
  // Rewrites are ignored in static-export mode; the launcher serves the API
  // directly from FastAPI, so the /api/* routes are handled by the backend.
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
    ];
  },
};

module.exports = nextConfig;
