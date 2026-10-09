import { NextResponse, type NextRequest } from "next/server";

import {
  browserOrigin,
  isExpired,
  isPublicPath,
  isSignedOutOnlyPath,
  jwtStringClaim,
  roleMayOpen,
  safeNextPath,
} from "@/lib/auth-helpers";

/**
 * Route protection + silent session renewal.
 *
 * - The homepage (/) and sign-in pages (/login, /signup, ...) are public. A visitor with a valid session (or one the
 *   refresh cookie can silently renew) never sees them: they are redirected to /chat (or a safe ?next= on /login).
 *   `?expired=1` means "this session is known dead": the cookies are cleared and the page is shown (this also
 *   breaks the loop for a cookie that looks valid but that the backend rejected).
 * - Every other page needs a session. The access token (15 min) lives in an httpOnly cookie; when it is missing or
 *   about to expire but the refresh cookie is present, the middleware renews the session BEFORE the page renders:
 *   it calls the backend's /auth/refresh with the visitor's cookies, forwards the new Set-Cookie headers to the
 *   browser and rewrites the request's own cookie header so the Server Components that run next already see the
 *   new tokens. If renewal fails the visitor is sent to /login?next=… and the dead cookies are cleared.
 * - The token's signature is NOT checked here (the middleware holds no secret): this is only a UX gate that decides
 *   "show the app or the login page". The backend verifies every API call, so a forged cookie opens nothing.
 *
 * `/api/*` is deliberately excluded (see `config.matcher`): it is proxied to the backend untouched.
 */

const ACCESS = "qs_access";
const REFRESH = "qs_refresh";
const CSRF = "qs_csrf";

function backendBase(): string {
  return process.env.INTERNAL_API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
}

function toLogin(req: NextRequest, expired: boolean): NextResponse {
  const url = req.nextUrl.clone();
  const next = req.nextUrl.pathname + req.nextUrl.search;
  url.pathname = "/login";
  url.search = "";
  if (next && next !== "/") url.searchParams.set("next", next);
  if (expired) url.searchParams.set("expired", "1");
  const res = NextResponse.redirect(url);
  for (const name of [ACCESS, REFRESH, CSRF]) res.cookies.delete(name);
  return res;
}

/** A signed-in user opened a page their role may not see (Users admin, Settings): back to the app. */
function toApp(req: NextRequest): NextResponse {
  const url = req.nextUrl.clone();
  url.pathname = "/chat";
  url.search = "";
  return NextResponse.redirect(url);
}

/** `name=value` pairs from Set-Cookie headers (attributes dropped), keyed by cookie name. */
function cookieUpdates(setCookies: string[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const sc of setCookies) {
    const first = sc.split(";", 1)[0];
    const eq = first.indexOf("=");
    if (eq > 0) out.set(first.slice(0, eq).trim(), first.slice(eq + 1));
  }
  return out;
}

type Renewal =
  | { kind: "renewed"; response: NextResponse; access: string | null }
  | { kind: "rejected" }
  | { kind: "unreachable" };

async function renewSession(req: NextRequest): Promise<Renewal> {
  const refresh = req.cookies.get(REFRESH)?.value;
  if (!refresh) return { kind: "rejected" };
  let res: Response;
  try {
    res = await fetch(`${backendBase()}/api/v1/auth/refresh`, {
      method: "POST",
      headers: {
        cookie: req.headers.get("cookie") ?? "",
        "x-csrf-token": req.cookies.get(CSRF)?.value ?? "",
        origin: browserOrigin(req.headers, req.nextUrl),
      },
      cache: "no-store",
    });
  } catch {
    return { kind: "unreachable" };
  }
  if (!res.ok) return { kind: "rejected" };

  const setCookies = res.headers.getSetCookie();
  const updates = cookieUpdates(setCookies);
  // Make the renewed tokens visible to this very request's Server Components.
  const merged = new Map<string, string>();
  for (const c of req.cookies.getAll()) merged.set(c.name, c.value);
  for (const [k, v] of updates) merged.set(k, v);
  const headers = new Headers(req.headers);
  headers.set("cookie", [...merged].map(([k, v]) => `${k}=${v}`).join("; "));
  const next = NextResponse.next({ request: { headers } });
  for (const sc of setCookies) next.headers.append("set-cookie", sc);
  return { kind: "renewed", response: next, access: merged.get(ACCESS) ?? null };
}

function clearSession(req: NextRequest, res: NextResponse): NextResponse {
  for (const name of [ACCESS, REFRESH, CSRF]) if (req.cookies.has(name)) res.cookies.delete(name);
  return res;
}

/** Homepage / sign-in pages: send people who are already signed in to the app, otherwise let the page render. */
async function signedOutOnly(req: NextRequest): Promise<NextResponse> {
  const { pathname, searchParams } = req.nextUrl;
  if (searchParams.get("expired")) return clearSession(req, NextResponse.next());

  const dest = req.nextUrl.clone();
  dest.search = "";
  const to = pathname === "/login" ? safeNextPath(searchParams.get("next")) : "/chat";
  const q = to.indexOf("?");
  dest.pathname = q >= 0 ? to.slice(0, q) : to;
  dest.search = q >= 0 ? to.slice(q) : "";

  const access = req.cookies.get(ACCESS)?.value;
  if (access && !isExpired(access)) return NextResponse.redirect(dest);
  if (!req.cookies.get(REFRESH)?.value) return clearSession(req, NextResponse.next());

  const renewed = await renewSession(req);
  if (renewed.kind === "renewed") {
    const res = NextResponse.redirect(dest);
    for (const sc of renewed.response.headers.getSetCookie()) res.headers.append("set-cookie", sc);
    return res;
  }
  if (renewed.kind === "unreachable") return NextResponse.next();
  return clearSession(req, NextResponse.next());
}

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (isSignedOutOnlyPath(pathname)) return signedOutOnly(req);
  if (isPublicPath(pathname)) return NextResponse.next();

  const access = req.cookies.get(ACCESS)?.value;
  if (access && !isExpired(access)) {
    return roleMayOpen(pathname, jwtStringClaim(access, "rol")) ? NextResponse.next() : toApp(req);
  }

  const renewed = await renewSession(req);
  if (renewed.kind === "renewed") {
    if (roleMayOpen(pathname, jwtStringClaim(renewed.access, "rol"))) return renewed.response;
    const away = toApp(req);
    for (const sc of renewed.response.headers.getSetCookie()) away.headers.append("set-cookie", sc);
    return away;
  }
  // Backend unreachable: keep the cookies and let the page render its own "could not reach the backend" error
  // instead of signing the visitor out over a blip.
  if (renewed.kind === "unreachable") return NextResponse.next();
  const hadSession = Boolean(access || req.cookies.get(REFRESH)?.value);
  return toLogin(req, hadSession);
}

export const config = {
  // Skip the API proxy, Next internals and static files (anything with an extension).
  matcher: ["/((?!api/|_next/|favicon.ico|.*\\.[a-zA-Z0-9]+$).*)"],
};
