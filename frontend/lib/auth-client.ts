import { authApiFetch } from "./api-client";

/**
 * Calls to the backend's `/api/v1/auth/*` routes. They go through the same plumbing as every other API call
 * (same-origin proxy, cookies, CSRF header) with `authRoute: true`: a 401 here is just "wrong email or password",
 * so it must not trigger the refresh-and-redirect used for an expired session.
 */

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  role: string;
  email_verified: boolean;
}

export interface ProviderInfo {
  id: "google" | "github" | "microsoft";
  name: string;
  enabled: boolean;
}

export function login(input: { email: string; password: string; rememberMe: boolean }): Promise<{ user: AuthUser }> {
  return authApiFetch("/api/v1/auth/login", {
    method: "POST",
    authRoute: true,
    body: { email: input.email.trim(), password: input.password, remember_me: input.rememberMe },
  });
}

export function signup(input: { email: string; name: string; password: string }): Promise<{ user: AuthUser }> {
  return authApiFetch("/api/v1/auth/signup", {
    method: "POST",
    authRoute: true,
    body: { email: input.email.trim(), name: input.name.trim(), password: input.password },
  });
}

export function requestPasswordReset(email: string): Promise<{ detail: string }> {
  return authApiFetch("/api/v1/auth/forgot-password", { method: "POST", authRoute: true, body: { email: email.trim() } });
}

export function resetPassword(input: { token: string; password: string }): Promise<{ detail: string }> {
  return authApiFetch("/api/v1/auth/reset-password", { method: "POST", authRoute: true, body: input });
}

export function verifyEmail(token: string): Promise<{ detail: string }> {
  return authApiFetch("/api/v1/auth/verify-email", { method: "POST", authRoute: true, body: { token } });
}

export function logout(): Promise<{ detail: string }> {
  return authApiFetch("/api/v1/auth/logout", { method: "POST", authRoute: true });
}

export async function listProviders(): Promise<ProviderInfo[]> {
  const res = await authApiFetch<{ providers: ProviderInfo[] }>("/api/v1/auth/providers", { authRoute: true });
  return res.providers;
}

/** Full-page navigation: the OAuth dance is a redirect chain, not an XHR. */
export function oauthStartUrl(provider: string, rememberMe: boolean): string {
  return `/api/v1/auth/oauth/${provider}/start${rememberMe ? "?remember=true" : ""}`;
}

export interface AuthConfig {
  /** Show "Sign up" / allow /signup. */
  signupEnabled: boolean;
  /** Zero users exist and a bootstrap token is configured: show the first-run /setup page. */
  setupRequired: boolean;
  /** Dev only (never in production): the sign-in form also accepts a username ("admin") instead of an email. */
  usernameLogin: boolean;
}

/**
 * What the sign-in pages need to know. Fails soft to the default state (sign-up offered, no setup) so a briefly
 * unreachable backend never changes how the login page looks; the backend enforces both rules regardless.
 */
export async function getAuthConfig(): Promise<AuthConfig> {
  try {
    const res = await authApiFetch<{
      signup_enabled: boolean;
      setup_required: boolean;
      username_login?: boolean;
    }>("/api/v1/auth/config", {
      authRoute: true,
    });
    return {
      signupEnabled: res.signup_enabled !== false,
      setupRequired: res.setup_required === true,
      usernameLogin: res.username_login === true,
    };
  } catch (err) {
    // Next's internal control-flow errors (dynamic rendering) must pass through untouched.
    if (err && typeof err === "object" && "digest" in err) throw err;
    return { signupEnabled: true, setupRequired: false, usernameLogin: false };
  }
}

/** First-run setup: creates the first administrator, authorised by the one-time BOOTSTRAP_TOKEN. */
export function registerFirstAdmin(input: {
  token: string;
  email: string;
  name: string;
  password: string;
}): Promise<{ email: string }> {
  return authApiFetch("/api/v1/auth/register-user", {
    method: "POST",
    authRoute: true,
    headers: { "X-Bootstrap-Token": input.token.trim() },
    body: { email: input.email.trim(), name: input.name.trim(), password: input.password },
  });
}
