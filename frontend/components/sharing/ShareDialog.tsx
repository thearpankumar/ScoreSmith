"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Check, Loader2, LogOut, Share2, UserMinus } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api-client";
import {
  getSharing,
  inviteCollaborator,
  leaveScorecard,
  removeCollaborator,
  revokeInvitation,
  type Sharing,
} from "@/lib/collab-client";
import { cn, formatDate } from "@/lib/utils";

/** Maps the server's machine-readable invite errors to clear sentences. Exported for the unit tests. */
export function inviteErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "user_not_found":
        return "No user found with that username or email. Check the spelling, or ask them for the exact username.";
      case "self_invite":
        return "That is you - you already own this chart.";
      case "already_collaborator":
        return err.message;
      case "lookup_rate_limited":
        return "You have sent a lot of invitations in a short time. Please wait a few minutes and try again.";
      case "owner_only":
        return "Only the owner of this chart can invite people.";
    }
    if (err.status === 429) return "Too many attempts. Please wait a moment and try again.";
    return err.message;
  }
  return "Could not send the invitation. Try again.";
}

const STATUS_STYLE: Record<string, "lemon" | "muted" | "outline"> = {
  pending: "lemon",
  accepted: "outline",
  declined: "muted",
  revoked: "muted",
};

/**
 * Share / manage collaborators of one chart. Owner: invite by username or email (a clear "no such user" answer comes
 * back when nobody matches), see every invitation with its status (pending / accepted / declined / revoked), revoke
 * pending ones and remove collaborators. Editor: see who collaborates and leave the chart.
 */
export function ShareButton({
  scorecardId,
  scorecardName,
  myRole,
  isShared,
}: {
  scorecardId: string;
  scorecardName: string;
  myRole: "owner" | "editor";
  isShared: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button type="button" variant={isShared ? "outline" : "default"} className="min-h-11" onClick={() => setOpen(true)}>
        <Share2 className="size-4" aria-hidden />
        Share
      </Button>
      <ShareDialog
        open={open}
        onOpenChange={setOpen}
        scorecardId={scorecardId}
        scorecardName={scorecardName}
        myRole={myRole}
      />
    </>
  );
}

