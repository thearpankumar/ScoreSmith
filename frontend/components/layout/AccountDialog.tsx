"use client";

import { useState, type FormEvent } from "react";
import { Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, updateCurrentUser } from "@/lib/api-client";
import { changeMyPassword } from "@/lib/collab-client";
import { validateName, validateNewPassword } from "@/lib/auth-helpers";
import type { SessionUser } from "./UserMenu";

/**
 * "Account": the one thing a normal user needs from the old Settings page (their display name) plus a password
 * change. Reachable from the user menu for everybody, so Settings itself can stay admin-only.
 */
export function AccountDialog({
  user,
  open,
  onOpenChange,
}: {
  user: SessionUser;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = useState(user.name);
  const [nameState, setNameState] = useState<{ kind: "idle" | "saving" | "saved" | "error"; message?: string }>({
    kind: "idle",
  });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [pwState, setPwState] = useState<{ kind: "idle" | "saving" | "error"; message?: string }>({ kind: "idle" });

  async function saveName(e: FormEvent) {
    e.preventDefault();
    const problem = validateName(name);
    if (problem) return setNameState({ kind: "error", message: problem });
    setNameState({ kind: "saving" });
    try {
      await updateCurrentUser({ name: name.trim() });
      setNameState({ kind: "saved" });
      setTimeout(() => window.location.reload(), 600); // the rail / top bar read the name from the server render
    } catch (err) {
      setNameState({ kind: "error", message: err instanceof ApiError ? err.message : "Could not save your name." });
    }
  }

  async function savePassword(e: FormEvent) {
    e.preventDefault();
    const problem = validateNewPassword(next, user.email);
    if (problem) return setPwState({ kind: "error", message: problem });
    setPwState({ kind: "saving" });
    try {
      await changeMyPassword(current, next);
      // Every session (including this one) was signed out by the server.
      window.location.assign("/login");
    } catch (err) {
      setPwState({ kind: "error", message: err instanceof ApiError ? err.message : "Could not change the password." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Your account</DialogTitle>
          <DialogDescription className="break-words">
            {user.email} · {user.role === "admin" ? "Administrator" : "User"}
            {user.username ? ` · @${user.username}` : ""}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={saveName} className="flex flex-col gap-2" aria-label="Profile">
          <Label htmlFor="acct-name">Display name</Label>
          <div className="flex gap-2">
            <Input id="acct-name" value={name} onChange={(e) => setName(e.target.value)} className="min-h-11" />
            <Button type="submit" className="min-h-11" disabled={nameState.kind === "saving" || name.trim() === user.name}>
              {nameState.kind === "saving" ? <Loader2 className="animate-spin" aria-hidden /> : nameState.kind === "saved" ? <Check aria-hidden /> : null}
              Save
            </Button>
          </div>
          {nameState.kind === "error" && (
            <p role="alert" className="text-xs text-[var(--rag-poor)]">
              {nameState.message}
            </p>
          )}
        </form>

        <form onSubmit={savePassword} className="flex flex-col gap-2 border-t border-hairline pt-4" aria-label="Change password">
          <h3 className="text-sm font-semibold text-ink">Change password</h3>
          <Label htmlFor="acct-current">Current password</Label>
          <Input id="acct-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} className="min-h-11" />
          <Label htmlFor="acct-new">New password</Label>
          <Input id="acct-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} className="min-h-11" />
          <p className="text-xs text-ink-muted">At least 12 characters. You will be signed out everywhere afterwards.</p>
          {pwState.kind === "error" && (
            <p role="alert" className="text-xs text-[var(--rag-poor)]">
              {pwState.message}
            </p>
          )}
          <Button type="submit" className="min-h-11 self-start" disabled={pwState.kind === "saving" || !current || !next}>
            {pwState.kind === "saving" && <Loader2 className="animate-spin" aria-hidden />}
            Change password
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
