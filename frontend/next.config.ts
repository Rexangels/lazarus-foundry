import type { NextConfig } from "next";

/**
 * Optional same-origin proxy to the Django API.
 *
 * The backend sends no CORS headers, so a browser on http://localhost:3000 cannot read
 * responses from http://localhost:8000 directly. Setting NEXT_PUBLIC_API_BASE_URL=/api makes
 * the browser call this Next.js server, which forwards /api/* to LAZARUS_BACKEND_ORIGIN.
 * WebSockets are not proxied; they go straight to NEXT_PUBLIC_WS_BASE_URL (Channels checks the
 * Origin host against ALLOWED_HOSTS, and "localhost" is allowed by default).
 */
const backendOrigin = (process.env.LAZARUS_BACKEND_ORIGIN ?? "http://localhost:8000").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backendOrigin}/api/:path*` }];
  },
};

export default nextConfig;
