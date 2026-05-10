// Bypass system HTTP proxy for localhost API rewrites
if (!process.env.NO_PROXY) {
  process.env.NO_PROXY = "localhost,127.0.0.1";
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Static export is not compatible with dynamic routes like /workflows/[id].
  // Enable only for EXE packaging: set MAS_STATIC_EXPORT=1 before running next build.
  ...(process.env.MAS_STATIC_EXPORT ? { output: "export", trailingSlash: true } : {}),
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
