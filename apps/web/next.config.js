// Bypass system HTTP proxy for localhost API rewrites
if (!process.env.NO_PROXY) {
  process.env.NO_PROXY = "localhost,127.0.0.1";
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const backend = process.env.BACKEND_URL || "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
    ];
  },
};

module.exports = nextConfig;
