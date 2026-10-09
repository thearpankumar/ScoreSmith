"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, History, Loader2, RefreshCw } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { listActivity, type ActivityEntry } from "@/lib/collab-client";
import { timeAgo } from "@/lib/time";
import { formatDateTime } from "@/lib/utils";

const POLL_MS = 20_000;

/**
 * The editing log of a SHARED chart: who changed what and when (KPI / weight / guideline edits, versions, evaluation
 * runs and outcomes, collaborators joining and leaving), newest first, paginated ("Show older"). It also polls the
 * newest entry: when somebody ELSE changed the chart since this page was loaded, a banner offers a refresh, so two
 * collaborators do not keep editing stale data (the server additionally rejects stale saves with a 409).
 */
export function ActivityPanel({ scorecardId, currentUserName }: { scorecardId: string; currentUserName?: string }) {
  const router = useRouter();
  const [items, setItems] = useState<ActivityEntry[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState<string | null>(null);
  const newest = useRef<number | null>(null);

  const load = useCallback(
    async (before: number | null) => {
      try {
        const page = await listActivity(scorecardId, { before, limit: 15 });
        setItems((prev) => {
          const seen = new Set(prev.map((a) => a.id));
          return before ? [...prev, ...page.items.filter((a) => !seen.has(a.id))] : page.items;
        });
        setNext(page.nextBefore);
        if (!before) newest.current = page.items[0]?.id ?? null;
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load the editing log.");
      } finally {
        setLoading(false);
        setMore(false);
      }
    },
    [scorecardId],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  useEffect(() => {
    const id = setInterval(async () => {
      if (document.hidden || newest.current === null) return;
      try {
        const page = await listActivity(scorecardId, { limit: 1 });
        const top = page.items[0];
        if (top && top.id > (newest.current ?? 0)) {
          // Own actions are not "stale" (the page already reflects them via router.refresh).
          if (top.actorName !== currentUserName) setStale(top.actorName);
          newest.current = top.id;
          void load(null);
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [scorecardId, currentUserName, load]);

  return (
    <GlassCard elevation={1} className="p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-base font-semibold text-ink">
          <History className="size-4" aria-hidden /> Editing log
        </h2>
        <span className="text-xs text-ink-muted">Shared chart · who changed what, and when</span>
      </div>

      {stale && (
        <div role="status" className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-lemon-soft px-3 py-2 text-sm text-lemon-ink">
          <span>{stale} changed this chart. Refresh to see the latest version before you edit.</span>
          <Button
            type="button"
            size="sm"
            className="min-h-11 sm:min-h-8"
            onClick={() => {
              setStale(null);
              router.refresh();
            }}
          >
            <RefreshCw aria-hidden /> Refresh
          </Button>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-3 flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}
      {loading ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-ink-muted">
          <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading…
        </p>
      ) : items.length === 0 ? (
        <p className="mt-3 text-sm text-ink-muted">No edits recorded yet.</p>
      ) : (
        <ol className="mt-3 flex max-h-96 flex-col overflow-y-auto" aria-label="Editing log entries" data-testid="activity-log">
          {items.map((a) => (
            <li key={a.id} className="flex gap-3 border-b border-hairline py-2 last:border-b-0">
              <span className="mt-1.5 size-2 shrink-0 rounded-full bg-ink/70" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="break-words text-sm text-ink">
                  <span className="font-semibold">{a.actorName}</span> · {a.summary}
                </p>
                <p className="text-xs text-ink-muted" title={formatDateTime(a.createdAt)} suppressHydrationWarning>
                  {timeAgo(a.createdAt)} · {formatDateTime(a.createdAt)}
                </p>
              </div>
            </li>
          ))}
          {next && (
            <li className="py-2 text-center">
              <Button
                type="button"
                variant="ghost"
                className="min-h-11"
                disabled={more}
                onClick={() => {
                  setMore(true);
                  void load(next);
                }}
              >
                {more && <Loader2 className="animate-spin" aria-hidden />} Show older
              </Button>
            </li>
          )}
        </ol>
      )}
    </GlassCard>
  );
}
