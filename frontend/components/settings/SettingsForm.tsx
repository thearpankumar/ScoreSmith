"use client";

import { useState, type FormEvent } from "react";
import { Check } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, updateCurrentUser } from "@/lib/api-client";
import type { User } from "@/lib/types";

/**
 * Wired to the real backend (`PATCH /api/v1/me` via `updateCurrentUser` — see
 * lib/api-client.ts): the display name is persisted for real; the e-mail address is the sign-in
 * identity and is shown read-only. The weekly-digest checkbox and
 * role remain local/display-only — there's no notification-preferences column or RBAC
 * yet (both explicitly deferred past Cycle 1, per the plan).
 */
export function SettingsForm({ user }: { user: User }) {
  const [name, setName] = useState(user.name);
  const [weeklyDigest, setWeeklyDigest] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await updateCurrentUser({ name });
      setName(updated.name);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save your changes. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-5">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="settings-name">Name</Label>
        <Input id="settings-name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="settings-email">Email</Label>
        <Input id="settings-email" type="email" value={user.email} readOnly aria-readonly />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label>Role</Label>
        <p className="text-sm text-ink-muted capitalize">{user.role} — role management is not yet available in Cycle 1</p>
      </div>

      <label className="flex items-center gap-2 text-sm text-ink">
        <input
          type="checkbox"
          checked={weeklyDigest}
          onChange={(e) => setWeeklyDigest(e.target.checked)}
          className="size-4 rounded border-hairline accent-[var(--lemon)]"
        />
        Email me a weekly digest of new evaluations below target
      </label>

      {error && (
        <p className="text-sm text-[var(--rag-poor)]" role="alert">
          {error}
        </p>
      )}

      <div className="flex items-center gap-3">
        <Button type="submit" disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
        {saved && (
          <span className="flex items-center gap-1 text-sm text-[var(--rag-excellent)]">
            <Check className="size-4" aria-hidden />
            Saved
          </span>
        )}
      </div>
    </form>
  );
}
