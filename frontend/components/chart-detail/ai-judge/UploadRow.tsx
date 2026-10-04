"use client";

import { AlertTriangle, CheckCircle2, FileSpreadsheet, FileText, FileVideo, RotateCcw, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { fileExtension, formatBytes } from "@/lib/ai-upload";
import { cn } from "@/lib/utils";
import type { UploadItem } from "./use-file-uploads";

function FileIcon({ name }: { name: string }) {
  const ext = fileExtension(name);
  const cls = "size-5";
  if (ext === "xlsx" || ext === "csv") return <FileSpreadsheet className={cls} aria-hidden />;
  if (ext === "pdf" || ext === "docx") return <FileText className={cls} aria-hidden />;
  return <FileVideo className={cls} aria-hidden />;
}

/** One file in an upload list: name, size, progress bar and cancel / retry / remove controls. */
export function UploadRow({
  item,
  onCancel,
  onRetry,
  onRemove,
}: {
  item: UploadItem;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onRemove: (id: string) => void;
}) {
  const { file, status, loaded, error } = item;
  const pct = file.size > 0 ? Math.round((loaded / file.size) * 100) : 0;
  const uploading = status === "uploading";

  return (
    <li
      className={cn(
        "flex items-center gap-3 rounded-xl border bg-solid p-3",
        status === "error" ? "border-[var(--rag-poor)]/40" : "border-hairline",
      )}
    >
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-black/5 text-ink-muted">
        <FileIcon name={file.name} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-2">
          <p className="truncate text-sm font-medium text-ink" title={file.name}>
            {file.name}
          </p>
          <span className="shrink-0 text-xs tabular-nums text-ink-muted">{formatBytes(file.size)}</span>
        </div>
        {status === "done" ? (
          <p className="mt-1 flex items-center gap-1 text-xs text-[var(--rag-excellent)]">
            <CheckCircle2 className="size-3.5" aria-hidden /> Uploaded
          </p>
        ) : status === "error" ? (
          <p className="mt-1 flex items-start gap-1 text-xs text-[var(--rag-poor)]" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden /> {error ?? "Upload failed."}
          </p>
        ) : (
          <div className="mt-1.5 flex items-center gap-2">
            <Progress label={`Uploading ${file.name}`} value={status === "queued" ? 0 : pct} className="flex-1" />
            <span className="w-20 shrink-0 text-right text-xs tabular-nums text-ink-muted">
              {status === "queued" ? "Waiting" : status === "finalizing" ? "Verifying" : `${pct}%`}
            </span>
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {status === "error" && (
          <Button type="button" size="sm" variant="outline" onClick={() => onRetry(item.id)}>
            <RotateCcw aria-hidden /> Retry
          </Button>
        )}
        {status !== "finalizing" && (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="size-8"
            onClick={() => (uploading ? onCancel(item.id) : onRemove(item.id))}
            aria-label={uploading ? `Cancel upload of ${file.name}` : `Remove ${file.name}`}
            title={uploading ? "Cancel" : "Remove"}
          >
            <X aria-hidden />
          </Button>
        )}
      </div>
    </li>
  );
}
