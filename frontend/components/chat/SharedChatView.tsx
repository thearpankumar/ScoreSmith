"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AlertTriangle, ArrowLeft, Loader2, Save } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ChatShareButton } from "@/components/sharing/ChatShareDialog";
import { saveSharedChat, type SharedChat } from "@/lib/chat-share-client";
import { cn, formatDateTime } from "@/lib/utils";

type Kpi = NonNullable<SharedChat["draft"]["kpis"]>[number];

/** Parents first, children indented under their parent (the draft carries `parent_name`, not ids). */
function outline(kpis: Kpi[]): Array<Kpi & { depth: number }> {
  const out: Array<Kpi & { depth: number }> = [];
  const walk = (parent: string | null, depth: number) => {
    for (const k of kpis.filter((x) => (x.parent_name ?? null) === parent)) {
      out.push({ ...k, depth });
      if (depth < 4) walk(k.name, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/**
 * A chat somebody shared with you: the conversation and the KPIs, read-only. You cannot talk to the assistant in it;
 * "Save as my chart" copies the KPIs into a chart of your own (when the owner also shared the chart you can open it).
 */
export function SharedChatView({ chat }: { chat: SharedChat }) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kpis = outline(chat.draft.kpis ?? []);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      router.push(`/charts/${await saveSharedChat(chat.sessionId)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the chart.");
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 py-2">
      {/* md:pr-14 leaves room for the notification bell that floats at the top right of the page */}
      <div className="flex flex-col gap-1">
        {/* One line: back link, the actions, and (md+) the bell at the far right; every control is 44px tall. */}
        <div className="flex flex-wrap items-center justify-between gap-3 md:pr-14">
          <Button asChild variant="ghost" size="sm" className="-ml-3 min-h-11">
            <Link href="/chat">
              <ArrowLeft aria-hidden /> All chats
            </Link>
          </Button>
          <div className="flex flex-wrap items-center gap-2">
            {/* Top right, like on the chat itself: pass the chat on (read-only) to somebody else. */}
            <ChatShareButton sessionId={chat.sessionId} hasChart={Boolean(chat.linkedScorecardId)} />
            {chat.linkedScorecardId && (
              <Button asChild variant="outline" className="min-h-11">
                <Link href={`/charts/${chat.linkedScorecardId}`}>Open the shared chart</Link>
              </Button>
            )}
            {chat.savedScorecardId ? (
              <Button asChild className="min-h-11">
                <Link href={`/charts/${chat.savedScorecardId}`}>Open my saved copy</Link>
              </Button>
            ) : (
              <Button
                type="button"
                className="min-h-11"
                onClick={save}
                disabled={saving || !chat.canSave}
                title={chat.canSave ? undefined : "The KPIs are not complete yet"}
              >
                {saving ? <Loader2 className="animate-spin" aria-hidden /> : <Save aria-hidden />} Save as my chart
              </Button>
            )}
          </div>
        </div>
        <div className="min-w-0">
          <h1 className="break-words text-2xl font-semibold text-ink">{chat.title ?? "Shared chat"}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-muted">
            {chat.sharedByName && chat.sharedByName !== chat.ownerName
              ? `${chat.ownerName}'s chat, shared by ${chat.sharedByName}`
              : `Shared by ${chat.ownerName}`}{" "}
            ·{" "}
            {/* server (UTC) and browser (local time zone) format the date differently: not a hydration error */}
            <time dateTime={chat.sharedAt} suppressHydrationWarning>
              {formatDateTime(chat.sharedAt)}
            </time>
            <Badge variant="soft">Read-only</Badge>
            {chat.withChart && <Badge variant="outline">Chart shared too</Badge>}
          </p>
        </div>
      </div>
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden /> {error}
        </p>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <GlassCard elevation={1} className="flex min-w-0 flex-col gap-3 p-4" aria-label="Conversation">
          {chat.messages.length === 0 && <p className="text-sm text-ink-muted">No messages.</p>}
          {chat.messages.map((m) => (
            <div key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
              <div
                className={cn(
                  "max-w-[90%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-sm",
                  m.role === "user" ? "bg-lemon-soft text-ink" : "bg-white/70 text-ink",
                )}
              >
                {m.content}
              </div>
            </div>
          ))}
        </GlassCard>
        <SolidPanel className="h-fit p-4" aria-label="KPIs in this chat">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-muted">The KPIs</p>
          <p className="mt-1 break-words text-base font-semibold text-ink">{chat.draft.name ?? "Untitled draft"}</p>
          {chat.draft.purpose && <p className="mt-1 text-sm text-ink-muted">{chat.draft.purpose}</p>}
          {kpis.length === 0 ? (
            <p className="mt-3 text-sm text-ink-muted">No KPIs yet.</p>
          ) : (
            <ul className="mt-3 flex flex-col">
              {kpis.map((k) => (
                <li
                  key={`${k.name}-${k.depth}`}
                  className="flex items-center justify-between gap-2 py-1.5 text-sm"
                  style={{ paddingLeft: `${k.depth}rem` }}
                >
                  <span className={cn("min-w-0 break-words", k.depth === 0 && "font-semibold")}>{k.name}</span>
                  {k.weight != null && <span className="shrink-0 text-xs tabular-nums text-ink-muted">{k.weight}%</span>}
                </li>
              ))}
            </ul>
          )}
        </SolidPanel>
      </div>
    </div>
  );
}
