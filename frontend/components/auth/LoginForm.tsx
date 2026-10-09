"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { ArrowRight, Lock, Mail } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { login } from "@/lib/auth-client";
import { safeNextPath, validateLoginIdentifier, validateLoginPassword } from "@/lib/auth-helpers";

import { AuthField } from "./AuthField";
import { SocialButtons } from "./SocialButtons";

export function LoginForm({
  next,
  notice,
  signupEnabled = true,
  usernameLogin = false,
}: {
  next?: string;
  notice?: string | null;
  signupEnabled?: boolean;
  /** Dev only: the server allows a username (e.g. "admin") in place of an email. */
  usernameLogin?: boolean;
}) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [badField, setBadField] = useState<"email" | "password" | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const emailProblem = validateLoginIdentifier(email, usernameLogin);
    const passwordProblem = validateLoginPassword(password);
    if (emailProblem || passwordProblem) {
      setError(emailProblem ?? passwordProblem);
      setBadField(emailProblem ? "email" : "password");
      (emailProblem ? emailRef : passwordRef).current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    setBadField(null);
    try {
      await login({ email, password, rememberMe: remember });
      router.replace(safeNextPath(next));
      router.refresh();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 401) {
        setError(usernameLogin ? "Incorrect email, username or password." : "Incorrect email or password.");
        setBadField("password");
        setPassword("");
        passwordRef.current?.focus();
      } else if (err instanceof ApiError && (err.status === 429 || err.status === 403)) {
        setError(err.message);
      } else {
        setError("We couldn’t sign you in right now. Please try again.");
      }
    }
  }

  const message = error ?? notice ?? null;
  return (
    <form onSubmit={onSubmit} noValidate className="auth-form">
      <p className="auth-status" role={error ? "alert" : "status"} data-tone={error ? "error" : "info"}>
        {message}
      </p>

      <AuthField
        id="login-email"
        label={usernameLogin ? "Email or username" : "Email address"}
        icon={Mail}
        type={usernameLogin ? "text" : "email"}
        inputRef={emailRef}
        autoComplete="username"
        inputMode={usernameLogin ? "text" : "email"}
        autoCapitalize="none"
        spellCheck={false}
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        invalid={badField === "email"}
      />
      <AuthField
        id="login-password"
        label="Password"
        icon={Lock}
        reveal
        inputRef={passwordRef}
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        invalid={badField === "password"}
      />

      <div className="auth-row">
        <label className="auth-check">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d="M5.5 12.5l4.2 4.2L18.5 7.6" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>Remember me</span>
        </label>
        <Link href="/forgot-password" className="auth-link">
          Forgot password?
        </Link>
      </div>

      <button type="submit" className="auth-submit" disabled={busy}>
        <span>{busy ? "Signing in…" : "Sign in"}</span>
        {!busy && <ArrowRight strokeWidth={2} aria-hidden />}
      </button>

      <SocialButtons rememberMe={remember} />

      {signupEnabled && (
        <p className="auth-switch">
          <span>Don’t have an account?</span>
          <Link href="/signup" className="auth-link-strong">
            Sign up
          </Link>
        </p>
      )}
    </form>
  );
}
