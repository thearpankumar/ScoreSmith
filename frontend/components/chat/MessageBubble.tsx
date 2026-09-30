import { Bot, User } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/lib/types";

import { MarkdownContent } from "./MarkdownContent";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

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
      <div
        className={cn(
          "max-w-[80%] min-w-0 rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
          isUser ? "bg-lemon-soft text-ink" : "glass-elev-1 text-ink",
        )}
      >
        <MarkdownContent content={message.content} />
      </div>
    </div>
  );
}
