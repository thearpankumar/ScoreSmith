"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileText,
  Pencil,
  RotateCcw,
  Send,
  Sparkles,
  Undo2,
} from "lucide-react";

import { useChatSessions } from "./ChatSessionsContext";
import { MessageBubble } from "./MessageBubble";
import { ClarifyingQuestionCard } from "./ClarifyingQuestionCard";
import { SimilarScorecardSuggestion } from "./SimilarScorecardSuggestion";
import { LivePreviewPanel } from "./LivePreviewPanel";
import { TurnTraceCard } from "./TurnTraceCard";
import { ChartStateNotice } from "./ChartStateNotice";
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";
import { GlassCard } from "@/components/design-system/GlassCard";
import { ChatShareButton } from "@/components/sharing/ChatShareDialog";
import { requestLiveRefresh, useLiveStatus } from "@/components/live/LiveStatusProvider";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import type { PanelImperativeHandle } from "@/components/ui/resizable";
import {
  ApiError,
  BedrockUnavailableError,
  cancelChatTurn,
  getChatSession,
  startChatTurn,
  watchChatTurn,
} from "@/lib/api-client";
import type { ChatTurnFailure, ChatTurnSnapshot } from "@/lib/api-client";
import {
  dedupeRetriedMessages,
  describeDraftEdits,
  draftKpiCounts,
  draftSignature,
  mergeInterimDraft,
  sameEvents,
  stripTrailingQuestion,
  turnFailureMessage,
} from "@/lib/chat-turn-state";
import { useMediaQuery } from "@/lib/useMediaQuery";
import { useUnsavedChangesGuard } from "@/lib/useUnsavedChangesGuard";
import { cn } from "@/lib/utils";
import type {
  ChatMessage,
  ChatSession,
  ChatTurnEvent,
  SavedScorecardRef,
  ScorecardDraft,
  SimilarScorecardSuggestion as Suggestion,
} from "@/lib/types";

/** localStorage key for the user's drag-resized live-preview panel width (px) — a
 * per-viewer UI preference, not app data, so it's persisted client-side only. */
const PREVIEW_WIDTH_STORAGE_KEY = "qs.livePreviewPanelWidth";

function describeTurnFailure(failure: ChatTurnFailure): string {
  return turnFailureMessage(failure.code);
}

function lastIsAssistant(messages: ChatMessage[]): boolean {
  return messages.length > 0 && messages[messages.length - 1].role === "assistant";
}

/** The user's last message when the transcript ends on it unanswered after a failed turn — what
 * "Try again" re-sends (null when there is nothing to retry, e.g. a failed FIRST turn: the backend
 * only persists a first message once its turn succeeds). */
function unansweredUserText(messages: ChatMessage[], failure: ChatTurnFailure | undefined): string | null {
  if (!failure || messages.length === 0) return null;
  const last = messages[messages.length - 1];
  return last.role === "user" ? last.content : null;
}

