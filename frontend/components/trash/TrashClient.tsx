"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, RotateCcw, Trash2, Users } from "lucide-react";

import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  emptyTrash,
  listTrash,
  purgeFromTrash,
  restoreFromTrash,
  TRASH_CHANGED_EVENT,
  type TrashedChart,
} from "@/lib/trash-client";
import {
  chartsLabel,
  clickRow,
  daysLeftLabel,
  EMPTY_SELECTION,
  headerState,
  isSelected,
  isUrgent,
  previewNames,
  pruneIds,
  selectedIds,
  toggleAll,
  type BulkSelection,
} from "@/lib/trash-selection";
import { cn, formatDate } from "@/lib/utils";

type Confirm = "purge" | "empty" | null;
type Notice = { kind: "success" | "error"; message: string } | null;

/**
 * The owner's chart trash: a list with a checkbox per row, a header "select all" (indeterminate when partial),
 * shift+click range selection and a sticky selection bar (Restore, Delete permanently, Empty trash). Mirrors the
 * Evaluations list's selection model (lib/eval-bulk.ts through lib/trash-selection.ts).
 */
export function TrashClient({ retentionDays = 30 }: { retentionDays?: number }) {
  const [items, setItems] = useState<TrashedChart[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selection, setSelection] = useState<BulkSelection>(EMPTY_SELECTION);
  const [busy, setBusy] = useState<null | "restore" | "purge" | "empty">(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [announce, setAnnounce] = useState("");
  const anchor = useRef<string | null>(null);
  const headerRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoadError(null);
    try {
      const rows = await listTrash(signal);
      setItems(rows);
      setSelection((prev) => pruneIds(prev, new Set(rows.map((r) => r.id))));
    } catch (err) {
      if (signal?.aborted) return;
      setLoadError(err instanceof Error ? err.message : "Couldn't load the trash.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const ordered = useMemo(() => (items ?? []).map((i) => i.id), [items]);
  const chosen = useMemo(() => selectedIds(selection, ordered), [selection, ordered]);
  const hState = headerState(selection, ordered);
  const nSelected = chosen.length;
  const working = busy !== null;

  useEffect(() => {
    if (headerRef.current) headerRef.current.indeterminate = hState === "some";
  }, [hState]);

  useEffect(() => {
    if (nSelected > 0) setAnnounce(`${chartsLabel(nSelected)} selected`);
    else setAnnounce("Selection cleared");
  }, [nSelected]);

  function onRowToggle(id: string, shift: boolean) {
    setSelection((prev) => clickRow(prev, ordered, id, shift, anchor.current));
    anchor.current = id;
  }

  function removeFromList(done: readonly string[]) {
    const gone = new Set(done);
    setItems((prev) => (prev ?? []).filter((i) => !gone.has(i.id)));
    setSelection((prev) => pruneIds(prev, new Set(ordered.filter((id) => !gone.has(id)))));
    anchor.current = null;
    window.dispatchEvent(new CustomEvent(TRASH_CHANGED_EVENT));
  }

  async function run(kind: "restore" | "purge" | "empty") {
    if (working) return;
    setBusy(kind);
    setNotice(null);
    try {
      const result =
        kind === "restore"
          ? await restoreFromTrash(chosen)
          : kind === "purge"
            ? await purgeFromTrash(chosen)
            : await emptyTrash();
      removeFromList(result.done);
      const n = result.done.length;
      const message =
        kind === "restore"
          ? `Restored ${chartsLabel(n)}. ${n === 1 ? "It is" : "They are"} back in the Charts library.`
          : kind === "purge"
            ? `Permanently deleted ${chartsLabel(n)}.`
            : n === 0
              ? "The trash was already empty."
              : `Trash emptied: ${chartsLabel(n)} permanently deleted.`;
      setNotice({ kind: "success", message });
      setAnnounce(message);
      setConfirm(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Something went wrong. Try again.";
      setNotice({ kind: "error", message });
      setAnnounce(message);
      setConfirm(null);
      void load(); // the server may have processed part of it; show the truth
    } finally {
      setBusy(null);
    }
  }

  const names = useMemo(() => {
    const byId = new Map((items ?? []).map((i) => [i.id, i.name]));
    return chosen.map((id) => byId.get(id) ?? id);
  }, [items, chosen]);
  const preview = previewNames(names);
  const total = items?.length ?? 0;

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-ink-muted">
        Deleted charts stay here for {retentionDays} days. Restore them to bring back their versions, evaluations and
        sharing, or delete them permanently. After {retentionDays} days they are removed automatically.
      </p>

      <p role="status" aria-live="polite" className="sr-only">
        {announce}
      </p>

      {notice && (
        <p
          role={notice.kind === "error" ? "alert" : "status"}
          className={cn(
            "flex items-start gap-1.5 text-sm",
            notice.kind === "error" ? "text-[var(--rag-poor)]" : "text-ink",
          )}
        >
          {notice.kind === "error" ? (
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          ) : (
            <CheckCircle2 className="mt-0.5 size-4 shrink-0" aria-hidden />
          )}
          <span className="min-w-0 break-words">{notice.message}</span>
        </p>
      )}

      {items === null && !loadError && (
        <SolidPanel className="flex min-h-24 items-center justify-center gap-2 p-6 text-sm text-ink-muted" aria-busy="true">
          <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden />
          Loading the trash…
        </SolidPanel>
      )}

      {loadError && (
        <SolidPanel className="flex flex-col items-start gap-3 p-6">
          <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            {loadError}
          </p>
          <Button type="button" variant="outline" className="min-h-11" onClick={() => void load()}>
            <RefreshCw aria-hidden /> Try again
          </Button>
        </SolidPanel>
      )}

      {items !== null && items.length === 0 && !loadError && (
        <SolidPanel className="flex flex-col items-center gap-2 p-10 text-center">
          <Trash2 className="size-8 text-ink-muted" aria-hidden />
          <p className="text-sm font-medium text-ink">The trash is empty</p>
          <p className="max-w-sm text-xs text-ink-muted">Charts you delete show up here so you can restore them.</p>
        </SolidPanel>
      )}

      {items !== null && items.length > 0 && (
        <SolidPanel className="overflow-hidden p-0">
          <div className="flex min-h-12 flex-wrap items-center gap-x-3 gap-y-1 border-b border-hairline px-3 py-1">
            <label className="flex min-h-11 cursor-pointer items-center gap-3 pr-2 text-sm font-medium text-ink">
              <input
                ref={headerRef}
                type="checkbox"
                checked={hState === "all"}
                onChange={() => setSelection((prev) => toggleAll(prev, ordered))}
                disabled={working}
                aria-label={hState === "all" ? "Deselect all charts" : "Select all charts"}
                className="size-5 accent-[var(--ink)]"
              />
              <span aria-hidden>{hState === "all" ? "Deselect all" : "Select all"}</span>
            </label>
            <span className="text-xs text-ink-muted tabular-nums">{chartsLabel(total)} in the trash</span>
            <Button
              type="button"
              variant="ghost"
              className="ml-auto min-h-11 text-[var(--rag-poor)] hover:bg-[var(--rag-poor)]/10"
              onClick={() => setConfirm("empty")}
              disabled={working}
            >
              <Trash2 aria-hidden /> Empty trash
            </Button>
          </div>
          <ul className="divide-y divide-hairline" data-testid="trash-list">
            {items.map((item) => (
              <TrashRow
                key={item.id}
                item={item}
                selected={isSelected(selection, item.id)}
                disabled={working}
                onToggle={onRowToggle}
              />
            ))}
          </ul>
        </SolidPanel>
      )}

      {nSelected > 0 && (
        <div
          role="region"
          aria-label="Trash actions"
          aria-busy={working}
          // Below `md` the fixed bottom nav sits at the bottom of the viewport, so lift the bar clear of it.
          className="sticky bottom-[calc(5.5rem+env(safe-area-inset-bottom))] z-30 mx-auto flex w-full max-w-3xl flex-col gap-2 rounded-2xl border border-hairline bg-ink px-4 py-3 text-sm text-bg shadow-lg md:bottom-4"
        >
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <p className="min-w-0 font-medium tabular-nums">{nSelected.toLocaleString()} selected</p>
            <div className="flex w-full flex-wrap items-center justify-end gap-2 sm:ml-auto sm:w-auto">
              <Button
                type="button"
                variant="ghost"
                className="min-h-11 text-bg hover:bg-white/10 hover:text-bg"
                onClick={() => setSelection(EMPTY_SELECTION)}
                disabled={working}
              >
                Clear
              </Button>
              <Button type="button" className="min-h-11" onClick={() => void run("restore")} disabled={working}>
                {busy === "restore" ? <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden /> : <RotateCcw aria-hidden />}
                Restore ({nSelected})
              </Button>
              <Button type="button" variant="destructive" className="min-h-11" onClick={() => setConfirm("purge")} disabled={working}>
                <Trash2 aria-hidden /> Delete permanently ({nSelected})
              </Button>
            </div>
          </div>
        </div>
      )}

      <Dialog open={confirm !== null} onOpenChange={(open) => !open && !working && setConfirm(null)}>
        <DialogContent
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            cancelRef.current?.focus(); // Cancel is the default so Enter/Space can never confirm a permanent delete
          }}
        >
          <DialogHeader>
            <DialogTitle>
              {confirm === "empty" ? `Empty the trash (${chartsLabel(total)})?` : `Delete ${chartsLabel(nSelected)} permanently?`}
            </DialogTitle>
            <DialogDescription>
              {confirm === "empty"
                ? "Every chart in the trash is deleted for good, together with its versions and evaluations. This can't be undone."
                : "These charts are deleted for good, together with their versions and evaluations. This can't be undone."}
            </DialogDescription>
          </DialogHeader>
          {confirm === "purge" && (
            <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-sm text-ink">
              {preview.shown.map((n, i) => (
                <li key={i} className="break-words">
                  {n}
                </li>
              ))}
              {preview.more > 0 && <li className="list-none text-ink-muted">…and {preview.more} more</li>}
            </ul>
          )}
          <DialogFooter>
            <Button ref={cancelRef} type="button" variant="ghost" className="min-h-11" onClick={() => setConfirm(null)} disabled={working}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              className="min-h-11"
              onClick={() => void run(confirm === "empty" ? "empty" : "purge")}
              disabled={working}
            >
              {working && <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />}
              {confirm === "empty" ? "Empty trash" : "Delete permanently"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function TrashRow({
  item,
  selected,
  disabled,
  onToggle,
}: {
  item: TrashedChart;
  selected: boolean;
  disabled: boolean;
  onToggle: (id: string, shift: boolean) => void;
}) {
  const urgent = isUrgent(item.daysLeft);
  return (
    <li
      data-testid="trash-row"
      onClick={(e) => !disabled && onToggle(item.id, e.shiftKey)}
      className={cn(
        "flex min-h-[4.25rem] cursor-pointer select-none items-center gap-3 border-l-4 border-l-transparent px-3 py-2 transition-colors hover:bg-bg",
        selected && "border-l-[var(--ink)] bg-[var(--lemon-soft)]",
      )}
    >
      {/* 44px tap target around the 20px checkbox */}
      <span className="-ml-1 flex size-11 shrink-0 items-center justify-center">
        <input
          type="checkbox"
          checked={selected}
          disabled={disabled}
          onChange={() => undefined}
          onClick={(e) => {
            e.stopPropagation();
            onToggle(item.id, e.shiftKey);
          }}
          aria-label={`Select “${item.name}”`}
          className="size-5 accent-[var(--ink)] disabled:cursor-not-allowed"
        />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <p className="min-w-0 break-words text-sm font-medium text-ink">{item.name}</p>
          {item.domain && (
            <Badge variant="muted" className="shrink-0">
              {item.domain}
            </Badge>
          )}
          {item.collaboratorCount > 0 && (
            <Badge variant="soft" className="shrink-0" title="Shared with collaborators, who lose access until it is restored">
              <Users className="size-3" aria-hidden /> Shared
            </Badge>
          )}
        </div>
        <p className="mt-0.5 text-xs text-ink-muted" suppressHydrationWarning>
          Moved to trash {formatDate(item.deletedAt)} · {item.evaluationCount}{" "}
          {item.evaluationCount === 1 ? "evaluation" : "evaluations"}
        </p>
      </div>
      <span
        className={cn(
          "shrink-0 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-medium tabular-nums",
          urgent ? "border-[var(--rag-poor)] text-[var(--rag-poor)]" : "border-hairline text-ink-muted",
        )}
      >
        {daysLeftLabel(item.daysLeft)}
      </span>
    </li>
  );
}