export function ShareDialog({
  open,
  onOpenChange,
  scorecardId,
  scorecardName,
  myRole,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scorecardId: string;
  scorecardName: string;
  myRole: "owner" | "editor";
}) {
  const router = useRouter();
  const [sharing, setSharing] = useState<Sharing | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [identifier, setIdentifier] = useState("");
  const [includeChat, setIncludeChat] = useState(false);
  const [sending, setSending] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSharing(await getSharing(scorecardId));
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Could not load sharing details.");
    }
  }, [scorecardId]);

  useEffect(() => {
    if (open) void load();
    else {
      setFeedback(null);
      setConfirming(null);
    }
  }, [open, load]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const value = identifier.trim();
    if (!value) return;
    setSending(true);
    setFeedback(null);
    try {
      const inv = await inviteCollaborator(scorecardId, value, includeChat && Boolean(sharing?.sourceChatId));
      setFeedback({
        kind: "ok",
        text: inv.alreadyPending
          ? `${inv.invitee.name} already has a pending invitation to this chart.`
          : inv.chatShared
            ? `Invitation sent to ${inv.invitee.name}, together with the chat (read-only). They will see both in their notifications.`
            : `Invitation sent to ${inv.invitee.name}. They will see it in their notifications.`,
      });
      setIdentifier("");
      await load();
    } catch (err) {
      setFeedback({ kind: "error", text: inviteErrorMessage(err) });
    } finally {
      setSending(false);
    }
  }

  async function run(key: string, fn: () => Promise<void>, done: string) {
    setBusy(key);
    setFeedback(null);
    try {
      await fn();
      setFeedback({ kind: "ok", text: done });
      setConfirming(null);
      await load();
      router.refresh();
    } catch (err) {
      setFeedback({ kind: "error", text: err instanceof Error ? err.message : "Something went wrong." });
    } finally {
      setBusy(null);
    }
  }

  const isOwner = myRole === "owner";
  // Re-sharing: the owner AND every collaborator may invite further people (chains of collaborators are fine);
  // only the owner removes people. Anyone can see who has access.
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] w-[calc(100vw-1.5rem)] max-w-xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader className="pr-6">
          <DialogTitle>Share this chart</DialogTitle>
          <DialogDescription className="break-words">
            {`Invite someone by username or email to edit “${scorecardName}” together. They can preview it before accepting.`}
            {isOwner ? "" : " You are an editor here: you can invite people too, but only the owner can remove them."}
          </DialogDescription>
        </DialogHeader>

        {(
          <form onSubmit={submit} className="flex flex-col gap-2" aria-label="Invite a collaborator">
            <Label htmlFor="share-identifier">Username or email</Label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                id="share-identifier"
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                placeholder="e.g. priya or priya@company.com"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                className="min-h-11 flex-1"
                aria-describedby="share-feedback"
              />
              <Button type="submit" className="min-h-11" disabled={sending || !identifier.trim()}>
                {sending && <Loader2 className="animate-spin" aria-hidden />}
                Send invite
              </Button>
            </div>
            {sharing?.sourceChatId && (
              <fieldset className="mt-1 flex flex-col gap-2">
                <legend className="mb-1 text-sm font-medium text-ink">What do they get?</legend>
                <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-hairline bg-white/50 p-3 text-sm">
                  <input
                    type="radio"
                    name="chart-share-mode"
                    checked={!includeChat}
                    onChange={() => setIncludeChat(false)}
                    className="mt-1 size-4 accent-[var(--ink)]"
                  />
                  <span>
                    <span className="block font-medium text-ink">Only the chart</span>
                    <span className="text-xs text-ink-muted">They can edit the chart; its evaluations are shared with it.</span>
                  </span>
                </label>
                <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-hairline bg-white/50 p-3 text-sm">
                  <input
                    type="radio"
                    name="chart-share-mode"
                    checked={includeChat}
                    onChange={() => setIncludeChat(true)}
                    className="mt-1 size-4 accent-[var(--ink)]"
                  />
                  <span>
                    <span className="block font-medium text-ink">Chart and its chat</span>
                    <span className="text-xs text-ink-muted">
                      They also read the conversation that built this chart (read-only).
                    </span>
                  </span>
                </label>
              </fieldset>
            )}
          </form>
        )}
        <div id="share-feedback" aria-live="polite" role="status">
          {feedback && (
            <p
              className={cn(
                "flex items-start gap-1.5 rounded-lg px-3 py-2 text-sm",
                feedback.kind === "ok" ? "bg-lemon-soft text-lemon-ink" : "bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]",
              )}
              role={feedback.kind === "error" ? "alert" : undefined}
            >
              {feedback.kind === "ok" ? <Check className="mt-0.5 size-4 shrink-0" aria-hidden /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />}
              <span>{feedback.text}</span>
            </p>
          )}
        </div>

        {loadError && (
          <p role="alert" className="text-sm text-[var(--rag-poor)]">
            {loadError}
          </p>
        )}
        {!sharing && !loadError && (
          <p className="flex items-center gap-2 text-sm text-ink-muted">
            <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading…
          </p>
        )}

        {sharing && (
          <>
            <section aria-label="People with access" className="flex flex-col gap-2">
              <h3 className="text-sm font-semibold text-ink">People with access</h3>
              <ul className="flex flex-col divide-y divide-hairline rounded-xl border border-hairline bg-white/50">
                <Person name={sharing.owner.name} detail={sharing.owner.email ?? ""} badge="Owner" />
                {sharing.collaborators.map((c) => (
                  <Person
                    key={c.user.id}
                    name={c.user.name}
                    detail={`${c.user.email ?? ""} · joined ${formatDate(c.joinedAt)}`}
                    badge="Editor"
                    action={
                      isOwner ? (
                        confirming === `rm:${c.user.id}` ? (
                          <span className="flex gap-1.5">
                            <Button type="button" variant="destructive" size="sm" className="min-h-11" disabled={busy !== null} onClick={() => run(`rm:${c.user.id}`, () => removeCollaborator(scorecardId, c.user.id), `${c.user.name} was removed.`)}>
                              {busy === `rm:${c.user.id}` && <Loader2 className="animate-spin" aria-hidden />} Confirm remove
                            </Button>
                            <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={() => setConfirming(null)}>
                              Cancel
                            </Button>
                          </span>
                        ) : (
                          <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={() => setConfirming(`rm:${c.user.id}`)} aria-label={`Remove ${c.user.name}`}>
                            <UserMinus aria-hidden /> Remove
                          </Button>
                        )
                      ) : null
                    }
                  />
                ))}
              </ul>
              {!isOwner && (
                <div className="flex flex-col gap-2">
                  {confirming === "leave" ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm text-ink">Leave this chart? You will lose access until invited again.</span>
                      <Button type="button" variant="destructive" size="sm" className="min-h-11" disabled={busy !== null} onClick={async () => { await run("leave", () => leaveScorecard(scorecardId), "You left the chart."); window.location.assign("/charts"); }}>
                        Leave chart
                      </Button>
                      <Button type="button" variant="ghost" size="sm" className="min-h-11" onClick={() => setConfirming(null)}>
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <Button type="button" variant="outline" className="min-h-11 self-start" onClick={() => setConfirming("leave")}>
                      <LogOut aria-hidden /> Leave this chart
                    </Button>
                  )}
                </div>
              )}
            </section>

            {(
              <section aria-label="Invitations you sent" className="flex flex-col gap-2">
                <h3 className="text-sm font-semibold text-ink">Invitations you sent</h3>
                {sharing.invitations.length === 0 ? (
                  <p className="text-sm text-ink-muted">No invitations yet.</p>
                ) : (
                  <ul className="flex flex-col divide-y divide-hairline rounded-xl border border-hairline bg-white/50">
                    {sharing.invitations.map((i) => (
                      <Person
                        key={i.id}
                        name={i.invitee.name}
                        detail={`${i.invitee.email ?? ""} · ${formatDate(i.createdAt)}`}
                        badge={i.status}
                        badgeVariant={STATUS_STYLE[i.status] ?? "muted"}
                        action={
                          i.status === "pending" ? (
                            <Button type="button" variant="ghost" size="sm" className="min-h-11" disabled={busy !== null} onClick={() => run(`rev:${i.id}`, () => revokeInvitation(scorecardId, i.id), "Invitation revoked.")} aria-label={`Revoke invitation to ${i.invitee.name}`}>
                              {busy === `rev:${i.id}` && <Loader2 className="animate-spin" aria-hidden />} Revoke
                            </Button>
                          ) : null
                        }
                      />
                    ))}
                  </ul>
                )}
              </section>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Person({
  name,
  detail,
  badge,
  badgeVariant = "outline",
  action,
}: {
  name: string;
  detail: string;
  badge: string;
  badgeVariant?: "lemon" | "muted" | "outline";
  action?: React.ReactNode;
}) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
          <span className="break-words">{name}</span>
          <Badge variant={badgeVariant} className="capitalize">
            {badge}
          </Badge>
        </p>
        <p className="break-all text-xs text-ink-muted">{detail}</p>
      </div>
      {action}
    </li>
  );
}
