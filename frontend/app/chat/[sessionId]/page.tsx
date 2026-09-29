import { notFound } from "next/navigation";

import { ChatWorkspace } from "@/components/chat/ChatWorkspace";
import { getChatSession } from "@/lib/api-client";
import { MOCK_CURRENT_USER } from "@/lib/mock-data";
import type { ChatSession, ScorecardDraft } from "@/lib/types";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function ChatSessionPage({
  params,
  searchParams,
}: {
  params: Promise<{ sessionId: string }>;
  searchParams: Promise<{ prompt?: string }>;
}) {
  const { sessionId } = await params;
  const { prompt } = await searchParams;

  // The sidebar's own session list is no longer fetched/passed down here — it lives in
  // ChatSessionsContext (see app/chat/layout.tsx), which persists across navigation
  // between this page and its siblings instead of being re-fetched on every switch.

  if (sessionId === "new") {
    const now = new Date().toISOString();
    const session: ChatSession = {
      id: "new",
      userId: MOCK_CURRENT_USER.id,
      title: "New scorecard",
      status: "active",
      contextSummary: "Just started.",
      targetScorecardId: null,
      createdAt: now,
      lastActivityAt: now,
    };
    const draft: ScorecardDraft = {
      sessionId: "new",
      name: null,
      domain: null,
      purposeStatement: null,
      scope: null,
      targetScore: null,
      kpis: [],
      scoringFormula: null,
    };

    return (
      <ChatWorkspace session={session} initialMessages={[]} initialDraft={draft} initialPrompt={prompt} />
    );
  }

  const result = await getChatSession(sessionId);
  if (!result) notFound();

  return (
    <ChatWorkspace
      session={result.session}
      initialMessages={result.messages}
      initialDraft={result.draft}
      initialSavedScorecardId={result.savedScorecardId}
      initialTurnInProgress={result.turnInProgress}
      initialTurnEvents={result.turnEvents}
    />
  );
}
