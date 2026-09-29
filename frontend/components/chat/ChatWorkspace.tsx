"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
import { UnsavedChangesDialog } from "./UnsavedChangesDialog";
import { GlassCard } from "@/components/design-system/GlassCard";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import type { PanelImperativeHandle } from "@/components/ui/resizable";
import {
  BedrockUnavailableError,
  getChatSession,
  getChatTurnEvents,
  getChatTurnStatus,
  sendChatMessage,
} from "@/lib/api-client";
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

export function ChatWorkspace({
  session,
  initialMessages,
  initialDraft,
  initialPrompt,
  initialSavedScorecardId,
  initialTurnInProgress,
  initialTurnEvents,
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
  const [refineTargetName] = useState(initialDraft.name);
  // Issue 1 (see task notes): the sidebar session list now lives in ChatSessionsContext
  // (see the root app/layout.tsx), not local state here — this component (rendered by
  // app/chat/[sessionId]/page.tsx) is itself remounted on every session switch, so any
  // state kept here wouldn't survive one anyway. `upsertSessionTitle` below is folded
  // into the same polling this component already runs for turn_in_progress/turn-events
  // (see the two poll effects below and runAssistantTurn's own success path), so a real,
  // AI-generated (or just-changed) title appears in the sidebar without a page reload.
  const { upsertSessionTitle: upsertSessionTitleInContext, setDirty: setContextDirty } = useChatSessions();
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  // `serverDraft` is the last draft the (authoritative) backend returned; `draft` is what
  // the live preview shows and lets the user edit inline. They differ exactly when the
  // user has made local edits that haven't been sent to the assistant yet.
  const [serverDraft, setServerDraft] = useState<ScorecardDraft>(initialDraft);
  const [draft, setDraft] = useState<ScorecardDraft>(initialDraft);
  const [lastFailedText, setLastFailedText] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [composer, setComposer] = useState("");
  const [pendingSuggestion, setPendingSuggestion] = useState<Suggestion | null>(null);
  const [chatError, setChatError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // The real chat backend is stateful server-side (a LangGraph session keyed by
  // chat_sessions.id) rather than driven by a client-tracked turn index. A brand-new
  // session starts as the literal id "new"; the backend assigns a real id on the first
  // successful turn, which we then adopt (and reflect in the URL) for every turn after.
  const [activeSessionId, setActiveSessionId] = useState(session.id);
  const initialPromptSentRef = useRef(false);

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

  useEffect(() => {
    if (!turnInProgress) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const [{ turnInProgress: stillRunning, title }, events] = await Promise.all([
          getChatTurnStatus(activeSessionId),
          // Best-effort alongside the authoritative turnInProgress flag — a failed
          // events fetch must never stop the "is it still running" poll from working.
          getChatTurnEvents(activeSessionId).catch(() => null),
        ]);
        if (cancelled) return;
        if (events) setTurnEvents(events);
        // Issue 1: the same poll that already watches turn_in_progress also carries the
        // session's title (generated fast, well before this poll's turn even finishes —
        // see backend app/api/v1/chat.py::_generate_and_persist_title) — reflect it into
        // the sidebar live, no reload needed.
        upsertSessionTitle(activeSessionId, title);
        if (!stillRunning) {
          // The turn finished (or its marker went stale) while we weren't watching —
          // reload the real message log + draft it produced, rather than guessing.
          const result = await getChatSession(activeSessionId);
          if (cancelled) return;
          if (result) {
            setMessages(result.messages);
            setServerDraft(result.draft);
            setDraft(result.draft);
            setTurnEvents(result.turnEvents);
            if (result.savedScorecardId) {
              setSaved({
                id: result.savedScorecardId,
                name: result.draft.name ?? "your scorecard",
                asNewVersion: !!session.targetScorecardId,
              });
            }
          }
          setTurnInProgress(false);
        }
      } catch {
        // Transient network hiccup while polling — keep trying on the next tick rather
        // than dropping the "still working" indicator on one failed request.
      }
    };
    // An immediate poll (not just the interval below) so a refreshed page's trace fills
    // in as soon as possible, plus a tighter 1.5s cadence (down from the original 4s)
    // for a genuinely live feel while a turn is actively in progress — this only runs at
    // all while turnInProgress is true, so an idle session is never polled.
    poll();
    const interval = setInterval(poll, 1500);
    // "Also fetch it on... returning to the tab" — a background tab's timers are
    // throttled/paused by the browser, so re-poll immediately the moment the tab
    // becomes visible again instead of waiting for the next throttled interval tick.
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") poll();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnInProgress, activeSessionId]);

  // Second polling loop: a turn sent from THIS tab (`pending`) blocks the whole
  // `sendChatMessage` call for the turn's full duration (see module docstring on the
  // synchronous request/response model) — the browser can still fire concurrent
  // requests alongside that outstanding one, so this polls the same turn-events endpoint
  // to show live progress DURING that call, not just via the refresh-recovery path
  // above. Guarded on `activeSessionId !== "new"`: a brand-new chat's very first message
  // has no real session id to poll with until the blocked call itself returns (the
  // backend only hands back the generated id in that same response) — see
  // TurnTraceCard's fallback for that one narrow case.
  useEffect(() => {
    if (!pending || activeSessionId === "new") return;
    let cancelled = false;
    const poll = async () => {
      try {
        const events = await getChatTurnEvents(activeSessionId);
        if (!cancelled) setTurnEvents(events);
      } catch {
        // Transient hiccup — next tick (or the final fetch in runAssistantTurn's own
        // success path) will catch it up.
      }
    };
    poll();
    const interval = setInterval(poll, 1500);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") poll();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [pending, activeSessionId]);

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
  const hasDraftContent =
    !!draft.name ||
    !!draft.purposeStatement ||
    !!draft.scope ||
    draft.targetScore != null ||
    draft.kpis.length > 0;
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
    setPending(true);
    setChatError(null);
    setLastFailedText(null);
    // A new turn starting server-side prunes the previous turn's chat_turn_events (see
    // `_mark_turn_in_progress` in app/api/v1/chat.py) — clear the client's copy too so a
    // stale trace from the last turn never lingers under the new "in progress" state.
    setTurnEvents([]);
    // Fold any unsent inline preview edits into this turn so the assistant applies them
    // to the server-side draft via its own update_draft tool (see LivePreviewPanel).
    const outgoing = editSummary ? `${text}\n\n${editSummary}` : text;
    try {
      const result = await sendChatMessage({ sessionId: activeSessionId, message: outgoing });
      if (result.sessionId !== activeSessionId) {
        // The backend just created the real session (first turn of a "new" chat) —
        // adopt its id and swap the URL to it without losing in-memory state.
        setActiveSessionId(result.sessionId);
        router.replace(`/chat/${result.sessionId}`, { scroll: false });
      }
      // Issue 1: a brand-new session's real title is already on THIS response (generated
      // fast, before the graph even ran — see _generate_and_persist_title) — reflect it
      // into the sidebar immediately, same mechanism as the poll above.
      upsertSessionTitle(result.sessionId, result.title);
      if (result.similarSuggestions && result.similarSuggestions.length > 0) {
        // The chat graph's own check_similarity node (see
        // backend/app/ai/scorecard_builder.py) paused *before* propose_kpis ran — "reuse
        // suggestions before generating from scratch" per the plan. Surface the card;
        // no assistant message/draft update happened on this turn.
        setPendingSuggestion(result.similarSuggestions[0]);
      } else {
        let assistantMessage = result.assistantMessage;
        if (result.materializedScorecardId) {
          // The draft was just confirmed AND saved as a real scorecard — attach an
          // unmissable confirmation to this turn (rendered as a distinct card in the
          // transcript) and raise the persistent banner above the transcript.
          const ref: SavedScorecardRef = {
            id: result.materializedScorecardId,
            name: result.draft.name ?? "Untitled scorecard",
            asNewVersion: !!session.targetScorecardId || saved !== null,
          };
          assistantMessage = { ...assistantMessage, savedScorecard: ref };
          setSaved(ref);
        }
        setMessages((prev) => [...prev, assistantMessage]);
        setServerDraft(result.draft);
        setDraft(result.draft);
      }
    } catch (err) {
      // The chat builder calls AWS Bedrock (Converse / Titan embeddings) — an
      // environment with no AWS credentials fails every such call. Show a clear,
      // friendly reason instead of crashing or spinning forever; the user's own message
      // above is kept in the transcript either way.
      // Note: edits in the live preview only reach the saved scorecard through the
      // assistant, so the message must not suggest "filling it in manually" saves anything.
      setChatError(
        err instanceof BedrockUnavailableError
          ? "The AI assistant is temporarily unavailable, so nothing was changed or saved. Your draft and any unsent edits are kept here — try again in a few minutes."
          : "Something went wrong reaching the assistant. Nothing was saved — please try again.",
      );
      setLastFailedText(text);
    } finally {
      setPending(false);
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

  const chatCard = (
    <GlassCard
      elevation={1}
      className={cn("flex h-[75vh] min-h-[28rem] min-w-0 flex-1 flex-col p-0", isDesktop && "h-full")}
    >
        <div className="flex items-center justify-between gap-3 border-b border-hairline px-5 py-3.5">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-ink">{session.title}</p>
            <p className="truncate text-xs text-ink-muted">{session.contextSummary}</p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5 xl:hidden">
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
                  Preview{draft.kpis.length > 0 ? ` (${draft.kpis.length})` : ""}
                </a>
              </Button>
            )}
          </div>
        </div>

        {turnInProgress && !pending && (
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

        {saved ? (
          <SavedScorecardBanner saved={saved} variant="banner" linkProps={navGuard.linkProps} />
        ) : (
          isRefineSession &&
          session.targetScorecardId && (
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-hairline bg-lemon-soft/50 px-5 py-2.5 text-xs text-ink">
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
          {messages.map((message) => (
            <div key={message.id} className="space-y-2">
              <MessageBubble message={message} />
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
          {(pending || turnInProgress) && <TurnTraceCard events={turnEvents} active={pending || turnInProgress} />}
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
                      disabled={pending}
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
            disabled={pending || turnInProgress || !!pendingSuggestion}
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
            disabled={pending || turnInProgress || !composer.trim() || !!pendingSuggestion}
            aria-label="Send"
          >
            <Send className="size-4" />
          </Button>
        </form>
      </GlassCard>
  );

  const previewPanel = showPreview && (
    <LivePreviewPanel
      draft={draft}
      onChange={setDraft}
      dirty={dirty}
      onSendEdits={handleSendEdits}
      onDiscardEdits={() => setDraft(serverDraft)}
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
    <div className="flex flex-col gap-4 xl:h-[calc(100vh-2rem)] xl:flex-row">
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

/**
 * Plain-language summary of how the locally-edited draft differs from the last
 * server draft, or null when there are no edits. Appended to the next chat message so
 * the assistant can apply the edits through its own update_draft tool.
 */
function describeDraftEdits(server: ScorecardDraft, local: ScorecardDraft): string | null {
  const changes: string[] = [];
  const fmt = (v: string | number | null) => (v === null || v === "" ? "(cleared)" : `"${v}"`);
  if ((server.name ?? "") !== (local.name ?? "")) changes.push(`- Scorecard name: ${fmt(local.name)}`);
  if ((server.purposeStatement ?? "") !== (local.purposeStatement ?? ""))
    changes.push(`- Purpose: ${fmt(local.purposeStatement)}`);
  if ((server.scope ?? "") !== (local.scope ?? "")) changes.push(`- Scope / audience: ${fmt(local.scope)}`);
  if (server.targetScore !== local.targetScore) changes.push(`- Target score: ${fmt(local.targetScore)}`);

  const serverById = new Map(server.kpis.map((k) => [k.id, k]));
  const localIds = new Set(local.kpis.map((k) => k.id));
  const removed = server.kpis.filter((k) => !localIds.has(k.id));
  const kpiChanged =
    removed.length > 0 ||
    local.kpis.some((k) => {
      const before = serverById.get(k.id);
      return !before || before.name !== k.name || before.weight !== k.weight || before.includedInScoring !== k.includedInScoring;
    });

  if (removed.length > 0) changes.push(`- Remove KPIs: ${removed.map((k) => `"${k.name}"`).join(", ")}`);
  if (kpiChanged) {
    const byId = new Map(local.kpis.map((k) => [k.id, k]));
    const list = local.kpis
      .map((k) => {
        const parent = k.parentId ? byId.get(k.parentId) : undefined;
        const excluded = k.includedInScoring === false ? " [excluded from the weighted score]" : "";
        return `"${k.name}" ${k.weight}%${parent ? ` (under "${parent.name}")` : ""}${excluded}`;
      })
      .join("; ");
    changes.push(`- KPI list should now be exactly: ${list || "(none)"} (use each KPI's included_in_scoring flag as noted)`);
  }

  // Issue 2 (see task notes): a scoring-formula edit made directly in the live preview
  // panel (see LivePreviewPanel) is carried to the assistant the same way as every other
  // field here — it applies it via its own `update_scoring_formula` tool, never a direct
  // draft field write.
  if ((server.scoringFormula ?? null) !== (local.scoringFormula ?? null)) {
    changes.push(
      local.scoringFormula
        ? `- Scoring formula: set it to exactly ${JSON.stringify(local.scoringFormula)}`
        : "- Scoring formula: clear the custom formula (revert to the default weighted average)",
    );
  }

  if (changes.length === 0) return null;
  return `[Edits I made directly in the live preview — please apply them to the draft:\n${changes.join("\n")}]`;
}
