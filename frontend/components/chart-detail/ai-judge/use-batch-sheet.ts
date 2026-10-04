"use client";

import { useCallback, useEffect, useState } from "react";

import { parseBatchSheet } from "@/lib/api-client";
import { classifyDriveLink } from "@/lib/ai-upload";
import type { BatchSheetParse } from "@/lib/types";

export interface SheetRow {
  key: string;
  rowIndex: number;
  email: string;
  name: string;
  driveUrl: string;
  warnings: string[];
}

export function sheetRowIssue(row: SheetRow): string | null {
  if (!row.driveUrl.trim()) return "Missing Google Drive link.";
  const c = classifyDriveLink(row.driveUrl);
  return c.kind === "invalid" ? (c.reason ?? "Invalid link.") : null;
}

/**
 * Once the batch sheet (purpose `batch_sheet`) has finished uploading, asks the backend to parse it
 * (`POST /evaluations/ai/batches/parse`) and keeps the editable preview rows.
 */
export function useBatchSheet(sheetKey: string | null) {
  const [rows, setRows] = useState<SheetRow[]>([]);
  const [skipped, setSkipped] = useState<BatchSheetParse["skipped"]>([]);
  const [columns, setColumns] = useState<BatchSheetParse["columns"]>({});
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setRows([]);
    setSkipped([]);
    setColumns({});
    setError(null);
    if (!sheetKey) {
      setParsing(false);
      return;
    }
    const ac = new AbortController();
    setParsing(true);
    parseBatchSheet(sheetKey, ac.signal)
      .then((res) => {
        setRows(
          res.rows.map((r) => ({
            key: `r${r.rowIndex}`,
            rowIndex: r.rowIndex,
            email: r.email ?? "",
            name: r.name ?? "",
            driveUrl: r.driveUrl ?? "",
            warnings: r.warnings,
          })),
        );
        setSkipped(res.skipped);
        setColumns(res.columns);
        setParsing(false);
      })
      .catch((err) => {
        if (ac.signal.aborted) return;
        setError(err instanceof Error ? err.message : "Could not read this spreadsheet.");
        setParsing(false);
      });
    return () => ac.abort();
  }, [sheetKey]);

  const updateRow = useCallback(
    (key: string, patch: Partial<Pick<SheetRow, "email" | "name" | "driveUrl">>) =>
      setRows((prev) => prev.map((r) => (r.key === key ? { ...r, ...patch } : r))),
    [],
  );
  const removeRow = useCallback((key: string) => setRows((prev) => prev.filter((r) => r.key !== key)), []);

  return { rows, skipped, columns, parsing, error, updateRow, removeRow };
}