export function ChatWorkspace({
  session,
  initialMessages,
  initialDraft,
  initialPrompt,
  initialSavedScorecardId,
  initialTurnInProgress,
  initialTurnEvents,
  initialTurnError,
}: {
  session: ChatSession;
  initialMessages: ChatMessage[];
  initialDraft: ScorecardDraft;
  /** When present (e.g. arriving from the Home prompt box), auto-sent as the first user turn. */
  initialPrompt?: string;
  /** Set when this (resumed) session already saved a scorecard earlier. */
  initialSavedScorecardId?: string;
  /** Refresh-recovery (Part A): true when this session had a turn still running
   * server-side at page-load time (see backend `ChatSession.turn_in_progress` /
   * `GET /chat/sessions/{id}`) — the chat-turn HTTP request blocks synchronously for the
   * whole LangGraph run (including the up-to-90s multi-agent research fan-out), so a
   * page refresh mid-turn has nothing else to reconnect to. When true, a persistent
   * "still working" indicator is shown and this endpoint is polled until it clears. */
  initialTurnInProgress?: boolean;
  /** Refresh-recovery (Part B, live turn trace): the CURRENT/most recent turn's granular
   * event log at page-load time — see `getChatTurnEvents`/`TurnTraceCard`. Lets a
   * refreshed page show the in-progress trace immediately instead of starting blank. */
  initialTurnEvents?: ChatTurnEvent[];
  /** Why the last background turn failed / was cancelled / interrupted (shown with a retry when
   * the transcript ends on the user's own unanswered message). */
  initialTurnError?: ChatTurnFailure;
}) {
  const router = useRouter();
  // The most recent save from this chat — drives the persistent "Saved" banner. A
  // session that targets an existing scorecard ("Refine with assistant") saves as a new
  // version of it; so does a second save from a chat that already saved once.
  const [saved, setSaved] = useState<SavedScorecardRef | null>(
    initialSavedScorecardId
      ? { id: initialSavedScorecardId, name: initialDraft.name ?? "your scorecard", asNewVersion: false }
      : null,
  );
  const isRefineSession = !!session.targetScorecardId && !initialSavedScorecardId;
  const [refineTargetName, setRefineTargetName] = useState(initialDraft.name);
  // Issue 1 (see task notes): the sidebar session list now lives in ChatSessionsContext
  // (see the root app/layout.tsx), not local state here. `upsertSessionTitle` below is
  // folded into the same polling this component already runs for turn_in_progress/
  // turn-events (see the two poll effects below and runAssistantTurn's own success
  // path), so a real, AI-generated (or just-changed) title appears in the sidebar
  // without a page reload.
  const {
    sessions,
    upsertSessionTitle: upsertSessionTitleInContext,
    removeSession,
    setDirty: setContextDirty,
    setSessionBusy,
  } = useChatSessions();
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  // `serverDraft` is the last draft the (authoritative) backend returned; `draft` is what
  // the live preview shows and lets the user edit inline. They differ exactly when the
  // user has made local edits that haven't been sent to the assistant yet.
  const [serverDraft, setServerDraft] = useState<ScorecardDraft>(initialDraft);
  const [draft, setDraft] = useState<ScorecardDraft>(initialDraft);
  const [lastFailedText, setLastFailedText] = useState<string | null>(() => unansweredUserText(initialMessages, initialTurnError));
  const [pending, setPending] = useState(false);
  const [composer, setComposer] = useState("");
  const [pendingSuggestion, setPendingSuggestion] = useState<Suggestion | null>(null);
  const [chatError, setChatError] = useState<string | null>(() =>
    initialTurnError && !initialTurnInProgress && !lastIsAssistant(initialMessages)
      ? turnFailureMessage(initialTurnError.code)
      : null,
  );
  const scrollRef = useRef<HTMLDivElement>(null);

  // The real chat backend is stateful server-side (a LangGraph session keyed by
  // chat_sessions.id) rather than driven by a client-tracked turn index. A brand-new
  // session starts as the literal id "new"; the backend assigns a real id on the first
  // successful turn, which we then adopt (and reflect in the URL) for every turn after.
  const [activeSessionId, setActiveSessionId] = useState(session.id);
  // Set (synchronously, in the SAME tick as `setActiveSessionId`) by `runAssistantTurn`
  // the moment it self-adopts a brand-new session's client-generated id, BEFORE its own
  // `router.replace` call — see the resync effect below, which uses this to tell "our own
  // in-flight self-adoption is still catching up to a `session` prop that hasn't arrived
  // yet" apart from "the user genuinely switched to a different session." Cleared back to
  // null once `session.id` catches up (resolved) so a later, unrelated external switch is
  // never mistaken for a still-pending self-adoption.
  const selfAdoptedSessionIdRef = useRef<string | null>(null);
  const initialPromptSentRef = useRef(false);
  // Always-current copy of `saved` for the (long-lived) turn watcher below, so a turn finishing
  // never reads a stale "was something already saved?" from its effect closure.
  const savedRef = useRef<SavedScorecardRef | null>(null);
  const startingTurnRef = useRef(false);

  /** Reflects a real (or changed) title into the sidebar's session list, live — see the
   * context docstring above. Adds a not-yet-listed session (a brand-new chat whose first
   * turn just resolved server-side, adopting a real id in place of "new") so it appears
   * immediately, without waiting for a full page/list reload. */
  function upsertSessionTitle(id: string, title: string | null | undefined) {
    upsertSessionTitleInContext(id, title, { userId: session.userId, targetScorecardId: session.targetScorecardId });
  }

  // Refresh-recovery (Part A): a turn already in flight server-side when this page
  // loaded/refreshed (see initialTurnInProgress prop docstring above).
  const [turnInProgress, setTurnInProgress] = useState(!!initialTurnInProgress);
  // Live turn trace (Part B): the CURRENT/most recent turn's granular event log — see
  // TurnTraceCard, which renders this in place of the old generic "Assistant is
  // thinking…" dots. Populated from the server on load (refresh-recovery) and kept live
  // by the two polling effects below (one for the refresh-recovery case, one for a turn
  // started locally in this tab).
  const [turnEvents, setTurnEvents] = useState<ChatTurnEvent[]>(initialTurnEvents ?? []);
  // The backend's INTERIM draft for the running turn (header fields, then the user's KPIs/weights,
  // then guidelines) — layered over the user's own draft for display only (see `displayDraft`), so
  // it can never clobber unsent edits and is simply dropped when the turn ends.
  const [interimDraft, setInterimDraft] = useState<ScorecardDraft | null>(null);
  // Wall-clock start of the running turn (this tab's, or "now" for one recovered after a refresh)
  // — drives the elapsed-time readout in the trace card.
  const [turnStartedAt, setTurnStartedAt] = useState<number | null>(null);
  // The last status polls failed (network blip / backend restarting): the watcher keeps retrying.
  const [connectionLost, setConnectionLost] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  // True only for a turn found already running at page load (started elsewhere / before a refresh)
  // — gates the explanatory "still working" banner and the elapsed-time origin.
  const [recoveredTurn, setRecoveredTurn] = useState(!!initialTurnInProgress);
  // Latest-value mirrors for the long-lived turn watcher (see below), which must not read stale
  // render closures.
  const messagesRef = useRef<ChatMessage[]>(initialMessages);
  const lastSentTextRef = useRef<string | null>(null);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  useEffect(() => {
    savedRef.current = saved;
  }, [saved]);
  // Sidebar "working…" badge for this chat while its turn runs (cleared when it ends or the chat is left).
  const busy = pending || turnInProgress;
  // One running chat turn per user across ALL their chats: while another chat of theirs is mid-turn the composer
  // is disabled with a link to it (the server enforces this too and answers 409 `user_chat_active`).
  const { slots } = useLiveStatus();
  const otherChatBusy = !!slots.chat && slots.chat.sessionId !== activeSessionId && !turnInProgress && !pending;
  const [busyElsewhere, setBusyElsewhere] = useState<string | null>(null);
  const elsewhereId = otherChatBusy ? slots.chat!.sessionId : busyElsewhere;
  useEffect(() => {
    if (busy) requestLiveRefresh();
  }, [busy]);
  useEffect(() => {
    if (!busy || activeSessionId === "new") return;
    setSessionBusy(activeSessionId, true);
    return () => setSessionBusy(activeSessionId, false);
  }, [busy, activeSessionId, setSessionBusy]);

  // STATE-BLEED FIX (see this task's final report for how this was found): `ChatWorkspace`
  // is rendered by `app/chat/[sessionId]/page.tsx` with NO `key` — deliberately, since
  // `runAssistantTurn` below adopts a brand-new session's real id via its own
  // `router.replace` WHILE that first turn is still in flight, and a `key` tied to the
  // session id would force-remount this component mid-send, orphaning that in-flight
  // turn's eventual response (its `setMessages`/`setDraft`/etc. calls would land on a
  // detached, unmounted instance instead of the fresh one). Every field above is seeded
  // via `useState(initial...)`, which only runs on a genuine mount — so without a `key`,
  // React reuses this SAME instance (same type, same tree position) when the user clicks
  // a DIFFERENT, already-existing chat session in the sidebar too, and none of those
  // fields would ever refresh: the whole workspace would keep showing the PREVIOUS
  // session's messages/draft/turn-in-progress status/composer text after the URL had
  // already changed to the new session (confirmed: this component's `initial*` props DO
  // change — `app/chat/[sessionId]/page.tsx` re-fetches per `sessionId` — but nothing
  // here reset to match before this fix).
  //
  // This effect is what makes a GENUINELY external session switch reset everything,
  // while leaving the self-triggered "new" -> real-id adoption alone: it compares the
  // incoming `session.id` PROP against this component's own `activeSessionId` STATE.
  //   - In sync (equal): either nothing has happened, or a pending self-adoption just
  //     resolved (the prop caught up) — clear the tracking ref and do nothing else.
  //   - Mismatched, but `activeSessionId` is the id WE just self-adopted
  //     (`selfAdoptedSessionIdRef`): `session.id` simply hasn't caught up yet via
  //     `runAssistantTurn`'s own `router.replace` — wait for it, don't reset anything
  //     (resetting here would orphan that in-flight turn's eventual response, which is
  //     the exact bug a blanket `key` on this component would reintroduce).
  //   - Mismatched otherwise: a genuinely external switch (a sidebar Link to a different,
  //     already-existing session) — reset every piece of per-session state to the fresh
  //     `initial*` props the new page fetched (mirrors the one-prop-id version of this
  //     same pattern already used by `EvaluationResultClientLoader`'s own
  //     `useEffect([scorecardId, evaluationId])`).
  // (Known, accepted edge case: clicking a different session within the same instant as
  // sending a brand-new chat's very first message — before that message's own
  // self-adoption round-trip resolves — can still race this; extremely narrow window,
  // not worth the extra complexity to close given how rare it is to hit in practice.)
  useEffect(() => {
    if (session.id === activeSessionId) {
      selfAdoptedSessionIdRef.current = null;
      return;
    }
    if (activeSessionId === selfAdoptedSessionIdRef.current) {
      return; // still waiting for the session prop to catch up to our own self-adoption
    }
    setActiveSessionId(session.id);
    setMessages(initialMessages);
    setServerDraft(initialDraft);
    setDraft(initialDraft);
    setRefineTargetName(initialDraft.name);
    setSaved(
      initialSavedScorecardId
        ? { id: initialSavedScorecardId, name: initialDraft.name ?? "your scorecard", asNewVersion: false }
        : null,
    );
    setTurnInProgress(!!initialTurnInProgress);
    setTurnEvents(initialTurnEvents ?? []);
    setInterimDraft(null);
    setTurnStartedAt(null);
    setRecoveredTurn(!!initialTurnInProgress);
    setConnectionLost(false);
    setCancelling(false);
    setComposer("");
    setPendingSuggestion(null);
    setChatError(
      initialTurnError && !initialTurnInProgress && !lastIsAssistant(initialMessages)
        ? turnFailureMessage(initialTurnError.code)
        : null,
    );
    setLastFailedText(unansweredUserText(initialMessages, initialTurnError));
    setPending(false);
    // Intentionally omits the initial* props/setters: this effect fires exactly on a
    // session.id/activeSessionId mismatch, and the initial* props are read fresh from
    // closure each run (they change together with session.id, since both come from the
    // same page-props update — no separate dep needed).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.id, activeSessionId]);

  // THE turn watcher — the only poller. While a background turn runs (this tab started it, or the
  // page was loaded/refreshed mid-turn) it polls the session state + trace (see `watchChatTurn`:
  // sequential, backoff on network errors, faster when the tab is visible) and:
  //   - mirrors the backend's INTERIM draft into `interimDraft` (the Live preview layers it over
  //     the user's own edits — see `mergeInterimDraft`), the trace into `turnEvents`, and the
  //     title into the sidebar, WITHOUT a manual refresh;
  //   - when the turn ends, reloads the authoritative transcript + draft and applies the outcome
  //     (reply / clarifying question / similar-suggestion card / saved scorecard / failure).
  // Aborted (cleanup) on unmount, session switch, or React StrictMode's dev double-invoke, so there
  // is never more than one watcher; a stale response can't land after cleanup (`ac.signal`).
  useEffect(() => {
    if (!turnInProgress || activeSessionId === "new") return;
    const ac = new AbortController();
    const sessionId = activeSessionId;
    let lastInterimSig = "";
    let startAdjusted = !recoveredTurn;
    setTurnStartedAt((t) => t ?? Date.now());
    (async () => {
      try {
        const final = await watchChatTurn(sessionId, {
          signal: ac.signal,
          onConnection: (ok) => setConnectionLost(!ok),
          onUpdate: (snap, events) => {
            if (ac.signal.aborted) return;
            if (events) setTurnEvents((prev) => (sameEvents(prev, events) ? prev : events));
            if (!startAdjusted && events && events.length > 0) {
              // A turn recovered after a refresh: measure elapsed time from its first trace event.
              startAdjusted = true;
              const first = Date.parse(events[0].createdAt);
              if (Number.isFinite(first)) setTurnStartedAt(Math.min(first, Date.now()));
            }
            upsertSessionTitle(sessionId, snap.title);
            const sig = draftSignature(snap.draft);
            if (sig !== lastInterimSig) {
              lastInterimSig = sig;
              setInterimDraft(snap.draft);
            }
          },
        });
        if (ac.signal.aborted) return;
        await finishTurn(final, ac.signal);
      } catch (err) {
        if (ac.signal.aborted || (err instanceof DOMException && err.name === "AbortError")) return;
        // Only a deleted session ends the watch with an error (network errors just retry).
        setChatError(
          err instanceof ApiError && err.status === 404
            ? "This chat no longer exists."
            : turnFailureMessage("turn_failed"),
        );
        endTurnState();
      }
    })();
    return () => ac.abort();
    // finishTurn/endTurnState/upsertSessionTitle are re-created every render but only read
    // refs/stable setters (and the latest props via closure at call time) — re-subscribing the
    // watcher on each render would restart the poll for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnInProgress, activeSessionId]);

  /** Clears every "a turn is running" flag/overlay (the turn ended one way or another). */
  function endTurnState() {
    setTurnInProgress(false);
    setPending(false);
    setInterimDraft(null);
    setConnectionLost(false);
    setCancelling(false);
    setTurnStartedAt(null);
    setRecoveredTurn(false);
  }

  /** Applies a finished turn's outcome to the screen. */
  async function finishTurn(final: ChatTurnSnapshot, signal: AbortSignal) {
    const sessionId = final.sessionId;
    upsertSessionTitle(sessionId, final.title);
    // The authoritative transcript + draft + trace (the persisted assistant message carries the
    // clarifying question). Retried once; if the reload still fails, fall back to the final
    // poll's own snapshot so the reply is never lost.
    let loaded: Awaited<ReturnType<typeof getChatSession>> = undefined;
    for (let attempt = 0; attempt < 2 && loaded === undefined; attempt++) {
      try {
        loaded = await getChatSession(sessionId);
      } catch {
        // retry once
      }
    }
    if (signal.aborted) return;

    const failure = final.turnError;
    if (failure) {
      // Keep the transcript as shown (a failed FIRST turn's message isn't persisted server-side, so
      // a reload would drop it) and the user's unsent edits; just say what happened.
      setTurnEvents(loaded?.turnEvents ?? []);
      setChatError(describeTurnFailure(failure));
      const lastUser = [...messagesRef.current].reverse().find((m) => m.role === "user");
      setLastFailedText(lastSentTextRef.current ?? lastUser?.content ?? null);
      endTurnState();
      return;
    }

    let nextMessages = loaded?.messages;
    if (nextMessages === undefined || !lastIsAssistant(nextMessages)) {
      // Fallback (reload failed, or the reply wasn't visible in the reload): append the reply
      // from the final poll's snapshot to what we already show.
      const base = nextMessages ?? messagesRef.current;
      nextMessages = [
        ...base,
        {
          id: `${sessionId}-a-${Date.now()}`,
          sessionId,
          role: "assistant",
          content: final.assistantText,
          createdAt: new Date().toISOString(),
          clarifyingQuestion: final.clarifyingQuestion,
        },
      ];
    }
    if (final.similarSuggestions && final.similarSuggestions.length > 0) {
      // The graph's check_similarity node paused before generating KPIs — surface the card.
      setPendingSuggestion(final.similarSuggestions[0]);
    }
    if (final.materializedScorecardId) {
      // The draft was just confirmed AND saved as a real scorecard — attach an unmissable
      // confirmation to this turn's reply (a distinct card in the transcript) and raise the
      // persistent banner above the transcript.
      const ref: SavedScorecardRef = {
        id: final.materializedScorecardId,
        name: final.draft.name ?? "Untitled scorecard",
        asNewVersion: !!session.targetScorecardId || savedRef.current !== null,
      };
      const lastIdx = nextMessages.length - 1;
      nextMessages = nextMessages.map((m, i) => (i === lastIdx ? { ...m, savedScorecard: ref } : m));
      setSaved(ref);
    }
    const finalDraft = loaded?.draft ?? final.draft;
    setMessages(nextMessages);
    setServerDraft(finalDraft);
    setDraft(finalDraft);
    setTurnEvents(loaded?.turnEvents ?? []);
    endTurnState();
  }

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, pendingSuggestion, chatError, pending, turnInProgress]);

  useEffect(() => {
    if (initialPrompt && !initialPromptSentRef.current) {
      initialPromptSentRef.current = true;
      handleSend(initialPrompt);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPrompt]);

  const editSummary = useMemo(() => describeDraftEdits(serverDraft, draft), [serverDraft, draft]);
  const dirty = editSummary !== null;
  // Mirrors `dirty` into ChatSessionsContext so the sidebar's own navigation links (which
  // live outside this component's render tree once mounted via SessionList, but are fed
  // by the same context — see ChatSessionsContext) get the same unsaved-changes guard
  // this component's own links already use (via `navGuard` below).
  useEffect(() => {
    setContextDirty(dirty);
    return () => setContextDirty(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);
  // Nothing to preview yet on a brand-new chat (no purpose, no KPIs, no target score) —
  // don't show an empty "Live preview" card as the default state; it appears once the
  // assistant has proposed something, or the user has made a local edit to show.
  // What the preview shows: while a turn runs, the backend's interim draft (name/purpose/audience/
  // target within seconds, then the KPIs + weights, then guidelines) layered over the user's own
  // draft WITHOUT overwriting fields they edited; otherwise just the draft. Memoized so a poll that
  // returns the same interim draft doesn't re-render the (potentially 100+ row) KPI tree.
  const displayDraft = useMemo(
    () => (turnInProgress ? mergeInterimDraft(draft, serverDraft, interimDraft) : draft),
    [turnInProgress, draft, serverDraft, interimDraft],
  );
  const hasDraftContent =
    !!displayDraft.name ||
    !!displayDraft.purposeStatement ||
    !!displayDraft.scope ||
    displayDraft.targetScore != null ||
    displayDraft.kpis.length > 0;
  const showPreview = hasDraftContent || dirty;

  // Preview edits live only in this tab until a chat turn carries them to the server —
  // warn before a reload/close silently throws them away.
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // `beforeunload` above only covers a reload/tab-close — it never fires for Next.js App
  // Router client-side navigation (clicking a Link), which would otherwise silently
  // discard unsent edits. Guards every in-app Link this component (and the session list
  // it renders) offers, via next/link's `onNavigate` — see lib/useUnsavedChangesGuard.ts.
  const navGuard = useUnsavedChangesGuard(dirty);

  async function runAssistantTurn(text: string) {
    // Double-submit guard (the composer/buttons are disabled too, but retry links, chips and the
    // initial-prompt effect all call in here): one turn at a time per session, enforced
    // synchronously via a ref since state updates are async.
    if (startingTurnRef.current) return;
    startingTurnRef.current = true;
    setPending(true);
    setChatError(null);
    setLastFailedText(null);
    setPendingSuggestion(null);
    lastSentTextRef.current = text;
    // A new turn starting server-side prunes the previous turn's chat_turn_events (see
    // `_mark_turn_in_progress` in app/api/v1/chat.py) — clear the client's copy too so a
    // stale trace from the last turn never lingers under the new "in progress" state.
    setTurnEvents([]);
    setInterimDraft(null);
    // Fold any unsent inline preview edits into this turn so the assistant applies them
    // to the server-side draft via its own update_draft tool (see LivePreviewPanel).
    const outgoing = editSummary ? `${text}\n\n${editSummary}` : text;

    // Sidebar-live-update fix: a brand-new chat has no real session id until the backend creates
    // one. Generating the id ourselves (a plain random UUID — the backend accepts and uses it
    // verbatim, see ChatSessionStart.session_id) and adopting it right now, before the request is
    // even sent, means the URL, the turn watcher and an optimistic sidebar placeholder are all
    // live immediately.
    const isFirstTurnOfNewSession = activeSessionId === "new";
    const clientSessionId = isFirstTurnOfNewSession ? crypto.randomUUID() : undefined;
    if (isFirstTurnOfNewSession && clientSessionId) {
      // Mark this as a SELF-adoption (see the resync effect above) before touching
      // activeSessionId/the URL, so the effect never mistakes its own transient
      // session.id/activeSessionId mismatch for an external session switch and resets
      // this in-flight turn's state out from under it.
      selfAdoptedSessionIdRef.current = clientSessionId;
      setActiveSessionId(clientSessionId);
      // (The URL is switched only AFTER the POST below created the session row — replacing it
      // first would let the page's server render race the row and 404 into notFound().)
      // Real title lands within a second or two (the watcher carries it into the sidebar as
      // soon as the backend has generated it) and upgrades this placeholder —
      // upsertSessionTitle no-ops on a null/empty title, so this placeholder is never blanked
      // out by a slow/failed title-generation call racing it.
      upsertSessionTitle(clientSessionId, "New chat");
    }

    try {
      // POST returns 202 at once with the turn running in the background; flipping
      // `turnInProgress` hands over to the turn watcher effect (the single poller).
      const started = await startChatTurn({
        sessionId: isFirstTurnOfNewSession ? "new" : activeSessionId,
        clientSessionId,
        message: outgoing,
      });
      upsertSessionTitle(started.sessionId, started.title);
      if (isFirstTurnOfNewSession) {
        router.replace(`/chat/${started.sessionId}`, { scroll: false });
      } else if (started.sessionId !== activeSessionId) {
        // Defensive fallback only (see the resync effect above).
        selfAdoptedSessionIdRef.current = started.sessionId;
        setActiveSessionId(started.sessionId);
        router.replace(`/chat/${started.sessionId}`, { scroll: false });
      }
      if (started.turnInProgress) {
        setTurnStartedAt(Date.now());
        setTurnInProgress(true);
        setPending(false);
      } else {
        // Inline/legacy backend mode: the POST already carries the finished turn.
        await finishTurn(started, new AbortController().signal);
      }
    } catch (err) {
      // The chat builder calls AWS Bedrock — an environment with no AWS credentials fails every
      // such call. Show a clear, friendly reason instead of crashing or spinning forever; the
      // user's own message above is kept in the transcript either way.
      // Note: edits in the live preview only reach the saved scorecard through the assistant,
      // so the message must not suggest "filling it in manually" saves anything.
      if (err instanceof ApiError && err.code === "user_chat_active") {
        // The user's one allowed chat turn is running in ANOTHER chat: nothing was sent from this one.
        if (isFirstTurnOfNewSession && clientSessionId) {
          selfAdoptedSessionIdRef.current = null;
          setActiveSessionId("new");
          removeSession(clientSessionId);
        }
        setBusyElsewhere(typeof err.data?.session_id === "string" ? err.data.session_id : null);
        setChatError(err.message);
        setLastFailedText(text);
        setPending(false);
        requestLiveRefresh();
      } else if (err instanceof ApiError && err.status === 409) {
        // Another turn is already running for this session (e.g. started from a second tab):
        // don't fail — attach to it; the watcher takes over and the reply shows up by itself.
        setTurnStartedAt(Date.now());
        setTurnInProgress(true);
        setPending(false);
        setChatError(
          "The assistant was still working on your previous message, so this one wasn't sent. Once it finishes, send it again.",
        );
        setLastFailedText(text);
      } else {
        if (isFirstTurnOfNewSession && clientSessionId) {
          // The session was never created: undo the optimistic adoption (id + sidebar entry) so a
          // retry starts a fresh "new" session instead of posting to a session that doesn't exist.
          selfAdoptedSessionIdRef.current = null;
          setActiveSessionId("new");
          removeSession(clientSessionId);
        }
        setChatError(
          err instanceof BedrockUnavailableError
            ? turnFailureMessage("bedrock_unavailable")
            : turnFailureMessage("turn_failed"),
        );
        setLastFailedText(text);
        setPending(false);
      }
    } finally {
      startingTurnRef.current = false;
    }
  }

  /** Stops the running turn (POST /cancel); the watcher then reports it as "cancelled". */
  async function handleCancelTurn() {
    if (cancelling || activeSessionId === "new") return;
    setCancelling(true);
    try {
      await cancelChatTurn(activeSessionId);
    } catch {
      // The turn may have finished by itself in the meantime — the watcher settles the state.
      setCancelling(false);
    }
  }

  async function handleSend(text: string) {
    if (!text.trim() || pending || turnInProgress) return;

    const userMessage: ChatMessage = {
      id: `m-${activeSessionId}-u-${Date.now()}`,
      sessionId: activeSessionId,
      role: "user",
      content: text,
      createdAt: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setComposer("");
    setChatError(null);

    await runAssistantTurn(text);
  }

  async function handleSuggestionChoice(action: "use" | "adapt" | "fresh", suggestion: Suggestion) {
    if (action === "use") {
      router.push(`/charts/${suggestion.scorecardId}`);
      return;
    }
    setPendingSuggestion(null);
    const noteText =
      action === "adapt"
        ? `Let's adapt "${suggestion.scorecardName}" for my use case.`
        : "Let's start fresh instead.";
    if (action === "adapt") {
      setDraft((d) => ({ ...d, domain: d.domain ?? suggestion.domain }));
      setServerDraft((d) => ({ ...d, domain: d.domain ?? suggestion.domain }));
    }
    setMessages((prev) => [
      ...prev,
      {
        id: `m-${activeSessionId}-u-choice-${Date.now()}`,
        sessionId: activeSessionId,
        role: "user",
        content: noteText,
        createdAt: new Date().toISOString(),
      },
    ]);
    await runAssistantTurn(noteText);
  }

  const lastMessage = messages[messages.length - 1];
  const awaitingClarification =
    lastMessage?.role === "assistant" && !!lastMessage.clarifyingQuestion && !pending && !turnInProgress;

  async function handleSendEdits() {
    if (!dirty || pending || turnInProgress) return;
    await handleSend("Please apply the edits I made in the live preview.");
  }

  // Chat/preview split is drag-resizable at xl+ (see ResizablePanelGroup below) — only
  // side-by-side layouts support that; below xl the panels stack and resizing a width
  // wouldn't mean anything. Default true so the resizable markup (and its ARIA
  // attributes) are present in the server-rendered HTML for the common desktop case;
  // corrects itself right after mount on an actual narrow viewport (see useMediaQuery).
  const isDesktop = useMediaQuery("(min-width: 1280px)", true);
  const previewPanelRef = useRef<PanelImperativeHandle>(null);

  // Restore the user's last drag-resized live-preview width once the resizable panel
  // actually exists (desktop layout, preview shown at all) — mirrors the plain
  // localStorage read/write pattern used elsewhere (see usePersistedState) rather than
  // the library's own autosave hook, which reads `localStorage` unconditionally as a
  // default parameter and would throw during server rendering.
  useEffect(() => {
    if (!isDesktop || !showPreview) return;
    try {
      const raw = window.localStorage.getItem(PREVIEW_WIDTH_STORAGE_KEY);
      const px = raw ? Number(raw) : NaN;
      if (Number.isFinite(px)) previewPanelRef.current?.resize(px);
    } catch {
      // Ignore read failures (quota, private browsing) — falls back to defaultSize.
    }
  }, [isDesktop, showPreview]);

  // The session title is generated asynchronously by the backend and lands in the sidebar list
  // (via the watcher); mirror it in this header too instead of the stale server-rendered prop.
  const liveTitle = sessions.find((s) => s.id === activeSessionId)?.title ?? session.title;

  const chatCard = (
    <GlassCard
      elevation={1}
      className={cn(
        "flex h-[75vh] min-h-[28rem] min-w-0 flex-1 flex-col p-0",
        // landscape phones (short viewports): the card fills the screen so the composer is on screen, not below the fold
        "[@media(max-height:520px)]:h-[calc(100dvh-5.5rem)] [@media(max-height:520px)]:min-h-[15rem]",
        isDesktop && "h-full",
      )}
    >
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-hairline px-4 py-3.5 sm:px-5">
          <div className="min-w-0 flex-1 basis-48">
            <p className="truncate text-sm font-semibold text-ink">{liveTitle}</p>
            <p className="truncate text-xs text-ink-muted">{session.contextSummary}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {/* Top right: share this chat (read-only) with someone, optionally together with its chart. */}
            {activeSessionId !== "new" && (
              <ChatShareButton sessionId={activeSessionId} hasChart={Boolean(saved || session.targetScorecardId)} />
            )}
            <span className="flex items-center gap-1.5 xl:hidden">
            <Button asChild variant="ghost" size="sm">
              <Link {...navGuard.linkProps("/chat")}>
                <ArrowLeft className="size-3.5" aria-hidden />
                All chats
              </Link>
            </Button>
            {showPreview && (
              <Button asChild variant="outline" size="sm">
                <a href="#live-preview">
                  <FileText className="size-3.5" aria-hidden />
                  Preview{displayDraft.kpis.length > 0 ? ` (${draftKpiCounts(displayDraft.kpis).kpis})` : ""}
                </a>
              </Button>
            )}
            </span>
          </div>
        </div>

        {connectionLost && turnInProgress && (
          <div
            role="status"
            aria-live="polite"
            className="flex items-center gap-2 border-b border-hairline bg-[var(--rag-poor)]/5 px-5 py-2 text-xs text-ink"
          >
            <AlertTriangle className="size-3.5 shrink-0 text-[var(--rag-poor)]" aria-hidden />
            <span>Lost contact with the server — retrying automatically. The assistant keeps working meanwhile.</span>
          </div>
        )}

        {turnInProgress && recoveredTurn && (
          // Refresh-recovery indicator (Part A): a turn is still running server-side —
          // e.g. the page was reloaded mid multi-agent research fan-out, from a
          // *different* tab/request than this one. Persistent — stays up across
          // re-renders until the poll above confirms the turn finished. The granular
          // live trace itself (Part B) renders below, in the transcript, via
          // TurnTraceCard — this banner is just the short "why is the composer
          // disabled" explanation for a page that just loaded mid-turn.
          <div
            role="status"
            aria-live="polite"
            className="flex items-center gap-2 border-b border-hairline bg-lemon-soft/60 px-5 py-2.5 text-xs text-ink"
          >
            <span>
              The assistant is still working on your last message — this can take a minute or two for
              research-heavy requests. See what it&apos;s doing below; this page will update automatically
              when it&apos;s done.
            </span>
          </div>
        )}

        <ChartStateNotice session={session} />

        {saved ? (
          <SavedScorecardBanner saved={saved} variant="banner" linkProps={navGuard.linkProps} />
        ) : (
          isRefineSession &&
          session.targetScorecardId && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline bg-lemon-soft/50 px-5 py-2.5 text-xs text-ink [@media(max-height:520px)]:hidden">
              <p className="flex min-w-0 items-center gap-1.5">
                <Sparkles className="size-3.5 shrink-0 text-lemon-ink" aria-hidden />
                <span>
                  Refining <strong>“{refineTargetName ?? "this scorecard"}”</strong>. Nothing changes on the saved scorecard
                  until you tell the assistant to save — then it&apos;s saved as a new version.
                </span>
              </p>
              <Button asChild variant="ghost" size="sm">
                <Link {...navGuard.linkProps(`/charts/${session.targetScorecardId}`)}>Back to scorecard</Link>
              </Button>
            </div>
          )
        )}

        <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-5 py-4 thin-scrollbar">
          {messages.length === 0 && !turnInProgress && (
            <p className="text-sm text-ink-muted">
              Describe what you want to rate, in plain language — I&apos;ll ask a couple of quick questions and
              propose KPIs. You can also fill in the live preview directly.
            </p>
          )}
          {dedupeRetriedMessages(messages).map((message) => (
            <div key={message.id} className="space-y-2">
              <MessageBubble
                message={
                  message.clarifyingQuestion
                    ? { ...message, content: stripTrailingQuestion(message.content, message.clarifyingQuestion.question) }
                    : message
                }
              />
              {message.savedScorecard && (
                <SavedScorecardBanner saved={message.savedScorecard} variant="inline" linkProps={navGuard.linkProps} />
              )}
              {message.clarifyingQuestion && (
                <ClarifyingQuestionCard
                  question={message.clarifyingQuestion}
                  onAnswer={handleSend}
                  disabled={pending || turnInProgress || message.id !== lastMessage?.id}
                />
              )}
            </div>
          ))}
          {pendingSuggestion && (
            <SimilarScorecardSuggestion
              suggestion={pendingSuggestion}
              onChoose={(action) => handleSuggestionChoice(action, pendingSuggestion)}
              disabled={pending || turnInProgress}
            />
          )}
          {(pending || turnInProgress) && (
            <TurnTraceCard
              events={turnEvents}
              active={pending || turnInProgress}
              startedAt={turnStartedAt}
              onCancel={turnInProgress && activeSessionId !== "new" ? handleCancelTurn : undefined}
              cancelling={cancelling}
            />
          )}
          {chatError && (
            // Rendered IN the transcript (where the assistant's reply would have
            // appeared), not as a small footer line, so a failed turn is never mistaken
            // for "nothing happened".
            <div
              role="alert"
              className="ml-9 flex max-w-[80%] items-start gap-2.5 rounded-2xl border border-[var(--rag-poor)]/30 bg-[var(--rag-poor)]/5 px-4 py-3 text-sm text-ink"
            >
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-[var(--rag-poor)]" aria-hidden />
              <div className="space-y-2">
                <p>{chatError}</p>
                {session.targetScorecardId && (
                  <p className="text-xs text-ink-muted">
                    You don&apos;t need the assistant to make changes: KPI names, weights and guidelines of a draft
                    scorecard can be edited directly on its Overview tab.
                  </p>
                )}
                <div className="flex flex-wrap gap-2">
                  {lastFailedText && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => runAssistantTurn(lastFailedText)}
                      disabled={pending || turnInProgress}
                    >
                      <RotateCcw className="size-3.5" aria-hidden />
                      Try again
                    </Button>
                  )}
                  {session.targetScorecardId && (
                    <Button asChild size="sm" variant="ghost">
                      <Link {...navGuard.linkProps(`/charts/${session.targetScorecardId}`)}>
                        <Pencil className="size-3.5" aria-hidden />
                        Edit it directly instead
                      </Link>
                    </Button>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>

        {dirty && (
          // Unsent preview edits are LOCAL until a chat turn carries them to the server
          // (see LivePreviewPanel). Surface that right above the composer — where the
          // user's attention is — so edits can't be silently lost by leaving, or be
          // mistaken for already-saved changes.
          <div
            role="status"
            className="flex flex-wrap items-center justify-between gap-2 border-t border-hairline bg-lemon-soft/60 px-4 py-2.5"
          >
            <p className="flex min-w-0 items-start gap-1.5 text-xs text-ink">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-lemon-ink" aria-hidden />
              <span>
                <strong>You have unsent edits</strong> in the live preview. They aren&apos;t saved until the assistant
                applies them — they&apos;ll go with your next message, or send them now.
              </span>
            </p>
            <div className="flex shrink-0 gap-1.5">
              <Button type="button" size="sm" onClick={handleSendEdits} disabled={pending || turnInProgress}>
                <Send className="size-3.5" aria-hidden />
                Send edits now
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setDraft(serverDraft)}
                disabled={pending || turnInProgress}
              >
                <Undo2 className="size-3.5" aria-hidden />
                Discard
              </Button>
            </div>
          </div>
        )}

        {elsewhereId && (
          <p role="status" className="mx-3 mb-1 rounded-lg bg-lemon-soft px-3 py-2 text-xs text-lemon-ink" data-testid="chat-busy-elsewhere">
            Your assistant is still working in another chat. You can send here once it finishes.{" "}
            <a className="font-semibold underline underline-offset-2" href={`/chat/${elsewhereId}`}>
              View it
            </a>
          </p>
        )}
        <form
          className="flex items-end gap-2 border-t border-hairline p-3"
          onSubmit={(e) => {
            e.preventDefault();
            handleSend(composer);
          }}
        >
          <Textarea
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder={
              turnInProgress
                ? "Waiting for the assistant to finish your last message…"
                : awaitingClarification
                  ? "Or type your own answer…"
                  : "Type a message…"
            }
            className="max-h-32 min-h-10 flex-1 resize-none"
            disabled={pending || turnInProgress || !!pendingSuggestion || otherChatBusy}
            aria-label="Message the scorecard assistant"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend(composer);
              }
            }}
          />
          <Button
            type="submit"
            size="icon"
            disabled={pending || turnInProgress || !composer.trim() || !!pendingSuggestion || otherChatBusy}
            aria-label="Send"
          >
            <Send className="size-4" />
          </Button>
        </form>
      </GlassCard>
  );

  // Stable callbacks (reading the latest handler/draft through refs) so the memoized preview
  // panel only re-renders when its draft/flags actually change — not on every trace/poll update.
  const sendEditsRef = useRef(handleSendEdits);
  const serverDraftRef = useRef(serverDraft);
  useEffect(() => {
    sendEditsRef.current = handleSendEdits;
    serverDraftRef.current = serverDraft;
  });
  const onPreviewSendEdits = useCallback(() => {
    void sendEditsRef.current();
  }, []);
  const onPreviewDiscardEdits = useCallback(() => setDraft(serverDraftRef.current), []);

  const previewPanel = showPreview && (
    <LivePreviewPanel
      draft={displayDraft}
      onChange={setDraft}
      dirty={dirty}
      onSendEdits={onPreviewSendEdits}
      onDiscardEdits={onPreviewDiscardEdits}
      disabled={pending || turnInProgress}
      className={isDesktop ? "h-full" : "w-full shrink-0 scroll-mt-4"}
    />
  );

  return (
    // Responsive layout: at xl+ the chat and live preview sit side by side at full
    // viewport height, and the split is drag-resizable. Below xl (laptop-width /
    // non-maximized windows) they STACK — chat first, then the preview full-width
    // underneath — so the preview can never be pushed off-screen by a fixed-width flex
    // row. A "Preview" jump link in the chat header makes the stacked preview reachable
    // in one click.
    //
    // The session list that used to render here as its own column (`SessionList`,
    // `hidden shrink-0 xl:block`) has moved into `NavRail`'s merged "Chat" section (see
    // that component's docstring) — this workspace is now just the chat/preview split,
    // no second sidebar column.
    <div className="flex flex-col gap-4 md:pt-12 xl:h-[calc(100vh-2rem)] xl:flex-row">
      {isDesktop ? (
        <ResizablePanelGroup orientation="horizontal" className="min-h-[28rem] flex-1">
          <ResizablePanel minSize={360} className="flex min-w-0 flex-col">
            {chatCard}
          </ResizablePanel>
          {showPreview && (
            <>
              <ResizableHandle withHandle aria-label="Resize live preview panel width" />
              <ResizablePanel
                panelRef={previewPanelRef}
                defaultSize={320}
                minSize={260}
                maxSize={560}
                className="flex min-w-0 flex-col"
                onResize={(size, _id, prevSize) => {
                  // `prevSize` is undefined on the initial mount render — only persist
                  // real changes (a drag, or the reflow that follows one), not that
                  // first layout commit (which would just re-write the default/restored
                  // size back to itself, harmlessly, but pointlessly).
                  if (prevSize === undefined) return;
                  try {
                    window.localStorage.setItem(PREVIEW_WIDTH_STORAGE_KEY, String(Math.round(size.inPixels)));
                  } catch {
                    // Ignore write failures (quota, private browsing).
                  }
                }}
              >
                {previewPanel}
              </ResizablePanel>
            </>
          )}
        </ResizablePanelGroup>
      ) : (
        <>
          {chatCard}
          {previewPanel}
        </>
      )}

      <UnsavedChangesDialog
        open={navGuard.isConfirmOpen}
        onConfirmLeave={navGuard.confirmLeave}
        onCancel={navGuard.cancelLeave}
      />
    </div>
  );
}

/**
 * Unmissable "your scorecard was saved" confirmation. Rendered twice on purpose: as a
 * persistent strip pinned above the transcript ("banner" — stays visible however far the
 * chat scrolls), and as a distinct card in the message stream at the exact turn the save
 * happened ("inline"). Uses the GlassCard surface + RAG "excellent" token, not an ad-hoc
 * style.
 */
function SavedScorecardBanner({
  saved,
  variant,
  linkProps,
}: {
  saved: SavedScorecardRef;
  variant: "banner" | "inline";
  /** Guards the "Open scorecard" link too — dirty can still be true here if the user made
   * further live-preview edits after saving. See `useUnsavedChangesGuard`. */
  linkProps: ReturnType<typeof useUnsavedChangesGuard>["linkProps"];
}) {
  const headline = saved.asNewVersion ? `Saved a new version of “${saved.name}”` : `Saved as “${saved.name}”`;
  const detail =
    variant === "banner"
      ? "It's now in your scorecard library."
      : saved.asNewVersion
        ? "The previous version and its evaluations are unchanged. Open the scorecard to review or evaluate with the new version."
        : "It's now a real scorecard in your library, saved as a draft. Open it to review, publish, or start evaluating.";
  return (
    <GlassCard
      elevation={3}
      role={variant === "inline" ? "status" : undefined}
      aria-live={variant === "inline" ? "polite" : undefined}
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-l-4 border-[var(--rag-excellent)]",
        variant === "banner" ? "mx-3 mt-3 rounded-xl px-4 py-2.5" : "ml-9 px-4 py-3.5",
      )}
    >
      <div className="flex min-w-0 items-start gap-2.5">
        <CheckCircle2
          className={cn("shrink-0 text-[var(--rag-excellent)]", variant === "banner" ? "mt-0.5 size-4" : "size-6")}
          aria-hidden
        />
        <div className="min-w-0">
          <p className={cn("font-semibold text-ink", variant === "banner" ? "text-sm" : "text-base")}>{headline}</p>
          <p className="text-xs text-ink-muted">{detail}</p>
        </div>
      </div>
      <Button asChild size="sm">
        <Link {...linkProps(`/charts/${saved.id}`)}>
          Open scorecard
          <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </Button>
    </GlassCard>
  );
}
