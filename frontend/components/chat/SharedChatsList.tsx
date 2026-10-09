"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Users } from "lucide-react";

import { listSharedChats, type SharedChatSummary } from "@/lib/chat-share-client";
import { cn } from "@/lib/utils";

/** "Shared with me": chats other people shared (read-only). Sits under the user's own chats in the chat list. */
export function SharedChatsList({ activeSessionId }: { activeSessionId: string }) {
  const [items, setItems] = useState<SharedChatSummary[]>([]);
  useEffect(() => {
    let alive = true;
    listSharedChats()
      .then((rows) => alive && setItems(rows))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  if (items.length === 0) return null;
  return (
    <section aria-label="Chats shared with me" className="mt-2 flex flex-col gap-1 border-t border-hairline pt-2">
      <h3 className="flex items-center gap-1.5 px-3 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
        <Users className="size-3" aria-hidden /> Shared with me
      </h3>
      {items.map((c) => (
        <Link
          key={c.sessionId}
          href={`/chat/shared/${c.sessionId}`}
          className={cn(
            "block rounded-xl px-3 py-2 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
            c.sessionId === activeSessionId ? "bg-white/80 shadow-sm" : "hover:bg-white/50",
          )}
        >
          <p className="truncate text-sm font-medium text-ink">{c.title ?? "Shared chat"}</p>
          <p className="truncate text-[11px] text-ink-muted">
            from {c.sharedByName && c.sharedByName !== c.ownerName ? `${c.ownerName} via ${c.sharedByName}` : c.ownerName}
            {c.withChart ? " · with chart" : ""}
          </p>
        </Link>
      ))}
    </section>
  );
}
