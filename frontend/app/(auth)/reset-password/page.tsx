import type { Metadata } from "next";

import { AuthCard } from "@/components/auth/AuthCard";
import { ResetForm } from "@/components/auth/ResetForm";

export const metadata: Metadata = { title: "Choose a new password · Score Smith" };

export default async function ResetPasswordPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  return (
    <AuthCard title="Choose a new password" subtitle="Use at least 12 characters" variant="simple">
      <ResetForm token={token ?? ""} />
    </AuthCard>
  );
}
