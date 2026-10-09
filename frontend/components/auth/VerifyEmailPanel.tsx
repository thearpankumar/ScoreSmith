"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { verifyEmail } from "@/lib/auth-client";

/** Confirms the e-mail address the visitor was sent a one-time link for. */
export function VerifyEmailPanel({ token }: { token: string }) {
  const [state, setState] = useState<"working" | "ok" | "failed">(token ? "working" : "failed");
  const [detail, setDetail] = useState<string>("");

  useEffect(() => {
    if (!token) return;
    let alive = true;
    verifyEmail(token)
      .then(() => alive && setState("ok"))
      .catch((err) => {
        if (!alive) return;
        setState("failed");
        setDetail(err instanceof ApiError ? err.message : "");
      });
    return () => {
      alive = false;
    };
  }, [token]);

  return (
    <div className="auth-form">
      <p className="auth-message" role={state === "failed" ? "alert" : "status"}>
        {state === "working" && "Confirming your email address…"}
        {state === "ok" && "Your email address is confirmed. Thank you!"}
        {state === "failed" && (detail || "This verification link is invalid or has expired.")}
      </p>
      <Link href={state === "ok" ? "/chat" : "/login"} className="auth-submit auth-submit-link">
        <span>{state === "ok" ? "Continue" : "Go to sign in"}</span>
        <ArrowRight strokeWidth={2} aria-hidden />
      </Link>
    </div>
  );
}
