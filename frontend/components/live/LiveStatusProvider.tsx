"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Bell, CheckCircle2, X } from "lucide-react";

import {
  fetchUnreadCount,
  getMySlots,
  listNotifications,
  type MySlots,
  type NotificationItem,
} from "@/lib/collab-client";

/**
 * Per-user live status for the whole signed-in app: the unread-notification count and the user's busy slots (one
 * running evaluation job, one running chat turn).
 *
 * Delivery is POLLING with conditional requests (no per-process state anywhere): `GET /notifications/unread-count`
 * carries an ETag, so an unchanged inbox answers `304` with no body; the slots endpoint is polled at the same beat
 * (faster while a job is running). Polling pauses while the tab is hidden and resumes at once on focus. Components
 * that just changed something (started a job, sent a chat message, answered an invite) call `refreshNow()`.
 * New notifications are announced through an `aria-live` region and a short toast.
 */

export const LIVE_REFRESH_EVENT = "qs:live-refresh";

/** Ask the provider to poll right away (from anywhere, no context needed). */
export function requestLiveRefresh(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(LIVE_REFRESH_EVENT));
}

export const SHOW_TOAST_EVENT = "qs:toast";

export interface ToastRequest {
  title: string;
  body?: string | null;
  /** "success" shows a check mark; the default is the bell used for notifications. */
  kind?: "info" | "success";
}

/** Pop a short toast from anywhere (no context needed), e.g. after a successful export. Does nothing on the server. */
export function showToast(toast: ToastRequest): void {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent<ToastRequest>(SHOW_TOAST_EVENT, { detail: toast }));
}

interface LiveStatus {
  unread: number;
  slots: MySlots;
  /** True once the first poll answered (so buttons are not disabled/enabled spuriously before we know). */
  ready: boolean;
  refreshNow: () => void;
  /** Local adjustment after the panel marked things read, before the next poll. */
  setUnread: (n: number) => void;
}

const EMPTY: MySlots = { job: null, chat: null };
const Ctx = createContext<LiveStatus>({
  unread: 0,
  slots: EMPTY,
  ready: false,
  refreshNow: () => undefined,
  setUnread: () => undefined,
});

export function useLiveStatus(): LiveStatus {
  return useContext(Ctx);
}

interface Toast {
  id: string;
  title: string;
  body: string | null;
  link: string | null;
  kind?: "info" | "success";
}

const IDLE_MS = 15_000;
const BUSY_MS = 5_000;

