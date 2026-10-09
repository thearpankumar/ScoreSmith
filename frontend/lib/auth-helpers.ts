/**
 * Pure helpers for the sign-in pages and the route-protection middleware (no React, no Next imports, so they
 * run under `node --test` — see tests/auth-helpers.test.mjs).
 *
 * The password rules mirror the backend (`backend/app/auth/security.py::password_problem`): the server is the
 * source of truth and re-checks everything; these only give instant feedback.
 */

export const PASSWORD_MIN_LENGTH = 12;
export const PASSWORD_MAX_LENGTH = 128;

/** Pages a signed-out visitor may open (plus the exact path "/", the marketing homepage). Everything else redirects to /login. */
export const PUBLIC_PATHS = ["/login", "/signup", "/setup", "/forgot-password", "/reset-password", "/verify-email"] as const;

/** Public pages that a visitor WITH a valid session is sent past (to /chat): the homepage and the sign-in forms. */
export const SIGNED_OUT_ONLY_PATHS = ["/", "/login", "/signup", "/setup", "/forgot-password", "/reset-password"] as const;

export function isSignedOutOnlyPath(pathname: string): boolean {
  return SIGNED_OUT_ONLY_PATHS.some((p) => pathname === p || (p !== "/" && pathname.startsWith(`${p}/`)));
}

export function isPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

/** `null` when fine, otherwise a short message for the field. */
export function validateEmail(value: string): string | null {
  const v = value.trim();
  if (!v) return "Enter your email address.";
  if (v.length > 320 || !EMAIL_RE.test(v)) return "Enter a valid email address.";
  return null;
}

/**
 * Sign-in identifier. Always an email, except when the server says the dev username shortcut is on
 * (`usernameLogin`, never in production): then any non-empty value without "@" is accepted and the server decides.
 */
export function validateLoginIdentifier(value: string, usernameLogin: boolean): string | null {
  const v = value.trim();
  if (usernameLogin) {
    if (!v) return "Enter your email address or username.";
    if (v.length > 320) return "That is too long to be an email address or username.";
    return v.includes("@") ? validateEmail(v) : null;
  }
  return validateEmail(v);
}

/** Sign-in only needs a non-empty password (the length rules apply when one is chosen). */
export function validateLoginPassword(value: string): string | null {
  return value ? null : "Enter your password.";
}

const COMMON = new Set([
  "password1234",
  "password12345",
  "123456789012",
  "qwertyuiop12",
  "letmein12345",
  "administrator",
  "iloveyou1234",
  "welcome12345",
  "changeme1234",
  "123456789abc",
  "qwerty123456",
  "1q2w3e4r5t6y",
]);

/** Rules for choosing a new password (signup / reset). */
export function validateNewPassword(value: string, email?: string): string | null {
  if (value.length < PASSWORD_MIN_LENGTH) return `Use at least ${PASSWORD_MIN_LENGTH} characters.`;
  if (value.length > PASSWORD_MAX_LENGTH) return `Use at most ${PASSWORD_MAX_LENGTH} characters.`;
  const low = value.toLowerCase();
  if (COMMON.has(low) || new Set(low).size < 5) return "That password is too easy to guess.";
  if (email && (low === email.trim().toLowerCase() || low === email.trim().toLowerCase().split("@")[0])) {
    return "The password must not be your email address.";
  }
  return null;
}

export function validateName(value: string): string | null {
  const v = value.trim();
  if (!v) return "Enter your name.";
  if (v.length > 200) return "That name is too long.";
  return null;
}

export type PasswordStrength = { score: 0 | 1 | 2 | 3 | 4; label: string };

/** A rough 0-4 meter: length first, then variety. Advisory only. */
export function passwordStrength(value: string): PasswordStrength {
  if (!value) return { score: 0, label: "" };
  let score = 0;
  if (value.length >= PASSWORD_MIN_LENGTH) score++;
  if (value.length >= 16) score++;
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(value)).length;
  if (classes >= 3) score++;
  if (value.length >= 20 || (classes === 4 && value.length >= 14)) score++;
  if (validateNewPassword(value) !== null) score = Math.min(score, 1) as 0 | 1;
  const s = Math.min(score, 4) as 0 | 1 | 2 | 3 | 4;
  return { score: s, label: ["Too short", "Weak", "Fair", "Good", "Strong"][s] };
}

