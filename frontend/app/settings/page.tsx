import { GlassCard } from "@/components/design-system/GlassCard";
import { SettingsForm } from "@/components/settings/SettingsForm";
import { getCurrentUser } from "@/lib/api-client";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const user = await getCurrentUser();

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Settings</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Profile and notification preferences. Full org/RBAC settings are deferred past Cycle 1.
        </p>
      </div>

      <GlassCard elevation={1} className="p-6">
        <SettingsForm user={user} />
      </GlassCard>
    </div>
  );
}
