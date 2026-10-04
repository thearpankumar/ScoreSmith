"use client";

import { useId } from "react";
import { Globe, Info } from "lucide-react";

import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { DriveLinkEntry, DriveLinkKind } from "@/lib/ai-upload";
import { cn } from "@/lib/utils";

const KIND_LABEL: Record<DriveLinkKind, string> = {
  folder: "Folder",
  file: "File",
  doc: "Google Doc",
  invalid: "Invalid",
};

function shorten(url: string): string {
  try {
    const u = new URL(url);
    const tail = `${u.pathname}${u.search}`;
    return `${u.hostname}${tail.length > 28 ? `${tail.slice(0, 26)}…` : tail}`;
  } catch {
    return url.length > 40 ? `${url.slice(0, 38)}…` : url;
  }
}

export function DriveTab({
  text,
  onChange,
  entries,
  disabled,
}: {
  text: string;
  onChange: (v: string) => void;
  entries: DriveLinkEntry[];
  disabled?: boolean;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const valid = entries.filter((e) => e.valid).length;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={id}>Google Drive links</Label>
        <span className="text-xs tabular-nums text-ink-muted">
          {valid} valid link{valid === 1 ? "" : "s"}
        </span>
      </div>
      <Textarea
        id={id}
        value={text}
        onChange={(e) => onChange(e.target.value)}
        placeholder={"https://drive.google.com/drive/folders/...\nhttps://drive.google.com/file/d/.../view"}
        className="min-h-40 bg-solid font-mono text-xs sm:text-sm"
        disabled={disabled}
        spellCheck={false}
        aria-describedby={hintId}
      />
      <p id={hintId} className="flex items-start gap-1.5 text-xs text-ink-muted">
        <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        <span>
          One link per line; each link becomes its own evaluation. Links must be public (sharing set to
          &ldquo;Anyone with the link can view&rdquo;), otherwise the files can&apos;t be fetched. Folders, files and Google
          Docs are supported.
        </span>
      </p>

      {entries.length > 0 && (
        <ul className="flex flex-wrap gap-2" aria-label="Detected links">
          {entries.map((e, i) => (
            <li
              key={`${i}-${e.raw}`}
              title={e.reason ?? e.raw}
              className={cn(
                "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
                e.kind === "invalid"
                  ? "border-[var(--rag-poor)]/40 bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]"
                  : e.duplicate
                    ? "border-hairline bg-black/5 text-ink-muted line-through"
                    : "border-hairline bg-solid text-ink",
              )}
            >
              <Globe className="size-3 shrink-0" aria-hidden />
              <span className="font-semibold">{KIND_LABEL[e.kind]}</span>
              <span className="truncate font-mono text-[11px] opacity-80">{shorten(e.raw)}</span>
              {e.reason && <span className="shrink-0 no-underline">{e.duplicate ? "duplicate" : e.reason}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
