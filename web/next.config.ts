import type { NextConfig } from "next";

// Proxy /api/* to the FastAPI backend so the browser talks same-origin in dev.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Pin the workspace root — this project sits inside a repo with other
  // lockfiles, so Turbopack's auto-inference would otherwise warn.
  turbopack: {
    root: __dirname,
  },
  experimental: {
    // The /api/* rewrite proxies to FastAPI; a cold backtest study can take ~25s,
    // and the default 30s proxy timeout 500s the dashboard when it does. The engine
    // caches those studies (and a scheduler job warms them), so this is only the
    // safety margin for the first uncached compute after new data lands.
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_BASE}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
