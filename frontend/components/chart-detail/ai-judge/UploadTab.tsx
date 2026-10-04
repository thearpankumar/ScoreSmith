"use client";

import { AlertTriangle, X } from "lucide-react";

import { MAX_FILES, SUBMISSION_EXTENSIONS } from "@/lib/ai-upload";
import { Dropzone } from "./Dropzone";
import { UploadRow } from "./UploadRow";
import type { useFileUploads } from "./use-file-uploads";

type Uploads = ReturnType<typeof useFileUploads>;

export function UploadTab({ uploads, disabled }: { uploads: Uploads; disabled?: boolean }) {
  const { items, rejections, clearRejections, addFiles, cancel, retry, remove } = uploads;
  const full = items.length >= MAX_FILES;
  return (
    <div className="flex flex-col gap-4">
      <Dropzone
        accept={SUBMISSION_EXTENSIONS.map((e) => `.${e}`).join(",")}
        multiple
        onFiles={addFiles}
        disabled={disabled || full}
        compact={items.length > 0}
        title={full ? "File limit reached" : "Drop files here, or click to browse"}
        hint={`Video (mp4, mov, mkv, webm, m4v), PDF, DOCX or Markdown/text. Up to ${MAX_FILES} files, 2 GiB each. All files here are scored together as one submission.`}
      />

      {rejections.length > 0 && (
        <div role="alert" className="flex items-start gap-2 rounded-xl bg-[var(--rag-poor)]/10 p-3 text-xs text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <ul className="min-w-0 flex-1 space-y-0.5 break-words">
            {rejections.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <button
            type="button"
            onClick={clearRejections}
            aria-label="Dismiss"
            className="rounded-full p-0.5 hover:bg-black/5 focus-visible:outline-2 focus-visible:outline-[var(--focus)]"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>
      )}

      {items.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Files to evaluate">
          {items.map((item) => (
            <UploadRow key={item.id} item={item} onCancel={cancel} onRetry={retry} onRemove={remove} />
          ))}
        </ul>
      )}
    </div>
  );
}
