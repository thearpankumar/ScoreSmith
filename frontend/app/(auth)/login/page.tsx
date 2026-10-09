import { redirect } from "next/navigation";

import { AuthCard } from "@/components/auth/AuthCard";
import { LoginForm } from "@/components/auth/LoginForm";
import { getAuthConfig } from "@/lib/auth-client";
import { oauthErrorMessage } from "@/lib/auth-helpers";

type SearchParams = Promise<{ next?: string; expired?: string; error?: string; reset?: string; setup?: string }>;

export default async function LoginPage({ searchParams }: { searchParams: SearchParams }) {
  const sp = await searchParams;
  const config = await getAuthConfig();
  // Fresh production install (no users yet, bootstrap token configured): the only useful page is the setup page.
  if (config.setupRequired) redirect("/setup");
  const notice = sp.setup
    ? "Administrator account created. Sign in to continue."
    : (oauthErrorMessage(sp.error) ?? (sp.expired ? "Your session expired. Please sign in again." : null));
  return (
    <AuthCard title="Welcome back" subtitle="Sign in to continue">
      <LoginForm next={sp.next} notice={notice} signupEnabled={config.signupEnabled} usernameLogin={config.usernameLogin} />
    </AuthCard>
  );
}
