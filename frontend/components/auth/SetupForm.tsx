"use client";

import { useRouter } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { ArrowRight, KeyRound, Lock, Mail, User } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { registerFirstAdmin } from "@/lib/auth-client";
import { validateEmail, validateName, validateNewPassword } from "@/lib/auth-helpers";

import { AuthField } from "./AuthField";

type FieldName = "token" | "name" | "email" | "password";

/** First-run setup (production): the BOOTSTRAP_TOKEN from the server's environment creates the first admin. */
export function SetupForm() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bad, setBad] = useState<FieldName | null>(null);
  const refs = {
    token: useRef<HTMLInputElement>(null),
    name: useRef<HTMLInputElement>(null),
    email: useRef<HTMLInputElement>(null),
    password: useRef<HTMLInputElement>(null),
  };

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const problems: [FieldName, string | null][] = [
      ["token", token.trim() ? null : "Enter the setup token."],
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
      await registerFirstAdmin({ token, email, name, password });
      router.replace("/login?setup=done");
      router.refresh();
    } catch (err) {
      setBusy(false);
      if (err instanceof ApiError && err.status === 403) {
        setError("That setup token is not correct.");
        setBad("token");
      } else if (err instanceof ApiError && err.status === 404) {
        router.replace("/login"); // setup was already completed
      } else if (err instanceof ApiError && (err.status === 422 || err.status === 429 || err.status === 409)) {
        setError(err.message);
        setBad(err.status === 422 ? "password" : null);
      } else {
        setError("We couldn’t finish the setup right now. Please try again.");
      }
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="auth-form">
      <p className="auth-status" role={error ? "alert" : "status"} data-tone="error">
        {error}
      </p>
      <AuthField
        id="setup-token"
        label="Setup token"
        icon={KeyRound}
        reveal
        inputRef={refs.token}
        autoComplete="off"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        invalid={bad === "token"}
      />
      <AuthField
        id="setup-name"
        label="Full name"
        icon={User}
        inputRef={refs.name}
        autoComplete="name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        invalid={bad === "name"}
      />
      <AuthField
        id="setup-email"
        label="Email address"
        icon={Mail}
        type="email"
        inputRef={refs.email}
        autoComplete="email"
        inputMode="email"
        autoCapitalize="none"
        spellCheck={false}
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        invalid={bad === "email"}
      />
      <AuthField
        id="setup-password"
        label="Password (14+ characters)"
        icon={Lock}
        reveal
        inputRef={refs.password}
        autoComplete="new-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        invalid={bad === "password"}
      />
      <button type="submit" className="auth-submit" disabled={busy}>
        <span>{busy ? "Creating administrator…" : "Create administrator"}</span>
        {!busy && <ArrowRight strokeWidth={2} aria-hidden />}
      </button>
    </form>
  );
}
