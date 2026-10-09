import { redirect } from "next/navigation";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SettingsForm } from "@/components/settings/SettingsForm";
import { getCurrentUser } from "@/lib/api-client";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const user = await getCurrentUser();
  // Settings is for administrators; everyone's own profile / password lives in the Account dialog of the user menu.
  if (user.role !== "admin") redirect("/chat");

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Administrator settings and your profile. People are managed under Users.
        </p>
      </div>

      <GlassCard elevation={1} className="p-6">
        <SettingsForm user={user} />
      </GlassCard>
    </div>
  );
}
