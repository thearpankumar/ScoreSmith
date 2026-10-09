import type { ReactNode } from "react";

/** The frosted-glass card: heading, a one-line subtitle, and the form / message body. */
export function AuthCard({
  title,
  subtitle,
  children,
  variant = "login",
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
  variant?: "login" | "signup" | "simple";
}) {
  return (
    <main className={`auth-card auth-card-${variant}`} aria-labelledby="auth-title">
      <h2 id="auth-title" className="auth-card-title">
        {title}
      </h2>
      <p className="auth-card-subtitle">{subtitle}</p>
      {children}
    </main>
  );
}
