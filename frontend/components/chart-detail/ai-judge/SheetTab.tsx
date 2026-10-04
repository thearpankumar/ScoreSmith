"use client";

import { AlertTriangle, FileSpreadsheet, Loader2, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { SHEET_EXTENSIONS } from "@/lib/ai-upload";
import { cn } from "@/lib/utils";
import { Dropzone } from "./Dropzone";
import { UploadRow } from "./UploadRow";
import type { useBatchSheet } from "./use-batch-sheet";
import { sheetRowIssue } from "./use-batch-sheet";
import type { useFileUploads } from "./use-file-uploads";

type Uploads = ReturnType<typeof useFileUploads>;
type Sheet = ReturnType<typeof useBatchSheet>;

export function SheetTab({ uploads, sheet, disabled }: { uploads: Uploads; sheet: Sheet; disabled?: boolean }) {
  const file = uploads.items[0];
  const { rows, skipped, columns, parsing, error, updateRow, removeRow } = sheet;
  const mapped = Object.entries(columns).filter(([, v]) => v);

  return (
    <div className="flex flex-col gap-4">
      <Dropzone
        accept={SHEET_EXTENSIONS.map((e) => `.${e}`).join(",")}
        onFiles={uploads.addFiles}
        disabled={disabled}
        compact={!!file}
        icon={<FileSpreadsheet className="size-6" aria-hidden />}
        title={file ? "Replace the spreadsheet" : "Drop a spreadsheet here, or click to browse"}
        hint="An .xlsx or .csv with one submission per row: email, name and a Google Drive link. We detect the columns for you and let you review every row before anything is queued."
      />

      {uploads.rejections.length > 0 && (
        <p role="alert" className="rounded-xl bg-[var(--rag-poor)]/10 p-3 text-xs text-[var(--rag-poor)]">
          {uploads.rejections.join(" ")}
        </p>
      )}

      {file && (
        <ul aria-label="Spreadsheet upload">
          <UploadRow item={file} onCancel={uploads.cancel} onRetry={uploads.retry} onRemove={uploads.remove} />
        </ul>
      )}

      {parsing && (
        <p className="flex items-center gap-2 text-sm text-ink-muted" role="status">
          <Loader2 className="size-4 animate-spin" aria-hidden /> Reading the spreadsheet and matching columns…
        </p>
      )}
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden /> {error}
        </p>
      )}

      {file?.status === "done" && !parsing && !error && (
        <section aria-label="Preview of parsed submissions" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-ink">
              {rows.length} submission{rows.length === 1 ? "" : "s"} found
            </h3>
            {mapped.length > 0 && (
              <p className="text-xs text-ink-muted">
                Columns used: {mapped.map(([k, v]) => `${k.replace("_", " ")} = “${v}”`).join(", ")}
              </p>
            )}
          </div>

          {rows.length === 0 && (
            <p className="rounded-xl bg-black/5 p-3 text-sm text-ink-muted">
              No usable rows were found. Check the spreadsheet has a Google Drive link column.
            </p>
          )}

          <ul className="flex flex-col gap-2">
            {rows.map((row, i) => {
              const issue = sheetRowIssue(row);
              return (
                <li
                  key={row.key}
                  className={cn(
                    "rounded-xl border bg-solid p-3",
                    issue ? "border-[var(--rag-poor)]/40" : "border-hairline",
                  )}
                >
                  <div className="grid grid-cols-1 items-start gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1.4fr)_auto]">
                    <label className="flex flex-col gap-1 text-[11px] font-medium text-ink-muted">
                      Email
                      <Input
                        type="email"
                        value={row.email}
                        onChange={(e) => updateRow(row.key, { email: e.target.value })}
                        disabled={disabled}
                        aria-label={`Email, row ${i + 1}`}
                        className="h-9 text-sm font-normal text-ink"
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[11px] font-medium text-ink-muted">
                      Name
                      <Input
                        value={row.name}
                        onChange={(e) => updateRow(row.key, { name: e.target.value })}
                        disabled={disabled}
                        aria-label={`Name, row ${i + 1}`}
                        className="h-9 text-sm font-normal text-ink"
                      />
                    </label>
                    <label className="flex flex-col gap-1 text-[11px] font-medium text-ink-muted">
                      Google Drive link
                      <Input
                        value={row.driveUrl}
                        onChange={(e) => updateRow(row.key, { driveUrl: e.target.value })}
                        disabled={disabled}
                        aria-label={`Google Drive link, row ${i + 1}`}
                        aria-invalid={!!issue}
                        className="h-9 font-mono text-xs font-normal text-ink"
                      />
                    </label>
                    <button
                      type="button"
                      onClick={() => removeRow(row.key)}
                      disabled={disabled}
                      aria-label={`Remove row ${i + 1}`}
                      title="Remove row"
                      className="justify-self-end rounded-full p-2 text-ink-muted hover:bg-[var(--rag-poor)]/10 hover:text-[var(--rag-poor)] focus-visible:outline-2 focus-visible:outline-[var(--focus)] md:mt-5"
                    >
                      <Trash2 className="size-4" aria-hidden />
                    </button>
                  </div>
                  {(issue || row.warnings.length > 0) && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {issue && (
                        <Badge variant="muted" className="bg-[var(--rag-poor)]/10 text-[var(--rag-poor)]">
                          {issue}
                        </Badge>
                      )}
                      {row.warnings.map((w) => (
                        <Badge key={w} variant="soft">
                          {w}
                        </Badge>
                      ))}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>

          {skipped.length > 0 && (
            <details className="rounded-xl border border-hairline bg-solid/60 p-3 text-xs text-ink-muted">
              <summary className="cursor-pointer font-medium text-ink">
                {skipped.length} row{skipped.length === 1 ? "" : "s"} skipped
              </summary>
              <ul className="mt-2 space-y-0.5">
                {skipped.map((s) => (
                  <li key={s.rowIndex}>
                    Row {s.rowIndex}: {s.reason}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </section>
      )}
    </div>
  );
}
