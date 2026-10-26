import { Bot, FileText, User } from "lucide-react";

import { outlineSummary } from "@/lib/chat-attachments";
import { stripEditSummary } from "@/lib/chat-turn-state";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";

import { MarkdownContent } from "./MarkdownContent";

export function MessageBubble({ message, modelLabel }: { message: ChatMessage; modelLabel?: string | null }) {
  const isUser = message.role === "user";
  const files = message.attachments ?? [];

  return (
    <div className={cn("flex items-start gap-2.5", isUser && "flex-row-reverse")}>
      <div
        className={cn(
          "flex size-7 shrink-0 items-center justify-center rounded-full",
          isUser ? "bg-ink text-white" : "bg-lemon text-lemon-ink",
        )}
        aria-hidden
      >
        {isUser ? <User className="size-3.5" /> : <Bot className="size-3.5" />}
      </div>
      <div className={cn("flex max-w-[80%] min-w-0 flex-col gap-1", isUser && "items-end")}>
        <div
          className={cn(
            "min-w-0 rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
            isUser ? "bg-lemon-soft text-ink" : "glass-elev-1 text-ink",
          )}
        >
          <MarkdownContent content={isUser ? stripEditSummary(message.content) : message.content} />
        </div>
        {files.length > 0 && (
          <ul className="flex flex-wrap justify-end gap-1.5" aria-label="Attached files">
            {files.map((f) => {
              const detail = outlineSummary(f.outline);
              return (
                <li
                  key={f.id}
                  className="flex max-w-full items-center gap-1 rounded-lg border border-hairline glass-elev-1 px-2 py-0.5 text-xs text-ink"
                  title={detail ? `${f.filename}: ${detail}` : f.filename}
                >
                  <FileText className="size-3 shrink-0 text-ink-muted" aria-hidden />
                  <span className="max-w-[14rem] truncate">{f.filename}</span>
                </li>
              );
            })}
          </ul>
        )}
        {!isUser && modelLabel && <p className="px-1 text-[11px] text-ink-muted">{modelLabel}</p>}
      </div>
    </div>
  );
}
