"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { ArrowRight, Lock, Mail, User } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { signup } from "@/lib/auth-client";
import { passwordStrength, validateEmail, validateName, validateNewPassword } from "@/lib/auth-helpers";

import { AuthField } from "./AuthField";
import { SocialButtons } from "./SocialButtons";

type FieldName = "name" | "email" | "password";

export function SignupForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bad, setBad] = useState<FieldName | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const refs = { name: nameRef, email: emailRef, password: passwordRef };
  const strength = passwordStrength(password);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const problems: [FieldName, string | null][] = [
      ["name", validateName(name)],
      ["email", validateEmail(email)],
      ["password", validateNewPassword(password, email)],
    ];
    const first = problems.find(([, p]) => p);
    if (first) {
      setError(first[1]);
      setBad(first[0]);
      refs[first[0]].current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    setBad(null);
    try {
      await signup({ email, name, password });
      router.replace("/chat");
      router.refresh();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 409) {
        setError(err.message);
        setBad("email");
      } else if (err instanceof ApiError && (err.status === 422 || err.status === 429)) {
        setError(err.message);
        setBad(err.status === 422 ? "password" : null);
      } else {
        setError("We couldn’t create your account right now. Please try again.");
      }
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="auth-form">
      <p className="auth-status" role={error ? "alert" : "status"} data-tone="error">
        {error}
      </p>
      <AuthField
        id="signup-name"
        label="Full name"
        icon={User}
        inputRef={nameRef}
        autoComplete="name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        invalid={bad === "name"}
      />
      <AuthField
        id="signup-email"
        label="Email address"
        icon={Mail}
        type="email"
        inputRef={emailRef}
        autoComplete="email"
        inputMode="email"
        autoCapitalize="none"
        spellCheck={false}
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        invalid={bad === "email"}
      />
      <AuthField
        id="signup-password"
        label="Password (12+ characters)"
        icon={Lock}
        reveal
        inputRef={passwordRef}
        autoComplete="new-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        invalid={bad === "password"}
      />
      <div className="auth-meter" aria-live="polite">
        <div className="auth-meter-bars" data-score={strength.score} aria-hidden="true">
          <i /> <i /> <i /> <i />
        </div>
        <span>{password ? `Password strength: ${strength.label}` : "Use a long passphrase; length matters most."}</span>
      </div>

      <button type="submit" className="auth-submit" disabled={busy}>
        <span>{busy ? "Creating account…" : "Create account"}</span>
        {!busy && <ArrowRight strokeWidth={2} aria-hidden />}
      </button>

      <SocialButtons rememberMe={false} />

      <p className="auth-switch">
        <span>Already have an account?</span>
        <Link href="/login" className="auth-link-strong">
          Sign in
        </Link>
      </p>
    </form>
  );
}
