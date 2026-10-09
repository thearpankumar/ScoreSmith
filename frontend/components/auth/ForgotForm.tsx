"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, Mail } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { requestPasswordReset } from "@/lib/auth-client";
import { validateEmail } from "@/lib/auth-helpers";

import { AuthField } from "./AuthField";

export function ForgotForm() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const problem = validateEmail(email);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await requestPasswordReset(email);
      setSent(true);
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 429 ? err.message : "We couldn’t send that right now. Please try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <div className="auth-form">
        <p className="auth-message" role="status">
          If an account exists for <strong>{email.trim()}</strong>, a link to reset the password is on its way. It works once
          and expires in an hour.
        </p>
        <Link href="/login" className="auth-submit auth-submit-link">
          <ArrowLeft strokeWidth={2} aria-hidden />
          <span>Back to sign in</span>
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
        id="forgot-email"
        label="Email address"
        icon={Mail}
        type="email"
        autoComplete="email"
        inputMode="email"
        autoCapitalize="none"
        spellCheck={false}
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        invalid={Boolean(error)}
      />
      <button type="submit" className="auth-submit" disabled={busy}>
        <span>{busy ? "Sending…" : "Send reset link"}</span>
        {!busy && <ArrowRight strokeWidth={2} aria-hidden />}
      </button>
      <p className="auth-switch">
        <Link href="/login" className="auth-link-strong">
          Back to sign in
        </Link>
      </p>
    </form>
  );
}
