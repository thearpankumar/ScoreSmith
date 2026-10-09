import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { AuthCard } from "@/components/auth/AuthCard";
import { SetupForm } from "@/components/auth/SetupForm";
import { getAuthConfig } from "@/lib/auth-client";

export const metadata: Metadata = { title: "First-run setup · KPI Metrics" };

export default async function SetupPage() {
  const config = await getAuthConfig();
  // Closed for good as soon as any user exists (the backend enforces this too).
  if (!config.setupRequired) redirect("/login");
  return (
    <AuthCard title="Create the administrator" subtitle="One-time setup for this installation" variant="signup">
      <SetupForm />
    </AuthCard>
  );
}
