import type { NextConfig } from "next";

// Proxy /api/* to the FastAPI backend so the browser talks same-origin in dev.
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Pin the workspace root — this project sits inside a repo with other
  // lockfiles, so Turbopack's auto-inference would otherwise warn.
  turbopack: {
    root: __dirname,
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