export function LiveStatusProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [unread, setUnread] = useState(0);
  const [slots, setSlots] = useState<MySlots>(EMPTY);
  const [ready, setReady] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const etag = useRef<string | null>(null);
  const lastUnread = useRef<number | null>(null);
  const lastJob = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);
  const busy = useRef(false);
  const stopped = useRef(false);

  const announce = useCallback(async (increase: number) => {
    try {
      const page = await listNotifications({ limit: Math.min(5, Math.max(1, increase)), unreadOnly: true });
      const fresh: NotificationItem[] = page.items.slice(0, Math.min(3, increase));
      if (fresh.length === 0) return;
      setAnnouncement(fresh.map((n) => n.title).join(". "));
      setToasts((prev) => [
        ...fresh.map((n) => ({ id: n.id, title: n.title, body: n.body, link: n.link })),
        ...prev.filter((t) => !fresh.some((n) => n.id === t.id)),
      ].slice(0, 3));
      for (const n of fresh) setTimeout(() => setToasts((p) => p.filter((t) => t.id !== n.id)), 8000);
    } catch {
      /* best effort */
    }
  }, []);

  const poll = useCallback(async () => {
    if (inFlight.current || stopped.current) return;
    inFlight.current = true;
    try {
      const [count, mine] = await Promise.all([
        fetchUnreadCount(etag.current).catch(() => null),
        getMySlots().catch(() => null),
      ]);
      if (count) {
        etag.current = count.etag;
        if (count.changed) {
          const previous = lastUnread.current;
          setUnread(count.unread);
          if (previous !== null && count.unread > previous) void announce(count.unread - previous);
          lastUnread.current = count.unread;
        }
      }
      if (mine) {
        setSlots(mine);
        busy.current = Boolean(mine.job || mine.chat);
        // A running job just finished: the server-rendered pages (lists, badges) are stale - refresh them.
        const jobId = mine.job?.evaluationId ?? null;
        if (lastJob.current && !jobId) router.refresh();
        lastJob.current = jobId;
      }
      setReady(true);
    } finally {
      inFlight.current = false;
    }
  }, [announce, router]);

  const schedule = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    if (stopped.current) return;
    timer.current = setTimeout(
      async () => {
        if (!document.hidden) await poll();
        schedule();
      },
      busy.current ? BUSY_MS : IDLE_MS,
    );
  }, [poll]);

  const refreshNow = useCallback(() => {
    void poll().then(schedule);
  }, [poll, schedule]);

  // Toasts requested by other components through showToast().
  useEffect(() => {
    const onToast = (e: Event) => {
      const t = (e as CustomEvent<ToastRequest>).detail;
      if (!t?.title) return;
      const id = `local-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setAnnouncement(t.body ? `${t.title}. ${t.body}` : t.title);
      setToasts((prev) => [{ id, title: t.title, body: t.body ?? null, link: null, kind: t.kind }, ...prev].slice(0, 3));
      setTimeout(() => setToasts((p) => p.filter((x) => x.id !== id)), 7000);
    };
    window.addEventListener(SHOW_TOAST_EVENT, onToast);
    return () => window.removeEventListener(SHOW_TOAST_EVENT, onToast);
  }, []);

  useEffect(() => {
    stopped.current = false;
    void poll().then(schedule);
    const onVisible = () => {
      if (!document.hidden) refreshNow();
    };
    window.addEventListener(LIVE_REFRESH_EVENT, refreshNow);
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped.current = true;
      if (timer.current) clearTimeout(timer.current);
      window.removeEventListener(LIVE_REFRESH_EVENT, refreshNow);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [poll, schedule, refreshNow]);

  const value = useMemo<LiveStatus>(
    () => ({
      unread,
      slots,
      ready,
      refreshNow,
      setUnread: (n: number) => {
        setUnread(n);
        lastUnread.current = n;
      },
    }),
    [unread, slots, ready, refreshNow],
  );

  return (
    <Ctx.Provider value={value}>
      {children}
      {/* Screen readers hear new notifications; sighted users get a short toast. */}
      <div aria-live="polite" role="status" className="sr-only">
        {announcement}
      </div>
      <div className="pointer-events-none fixed inset-x-3 bottom-24 z-[60] flex flex-col items-end gap-2 md:inset-x-auto md:bottom-6 md:right-6">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="glass-strong pointer-events-auto flex w-full max-w-sm items-start gap-2.5 rounded-2xl p-3 shadow-lg motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2"
          >
            {t.kind === "success" ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ink" aria-hidden />
            ) : (
              <Bell className="mt-0.5 size-4 shrink-0 text-ink" aria-hidden />
            )}
            <button
              type="button"
              className="min-w-0 flex-1 text-left"
              onClick={() => {
                setToasts((p) => p.filter((x) => x.id !== t.id));
                if (t.link) router.push(t.link);
              }}
            >
              <span className="block truncate text-sm font-semibold text-ink">{t.title}</span>
              {t.body && <span className="block truncate text-xs text-ink-muted">{t.body}</span>}
            </button>
            <button
              type="button"
              aria-label="Dismiss notification"
              className="-m-1 flex size-8 shrink-0 items-center justify-center rounded-full text-ink-muted hover:bg-white/70 hover:text-ink focus-visible:outline-2 focus-visible:outline-[var(--focus)]"
              onClick={() => setToasts((p) => p.filter((x) => x.id !== t.id))}
            >
              <X className="size-4" aria-hidden />
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
