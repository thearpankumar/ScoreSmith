import type { Metadata } from "next";

import { AuthCard } from "@/components/auth/AuthCard";
import { ForgotForm } from "@/components/auth/ForgotForm";

export const metadata: Metadata = { title: "Reset password · KPI Metrics" };

export default function ForgotPasswordPage() {
  return (
    <AuthCard title="Forgot password?" subtitle="We’ll email you a link to reset it" variant="simple">
      <ForgotForm />
    </AuthCard>
  );
}
