"use client";

import { useRef } from "react";
import { AlertTriangle, FileSpreadsheet, FileText, Image as ImageIcon, Loader2, Paperclip, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatBytes } from "@/lib/ai-upload";
import { CHAT_FILE_ACCEPT, fileExtension, outlineSummary } from "@/lib/chat-attachments";
import type { ComposerAttachment } from "@/lib/chat-attachments";
import type { ChatConfig } from "@/lib/chat-models";
import { cn } from "@/lib/utils";

function FileIcon({ name, className }: { name: string; className?: string }) {
  const ext = fileExtension(name);
  if (ext === "xlsx" || ext === "csv") return <FileSpreadsheet className={className} aria-hidden />;
  if (["png", "jpg", "jpeg", "webp"].includes(ext)) return <ImageIcon className={className} aria-hidden />;
  return <FileText className={className} aria-hidden />;
}

/** The files attached to the next message, as removable chips (upload progress, "reading", failure reason). */
export function ComposerAttachmentChips({
  items,
  rejections,
  onRemove,
  onDismissRejections,
}: {
  items: ComposerAttachment[];
  rejections: string[];
  onRemove: (localId: string) => void;
  onDismissRejections: () => void;
}) {
  if (items.length === 0 && rejections.length === 0) return null;
  return (
    <div className="space-y-1.5 px-3 pt-2.5" data-testid="composer-attachments">
      {rejections.length > 0 && (
        <div role="alert" className="flex items-start justify-between gap-2 rounded-lg bg-lemon-soft px-2.5 py-1.5 text-xs text-lemon-ink">
          <span className="min-w-0">{rejections.join(" ")}</span>
          <button type="button" onClick={onDismissRejections} aria-label="Dismiss" className="shrink-0">
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      )}
      <ul className="flex flex-wrap gap-1.5">
        {items.map((item) => {
          const detail =
            item.status === "uploading"
              ? `Uploading ${Math.round(item.progress * 100)}%`
              : item.status === "reading"
                ? "Reading…"
                : item.status === "failed"
                  ? item.error || "Failed"
                  : outlineSummary(item.outline) || formatBytes(item.size);
          return (
            <li
              key={item.localId}
              className={cn(
                "flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1 text-xs",
                item.status === "failed" ? "border-red-300 bg-red-50 text-red-800" : "border-hairline glass-elev-1 text-ink",
              )}
              title={item.status === "failed" ? item.error ?? undefined : item.name}
            >
              {item.status === "uploading" || item.status === "reading" ? (
                <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />
              ) : item.status === "failed" ? (
                <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
              ) : (
                <FileIcon name={item.name} className="size-3.5 shrink-0 text-ink-muted" />
              )}
              <span className="max-w-[12rem] truncate font-medium">{item.name}</span>
              <span className="truncate text-ink-muted">{detail}</span>
              <button
                type="button"
                onClick={() => onRemove(item.localId)}
                aria-label={`Remove ${item.name}`}
                className="shrink-0 rounded p-0.5 hover:bg-black/5"
              >
                <X className="size-3" aria-hidden />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The attach button and the chat-model picker, shown only when the server offers them (OpenRouter). */
export function ComposerToolbar({
  config,
  model,
  onModelChange,
  onFiles,
  disabled,
}: {
  config: ChatConfig;
  model: string | null;
  onModelChange: (model: string) => void;
  onFiles: (files: File[]) => void;
  disabled: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  if (!config.attachments.enabled && !config.modelPicker) return null;
  return (
    <div className="flex items-center gap-2 px-3 pt-2" data-testid="composer-toolbar">
      {config.attachments.enabled && (
        <>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={CHAT_FILE_ACCEPT}
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              e.target.value = ""; // picking the same file again still fires
              if (files.length) onFiles(files);
            }}
          />
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
            aria-label="Attach files"
            title="Attach PDF, Word, Excel, CSV, text or image files"
          >
            <Paperclip className="size-3.5" aria-hidden />
            Attach
          </Button>
        </>
      )}
      {config.modelPicker && model && (
        <label className="ml-auto flex items-center gap-1.5 text-xs text-ink-muted">
          <span>Model</span>
          <select
            value={model}
            disabled={disabled}
            onChange={(e) => onModelChange(e.target.value)}
            className="h-8 glass-field rounded-lg px-2 text-xs text-ink disabled:opacity-60"
            aria-label="Chat model"
            data-testid="chat-model-select"
          >
            {config.models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}
