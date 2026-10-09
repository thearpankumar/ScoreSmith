"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { ArrowRight, Lock } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { resetPassword } from "@/lib/auth-client";
import { passwordStrength, validateNewPassword } from "@/lib/auth-helpers";

import { AuthField } from "./AuthField";

export function ResetForm({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const strength = passwordStrength(password);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const problem = validateNewPassword(password) ?? (password !== confirm ? "The two passwords don’t match." : null);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await resetPassword({ token, password });
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "We couldn’t update the password right now. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <div className="auth-form">
        <p className="auth-message" role="alert">
          This reset link is incomplete. Request a new one.
        </p>
        <Link href="/forgot-password" className="auth-submit auth-submit-link">
          <span>Request a new link</span>
          <ArrowRight strokeWidth={2} aria-hidden />
        </Link>
      </div>
    );
  }
  if (done) {
    return (
      <div className="auth-form">
        <p className="auth-message" role="status">
          Your password has been updated and every other session was signed out.
        </p>
        <Link href="/login" className="auth-submit auth-submit-link">
          <span>Sign in</span>
          <ArrowRight strokeWidth={2} aria-hidden />
        </Link>
      </div>
    );
  }
  return (
    <form onSubmit={onSubmit} noValidate className="auth-form">
      <p className="auth-status" role={error ? "alert" : "status"} data-tone="error">
        {error}
      </p>
      <AuthField
        id="reset-password"
        label="New password (12+ characters)"
        icon={Lock}
        reveal
        autoComplete="new-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        invalid={Boolean(error) && !password}
      />
      <AuthField
        id="reset-confirm"
        label="Repeat new password"
        icon={Lock}
        reveal
        autoComplete="new-password"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
        invalid={Boolean(error) && password !== confirm}
      />
      <div className="auth-meter" aria-live="polite">
        <div className="auth-meter-bars" data-score={strength.score} aria-hidden="true">
          <i /> <i /> <i /> <i />
        </div>
        <span>{password ? `Password strength: ${strength.label}` : "Use a long passphrase; length matters most."}</span>
      </div>
      <button type="submit" className="auth-submit" disabled={busy}>
        <span>{busy ? "Updating…" : "Update password"}</span>
        {!busy && <ArrowRight strokeWidth={2} aria-hidden />}
      </button>
    </form>
  );
}
