import type { Metadata } from "next";

import { AuthCard } from "@/components/auth/AuthCard";
import { VerifyEmailPanel } from "@/components/auth/VerifyEmailPanel";

export const metadata: Metadata = { title: "Verify email · Score Smith" };

export default async function VerifyEmailPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  return (
    <AuthCard title="Verify your email" subtitle="One moment" variant="simple">
      <VerifyEmailPanel token={token ?? ""} />
    </AuthCard>
  );
}