/**
 * Where to go after signing in. Only same-site absolute paths are allowed (`/charts/…`): anything else - a full URL,
 * `//evil.example`, `/\evil.example`, a `javascript:` URI - falls back, so the `?next=` parameter cannot be used as an
 * open redirect.
 */
export function safeNextPath(raw: string | null | undefined, fallback = "/chat"): string {
  if (!raw) return fallback;
  let v = raw;
  try {
    v = decodeURIComponent(raw);
  } catch {
    return fallback;
  }
  if (!v.startsWith("/") || v.startsWith("//") || v.includes("\\") || /[\u0000-\u001f]/.test(v)) return fallback;
  if (isPublicPath(v.split(/[?#]/)[0])) return fallback; // never bounce back into the sign-in pages
  return v;
}

/** `exp` (seconds since the epoch) of a JWT, or null. Does NOT verify the signature: the backend does that. */
export function jwtExpiry(token: string | undefined | null): number | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("utf8");
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp === "number" ? exp : null;
  } catch {
    return null;
  }
}

/** A string claim of a JWT payload (NOT verified: only for UX gating, the backend re-checks everything). */
export function jwtStringClaim(token: string | undefined | null, name: string): string | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const b64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = typeof atob === "function" ? atob(b64) : Buffer.from(b64, "base64").toString("utf8");
    const value = (JSON.parse(json) as Record<string, unknown>)[name];
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}

/** Pages only administrators may open (Users admin, Settings). Everything under them is covered. */
export const ADMIN_ONLY_PATHS = ["/admin", "/settings"] as const;

export function isAdminOnlyPath(pathname: string): boolean {
  return ADMIN_ONLY_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/**
 * Whether `role` (the `rol` claim, null when the token predates role claims) may open `pathname`. An unknown role is
 * allowed through here on purpose: the page itself and the API re-check the role authoritatively.
 */
export function roleMayOpen(pathname: string, role: string | null | undefined): boolean {
  if (!isAdminOnlyPath(pathname)) return true;
  return role == null || role === "admin";
}

/** True when the token is missing, malformed, or expires within `skewSeconds`. */
export function isExpired(token: string | undefined | null, nowMs: number = Date.now(), skewSeconds = 10): boolean {
  const exp = jwtExpiry(token);
  return exp === null || exp * 1000 <= nowMs + skewSeconds * 1000;
}

/** Friendly text for the `?error=` codes the OAuth callback redirects back with. */
export function oauthErrorMessage(code: string | null | undefined): string | null {
  switch (code) {
    case null:
    case undefined:
    case "":
      return null;
    case "oauth_denied":
      return "Sign-in was cancelled.";
    case "oauth_state":
      return "That sign-in attempt expired. Please try again.";
    case "oauth_no_email":
      return "That account did not share a verified email address.";
    case "oauth_account_exists":
      return "An account with that email already exists. Sign in with your password, then connect the provider.";
    case "oauth_signup_disabled":
      return "Sign-up is disabled. Ask an administrator to create your account.";
    case "oauth_disabled":
      return "This account has been deactivated.";
    default:
      return "Could not sign in with that provider. Please try again.";
  }
}

/**
 * The origin the BROWSER used (what the backend's CSRF Origin check compares against). `nextUrl.origin` is not it:
 * behind `next dev -H 0.0.0.0` / a container it is "http://0.0.0.0:3000", which the backend rejects with 403 and
 * silently signs the visitor out when the access token expires.
 */
export function browserOrigin(
  headers: { get(name: string): string | null },
  nextUrl: { host: string; protocol: string },
): string {
  const host = headers.get("x-forwarded-host") ?? headers.get("host") ?? nextUrl.host;
  const proto = (headers.get("x-forwarded-proto") ?? nextUrl.protocol.replace(":", "")).split(",")[0].trim();
  return `${proto}://${host}`;
}
