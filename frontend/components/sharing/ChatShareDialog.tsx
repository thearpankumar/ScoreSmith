"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, Check, Loader2, Share2, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/api-client";
import { listChatShares, revokeChatShare, shareChat, type ChatShare } from "@/lib/chat-share-client";
import { cn } from "@/lib/utils";
import { inviteErrorMessage } from "./ShareDialog";

/**
 * Top-right "Share" of a chat. Two choices: share the chat only (the person reads the conversation and sees the KPIs,
 * and has to save their own copy), or share the chart too (they are invited to collaborate on the chart and its
 * evaluations come with it). The second is only offered once the chat has produced a saved chart.
 */
export function ChatShareButton({ sessionId, hasChart }: { sessionId: string; hasChart: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="min-h-11"
        onClick={() => setOpen(true)}
        data-testid="chat-share-button"
      >
        <Share2 className="size-4" aria-hidden /> Share
      </Button>
      <ChatShareDialog open={open} onOpenChange={setOpen} sessionId={sessionId} hasChart={hasChart} />
    </>
  );
}

function ChatShareDialog({
  open,
  onOpenChange,
  sessionId,
  hasChart,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  sessionId: string;
  hasChart: boolean;
}) {
  const [identifier, setIdentifier] = useState("");
  const [withChart, setWithChart] = useState(false);
  const [shares, setShares] = useState<ChatShare[]>([]);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setShares(await listChatShares(sessionId));
    } catch {
      /* the list is optional */
    }
  }, [sessionId]);

  useEffect(() => {
    if (open) void load();
    else setFeedback(null);
  }, [open, load]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!identifier.trim()) return;
    setBusy(true);
    setFeedback(null);
    try {
      const s = await shareChat(sessionId, identifier.trim(), withChart && hasChart);
      setFeedback({
        kind: "ok",
        text: s.withChart
          ? `Shared with ${s.recipientName}: the chat now, and an invitation to the chart.`
          : `Shared with ${s.recipientName}. They can read the chat and save the KPIs as their own chart.`,
      });
      setIdentifier("");
      await load();
    } catch (err) {
      setFeedback({
        kind: "error",
        text: err instanceof ApiError && err.code === "no_chart_yet" ? err.message : inviteErrorMessage(err),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] w-[calc(100vw-1.5rem)] max-w-xl overflow-y-auto p-4 sm:p-6">
        <DialogHeader className="pr-6">
          <DialogTitle>Share this chat</DialogTitle>
          <DialogDescription>Invite someone by username or email. Nobody else can see your chats.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-3" aria-label="Share this chat">
          <Label htmlFor="chat-share-identifier">Username or email</Label>
          <Input
            id="chat-share-identifier"
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
            placeholder="e.g. priya or priya@company.com"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            className="min-h-11"
          />
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium text-ink">What do they get?</legend>
            <label className="flex cursor-pointer items-start gap-2 rounded-xl border border-hairline bg-white/50 p-3 text-sm">
              <input
                type="radio"
                name="chat-share-mode"
                checked={!withChart}
                onChange={() => setWithChart(false)}
                className="mt-1 size-4 accent-[var(--ink)]"
              />
              <span>
                <span className="block font-medium text-ink">Only the chat</span>
                <span className="text-xs text-ink-muted">
                  They read the conversation and see the KPIs. To keep them they save their own chart.
                </span>
              </span>
            </label>
            <label
              className={cn(
                "flex items-start gap-2 rounded-xl border border-hairline bg-white/50 p-3 text-sm",
                hasChart ? "cursor-pointer" : "opacity-60",
              )}
            >
              <input
                type="radio"
                name="chat-share-mode"
                checked={withChart}
                disabled={!hasChart}
                onChange={() => setWithChart(true)}
                className="mt-1 size-4 accent-[var(--ink)]"
              />
              <span>
                <span className="block font-medium text-ink">Chat and chart</span>
                <span className="text-xs text-ink-muted">
                  {hasChart
                    ? "They are also invited to edit the chart with you. Its evaluations are shared with the chart."
                    : "Available once this chat has saved a chart."}
                </span>
              </span>
            </label>
          </fieldset>
          <Button type="submit" className="min-h-11 self-start" disabled={busy || !identifier.trim()}>
            {busy && <Loader2 className="animate-spin" aria-hidden />} Share
          </Button>
        </form>
        <div aria-live="polite" role="status">
          {feedback && (
            <p
              className={cn(
                "flex items-start gap-1.5 rounded-lg px-3 py-2 text-sm",
                feedback.kind === "ok" ? "bg-lemon-soft text-lemon-ink" : "bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]",
              )}
              role={feedback.kind === "error" ? "alert" : undefined}
            >
              {feedback.kind === "ok" ? (
                <Check className="mt-0.5 size-4 shrink-0" aria-hidden />
              ) : (
                <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              )}
              <span>{feedback.text}</span>
            </p>
          )}
        </div>
        {shares.length > 0 && (
          <section aria-label="Shared with" className="flex flex-col gap-2">
            <h3 className="text-sm font-semibold text-ink">Shared with</h3>
            <ul className="flex flex-col divide-y divide-hairline rounded-xl border border-hairline bg-white/50">
              {shares.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
                      <span className="break-words">{s.recipientName}</span>
                      <Badge variant="outline">{s.withChart ? "Chat and chart" : "Chat only"}</Badge>
                      {s.withChart && s.chartInvitationStatus && (
                        <Badge variant={s.chartInvitationStatus === "pending" ? "lemon" : "muted"}>
                          Chart invitation: {s.chartInvitationStatus}
                        </Badge>
                      )}
                    </p>
                    <p className="break-all text-xs text-ink-muted">
                      {s.recipientEmail}
                      {s.sharedByName ? ` · shared by ${s.sharedByName}` : ""}
                    </p>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="min-h-11"
                    aria-label={`Stop sharing the chat with ${s.recipientName}`}
                    onClick={async () => {
                      await revokeChatShare(sessionId, s.id).catch(() => undefined);
                      await load();
                    }}
                  >
                    <Trash2 aria-hidden /> Stop
                  </Button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </DialogContent>
    </Dialog>
  );
}
