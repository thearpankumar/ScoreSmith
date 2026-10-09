import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { AuthCard } from "@/components/auth/AuthCard";
import { SignupForm } from "@/components/auth/SignupForm";
import { getAuthConfig } from "@/lib/auth-client";

export const metadata: Metadata = { title: "Create account · Score Smith" };

export default async function SignupPage() {
  const config = await getAuthConfig();
  if (config.setupRequired) redirect("/setup");
  if (!config.signupEnabled) redirect("/login");
  return (
    <AuthCard title="Create your account" subtitle="Start designing and evaluating scorecards" variant="signup">
      <SignupForm />
    </AuthCard>
  );
}
