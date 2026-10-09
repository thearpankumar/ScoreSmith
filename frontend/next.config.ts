import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV !== "production";

// Where the Next.js server reaches the backend: the Compose service name under docker-compose, localhost for
// `npm run dev` on the host. Used by the /api/v1/* proxy below, the middleware and Server Components.
const BACKEND = process.env.INTERNAL_API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

/**
 * Content-Security-Policy for the web app. Next.js injects small inline bootstrap scripts, so `script-src` needs
 * 'unsafe-inline' (a nonce-based policy would force every page dynamic); `'unsafe-eval'` is only added for the dev
 * server's React refresh. `connect-src` allows the browser's direct multipart uploads to S3 presigned URLs.
 */
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self' https://*.amazonaws.com${isDev ? " ws: wss:" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  ...(isDev ? [] : [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]),
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    // Same-origin API: the browser calls /api/v1/*, Next forwards it to the backend. Session cookies are therefore
    // first-party (no CORS, SameSite=Lax just works) and the backend URL never reaches the client bundle.
    return [{ source: "/api/v1/:path*", destination: `${BACKEND}/api/v1/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
