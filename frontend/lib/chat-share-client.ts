import { ApiError, authApiFetch } from "./api-client";

/**
 * Chat sharing (read-only; optionally together with the chart). A shared chat shows the recipient the conversation and
 * the KPIs; they cannot talk to the assistant in it and keep the result by saving their OWN chart.
 */

export interface ChatShare {
  id: string;
  sessionId: string;
  recipientId: string;
  recipientName: string;
  recipientEmail: string;
  withChart: boolean;
  chartInvitationStatus: string | null;
  /** Who passed the chat on to this recipient (the owner or an earlier recipient). */
  sharedByName: string | null;
}

interface BeChatShare {
  id: string;
  session_id: string;
  recipient_id: string;
  recipient_name: string;
  recipient_email: string;
  with_chart: boolean;
  chart_invitation_status?: string | null;
  shared_by_name?: string | null;
}

function mapChatShare(s: BeChatShare): ChatShare {
  return {
    id: s.id,
    sessionId: s.session_id,
    recipientId: s.recipient_id,
    recipientName: s.recipient_name,
    recipientEmail: s.recipient_email,
    withChart: s.with_chart,
    chartInvitationStatus: s.chart_invitation_status ?? null,
    sharedByName: s.shared_by_name ?? null,
  };
}

/** Shares a chat with someone; `withChart` also invites them to the chart the chat produced. */
export async function shareChat(sessionId: string, identifier: string, withChart: boolean): Promise<ChatShare> {
  return mapChatShare(
    await authApiFetch<BeChatShare>(`/api/v1/chat/sessions/${sessionId}/shares`, {
      method: "POST",
      body: { identifier, with_chart: withChart },
    }),
  );
}

export async function listChatShares(sessionId: string): Promise<ChatShare[]> {
  return (await authApiFetch<BeChatShare[]>(`/api/v1/chat/sessions/${sessionId}/shares`)).map(mapChatShare);
}

export async function revokeChatShare(sessionId: string, shareId: string): Promise<void> {
  await authApiFetch(`/api/v1/chat/sessions/${sessionId}/shares/${shareId}`, { method: "DELETE" });
}

export interface SharedChatSummary {
  sessionId: string;
  title: string | null;
  ownerName: string;
  /** Who shared it with the viewer (the owner, or somebody who received it earlier). */
  sharedByName: string | null;
  withChart: boolean;
  sharedAt: string;
  savedScorecardId: string | null;
}

export async function listSharedChats(): Promise<SharedChatSummary[]> {
  const rows = await authApiFetch<
    Array<{
      session_id: string;
      title: string | null;
      owner_name: string;
      shared_by_name?: string | null;
      with_chart: boolean;
      shared_at: string;
      saved_scorecard_id: string | null;
    }>
  >("/api/v1/chat/shared");
  return rows.map((r) => ({
    sessionId: r.session_id,
    title: r.title,
    ownerName: r.owner_name,
    sharedByName: r.shared_by_name ?? null,
    withChart: r.with_chart,
    sharedAt: r.shared_at,
    savedScorecardId: r.saved_scorecard_id,
  }));
}

export interface SharedChat extends SharedChatSummary {
  messages: Array<{ id: string; role: string; content: string; createdAt: string }>;
  draft: {
    name?: string | null;
    purpose?: string | null;
    domain?: string | null;
    target_score?: number | null;
    kpis?: Array<{ name: string; weight: number | null; level: number; parent_name: string | null }>;
  };
  canSave: boolean;
  linkedScorecardId: string | null;
}

export async function getSharedChat(sessionId: string): Promise<SharedChat | null> {
  try {
    const r = await authApiFetch<{
      session_id: string;
      title: string | null;
      owner_name: string;
      shared_by_name?: string | null;
      with_chart: boolean;
      shared_at: string;
      saved_scorecard_id: string | null;
      messages: Array<{ id: string; role: string; content: string; created_at: string }>;
      draft: SharedChat["draft"];
      can_save: boolean;
      linked_scorecard_id: string | null;
    }>(`/api/v1/chat/shared/${sessionId}`);
    return {
      sessionId: r.session_id,
      title: r.title,
      ownerName: r.owner_name,
      sharedByName: r.shared_by_name ?? null,
      withChart: r.with_chart,
      sharedAt: r.shared_at,
      savedScorecardId: r.saved_scorecard_id,
      messages: r.messages.map((m) => ({ id: m.id, role: m.role, content: m.content, createdAt: m.created_at })),
      draft: r.draft ?? {},
      canSave: r.can_save,
      linkedScorecardId: r.linked_scorecard_id,
    };
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Saves the shared chat's KPIs as the caller's OWN chart; resolves to its id. */
export async function saveSharedChat(sessionId: string): Promise<string> {
  const r = await authApiFetch<{ scorecard_id: string }>(`/api/v1/chat/shared/${sessionId}/save`, { method: "POST" });
  return r.scorecard_id;
}
