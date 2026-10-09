import { ApiError, apiRequest, authApiFetch } from "./api-client";

/**
 * Client for the sharing, notification, evaluations-list and admin endpoints. Same plumbing as the rest of the app
 * (`apiFetch` through `authApiFetch`: cookies, CSRF, single-flight refresh), snake_case on the wire, camelCase here.
 */

const get = <T>(path: string, signal?: AbortSignal) => authApiFetch<T>(path, { signal });
const send = <T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown, headers?: Record<string, string>) =>
  authApiFetch<T>(path, { method, body, headers });

// ---------------------------------------------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------------------------------------------

export interface NotificationItem {
  id: string;
  type: string;
  title: string;
  body: string | null;
  data: Record<string, unknown> | null;
  link: string | null;
  createdAt: string;
  readAt: string | null;
  /** invite_received only: pending | accepted | declined | revoked. */
  invitationStatus: string | null;
}

interface BeNotification {
  id: string;
  type: string;
  title: string;
  body: string | null;
  data: Record<string, unknown> | null;
  link: string | null;
  created_at: string;
  read_at: string | null;
  invitation_status?: string | null;
}

function mapNotification(n: BeNotification): NotificationItem {
  return {
    id: n.id,
    type: n.type,
    title: n.title,
    body: n.body,
    data: n.data,
    link: n.link,
    createdAt: n.created_at,
    readAt: n.read_at,
    invitationStatus: n.invitation_status ?? null,
  };
}

export type UnreadResult = { changed: false; etag: string } | { changed: true; etag: string; unread: number };

/** Cheap poll: a conditional request, so an unchanged inbox answers 304 with no body. */
export async function fetchUnreadCount(etag: string | null, signal?: AbortSignal): Promise<UnreadResult> {
  const res = await apiRequest("/api/v1/notifications/unread-count", {
    headers: etag ? { "If-None-Match": etag } : undefined,
    signal,
  });
  if (res.status === 304 && etag) return { changed: false, etag };
  if (!res.ok) throw new ApiError(res.statusText || `Request failed (${res.status})`, res.status);
  const body = (await res.json()) as { unread: number };
  return { changed: true, etag: res.headers.get("ETag") ?? "", unread: body.unread };
}

export async function listNotifications(
  opts: { cursor?: string | null; limit?: number; unreadOnly?: boolean } = {},
  signal?: AbortSignal,
): Promise<{ items: NotificationItem[]; nextCursor: string | null; unread: number }> {
  const q = new URLSearchParams({ limit: String(opts.limit ?? 20) });
  if (opts.cursor) q.set("cursor", opts.cursor);
  if (opts.unreadOnly) q.set("unread_only", "true");
  const page = await get<{ items: BeNotification[]; next_cursor: string | null; unread: number }>(
    `/api/v1/notifications?${q}`,
    signal,
  );
  return { items: page.items.map(mapNotification), nextCursor: page.next_cursor, unread: page.unread };
}

export async function markNotificationRead(id: string): Promise<number> {
  return (await send<{ unread: number }>("POST", `/api/v1/notifications/${id}/read`)).unread;
}

export async function markAllNotificationsRead(): Promise<void> {
  await send("POST", "/api/v1/notifications/read-all");
}

// ---------------------------------------------------------------------------------------------------------------
// Invitations + sharing
// ---------------------------------------------------------------------------------------------------------------

export interface PersonBrief {
  id: string;
  name: string;
  email: string | null;
  username: string | null;
}

export interface Invitation {
  id: string;
  scorecardId: string;
  scorecardName: string;
  status: "pending" | "accepted" | "declined" | "revoked";
  inviter: PersonBrief;
  invitee: PersonBrief;
  createdAt: string;
  respondedAt: string | null;
  alreadyPending: boolean;
  /** The invite also carried the sender's source chat (read-only). */
  chatShared: boolean;
}

interface BeInvitation {
  id: string;
  scorecard_id: string;
  scorecard_name: string;
  status: Invitation["status"];
  inviter: PersonBrief;
  invitee: PersonBrief;
  created_at: string;
  responded_at: string | null;
  already_pending?: boolean;
  chat_shared?: boolean;
}

function mapInvitation(i: BeInvitation): Invitation {
  return {
    id: i.id,
    scorecardId: i.scorecard_id,
    scorecardName: i.scorecard_name,
    status: i.status,
    inviter: i.inviter,
    invitee: i.invitee,
    createdAt: i.created_at,
    respondedAt: i.responded_at,
    alreadyPending: Boolean(i.already_pending),
    chatShared: Boolean(i.chat_shared),
  };
}

export interface InvitationPreview {
  invitation: Invitation;
  ownerName: string;
  name: string;
  domain: string | null;
  purposeStatement: string | null;
  targetScore: number | null;
  versionNumber: number | null;
  scoringFormula: string | null;
  kpis: Array<{
    id: string;
    parentId: string | null;
    level: number;
    name: string;
    weight: number | null;
    displayOrder: number;
    includedInScoring: boolean;
  }>;
}

export async function listMyInvitations(): Promise<Invitation[]> {
  return (await get<BeInvitation[]>("/api/v1/invitations")).map(mapInvitation);
}

export async function previewInvitation(id: string): Promise<InvitationPreview> {
  const p = await get<{
    invitation: BeInvitation;
    owner_name: string;
    name: string;
    domain: string | null;
    purpose_statement: string | null;
    target_score: number | null;
    version_number: number | null;
    scoring_formula: string | null;
    kpis: Array<{
      id: string;
      parent_id: string | null;
      level: number;
      name: string;
      weight: number | null;
      display_order: number;
      included_in_scoring: boolean;
    }>;
  }>(`/api/v1/invitations/${id}/preview`);
  return {
    invitation: mapInvitation(p.invitation),
    ownerName: p.owner_name,
    name: p.name,
    domain: p.domain,
    purposeStatement: p.purpose_statement,
    targetScore: p.target_score,
    versionNumber: p.version_number,
    scoringFormula: p.scoring_formula,
    kpis: p.kpis.map((k) => ({
      id: k.id,
      parentId: k.parent_id,
      level: k.level,
      name: k.name,
      weight: k.weight,
      displayOrder: k.display_order,
      includedInScoring: k.included_in_scoring,
    })),
  };
}

export async function acceptInvitation(id: string): Promise<Invitation> {
  return mapInvitation(await send<BeInvitation>("POST", `/api/v1/invitations/${id}/accept`));
}

export async function declineInvitation(id: string): Promise<Invitation> {
  return mapInvitation(await send<BeInvitation>("POST", `/api/v1/invitations/${id}/decline`));
}

export interface Sharing {
  scorecardId: string;
  myRole: "owner" | "editor";
  owner: PersonBrief;
  collaborators: Array<{ user: PersonBrief; role: string; joinedAt: string }>;
  invitations: Invitation[];
  /** The caller's own chat behind this chart (the dialog then offers "chart and its chat"). */
  sourceChatId: string | null;
}

export async function getSharing(scorecardId: string): Promise<Sharing> {
  const s = await get<{
    scorecard_id: string;
    my_role: "owner" | "editor";
    owner: PersonBrief;
    collaborators: Array<{ user: PersonBrief; role: string; joined_at: string }>;
    invitations: BeInvitation[];
    source_chat_id?: string | null;
  }>(`/api/v1/scorecards/${scorecardId}/sharing`);
  return {
    scorecardId: s.scorecard_id,
    myRole: s.my_role,
    owner: s.owner,
    collaborators: s.collaborators.map((c) => ({ user: c.user, role: c.role, joinedAt: c.joined_at })),
    invitations: s.invitations.map(mapInvitation),
    sourceChatId: s.source_chat_id ?? null,
  };
}

/** Sends an invitation. Rejects with `ApiError` code `user_not_found` / `self_invite` / `already_collaborator` /
 * `lookup_rate_limited`, which the dialog turns into specific messages. */
export async function inviteCollaborator(
  scorecardId: string,
  identifier: string,
  includeChat = false,
): Promise<Invitation> {
  return mapInvitation(
    await send<BeInvitation>("POST", `/api/v1/scorecards/${scorecardId}/invitations`, {
      identifier,
      ...(includeChat ? { include_chat: true } : {}),
    }),
  );
}

export async function revokeInvitation(scorecardId: string, invitationId: string): Promise<void> {
  await send("DELETE", `/api/v1/scorecards/${scorecardId}/invitations/${invitationId}`);
}

export async function removeCollaborator(scorecardId: string, userId: string): Promise<void> {
  await send("DELETE", `/api/v1/scorecards/${scorecardId}/collaborators/${userId}`);
}

export async function leaveScorecard(scorecardId: string): Promise<void> {
  await send("POST", `/api/v1/scorecards/${scorecardId}/leave`);
}

export interface ActivityEntry {
  id: number;
  actorId: string | null;
  actorName: string;
  action: string;
  summary: string;
  createdAt: string;
}

/** Light list of the charts the user can open, for the Evaluations "workflow" filter. */
export async function listScorecardChoices(): Promise<Array<{ id: string; name: string }>> {
  const rows = await get<Array<{ id: string; name: string }>>("/api/v1/scorecards?limit=200");
  return rows.map((r) => ({ id: r.id, name: r.name })).sort((a, b) => a.name.localeCompare(b.name));
}

export async function listActivity(
  scorecardId: string,
  opts: { before?: number | null; limit?: number } = {},
  signal?: AbortSignal,
): Promise<{ items: ActivityEntry[]; nextBefore: number | null }> {
  const q = new URLSearchParams({ limit: String(opts.limit ?? 20) });
  if (opts.before) q.set("before", String(opts.before));
  const page = await get<{
    items: Array<{
      id: number;
      actor_id: string | null;
      actor_name: string;
      action: string;
      summary: string;
      created_at: string;
    }>;
    next_before: number | null;
  }>(`/api/v1/scorecards/${scorecardId}/activity?${q}`, signal);
  return {
    items: page.items.map((a) => ({
      id: a.id,
      actorId: a.actor_id,
      actorName: a.actor_name,
      action: a.action,
      summary: a.summary,
      createdAt: a.created_at,
    })),
    nextBefore: page.next_before,
  };
}

// ---------------------------------------------------------------------------------------------------------------
// Per-user concurrency slots
// ---------------------------------------------------------------------------------------------------------------

export interface MySlots {
  job: { evaluationId: string; batchId: string | null; status: string; scorecardId: string | null } | null;
  chat: { sessionId: string } | null;
}

export async function getMySlots(signal?: AbortSignal): Promise<MySlots> {
  const s = await get<{
    job: { evaluation_id: string; batch_id: string | null; status: string; scorecard_id?: string | null } | null;
    chat: { session_id: string } | null;
  }>("/api/v1/me/slots", signal);
  return {
    job: s.job
      ? { evaluationId: s.job.evaluation_id, batchId: s.job.batch_id, status: s.job.status, scorecardId: s.job.scorecard_id ?? null }
      : null,
    chat: s.chat ? { sessionId: s.chat.session_id } : null,
  };
}

/** Where the "You already have an evaluation running - view it" link goes for a `user_job_active` 409. */
export function activeJobHref(err: ApiError): string | null {
  if (err.code !== "user_job_active") return null;
  const batch = err.data?.batch_id;
  if (typeof batch === "string" && batch) return `/evaluations?batch=${batch}`;
  const sc = err.data?.scorecard_id;
  const ev = err.data?.evaluation_id;
  if (typeof ev === "string" && typeof sc === "string") return `/charts/${sc}/evaluations/${ev}`;
  return "/evaluations?status=active";
}

// ---------------------------------------------------------------------------------------------------------------
// Evaluations list (keyset pages, server-side filters)
// ---------------------------------------------------------------------------------------------------------------

export type EvalSort = "newest" | "oldest" | "score_desc" | "score_asc" | "name_asc" | "name_desc";

export interface EvalQuery {
  status: "all" | "active" | "completed" | "failed";
  scorecardId: string | null;
  batchId: string | null;
  bands: string[];
  minScore: string;
  maxScore: string;
  meetsTarget: "any" | "yes" | "no";
  dateFrom: string;
  dateTo: string;
  q: string;
  runner: "any" | "me" | "others";
  shared: "any" | "shared" | "private";
  sort: EvalSort;
}

export const DEFAULT_EVAL_QUERY: EvalQuery = {
  status: "all",
  scorecardId: null,
  batchId: null,
  bands: [],
  minScore: "",
  maxScore: "",
  meetsTarget: "any",
  dateFrom: "",
  dateTo: "",
  q: "",
  runner: "any",
  shared: "any",
  sort: "newest",
};

export interface EvalRow {
  id: string;
  name: string;
  status: string;
  stage: string | null;
  finalWeightedScore: number | null;
  ragBand: string | null;
  createdAt: string;
  submittedAt: string | null;
  queuedAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  subjectName: string | null;
  subjectEmail: string | null;
  batchId: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  attempt: number;
  domain: string | null;
  scorecardId: string;
  scorecardVersionId: string;
  scorecardName: string;
  targetScore: number | null;
  runnerId: string;
  runnerName: string;
  isMine: boolean;
  shared: boolean;
}

interface BeEvalRow {
  id: string;
  name: string;
  status: string;
  stage: string | null;
  final_weighted_score: number | null;
  rag_band: string | null;
  created_at: string;
  submitted_at: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  subject_name: string | null;
  subject_email: string | null;
  batch_id: string | null;
  error_code: string | null;
  error_message: string | null;
  attempt: number;
  domain: string | null;
  scorecard_id: string;
  scorecard_version_id: string;
  scorecard_name: string;
  target_score: number | null;
  runner_id: string;
  runner_name: string;
  is_mine: boolean;
  shared: boolean;
}

function mapEvalRow(r: BeEvalRow): EvalRow {
  return {
    id: r.id,
    name: r.name,
    status: r.status,
    stage: r.stage,
    finalWeightedScore: r.final_weighted_score,
    ragBand: r.rag_band,
    createdAt: r.created_at,
    submittedAt: r.submitted_at,
    queuedAt: r.queued_at,
    startedAt: r.started_at,
    finishedAt: r.finished_at,
    subjectName: r.subject_name,
    subjectEmail: r.subject_email,
    batchId: r.batch_id,
    errorCode: r.error_code,
    errorMessage: r.error_message,
    attempt: r.attempt,
    domain: r.domain,
    scorecardId: r.scorecard_id,
    scorecardVersionId: r.scorecard_version_id,
    scorecardName: r.scorecard_name,
    targetScore: r.target_score,
    runnerId: r.runner_id,
    runnerName: r.runner_name,
    isMine: r.is_mine,
    shared: r.shared,
  };
}

/** The filter as the server's `EvalFilter` JSON (used by "select all matching": delete / export). */
export function evalFilterBody(q: EvalQuery): Record<string, unknown> {
  const body: Record<string, unknown> = { status: q.status };
  if (q.scorecardId) body.scorecard_id = q.scorecardId;
  if (q.batchId) body.batch_id = q.batchId;
  if (q.bands.length) body.band = q.bands;
  if (q.minScore.trim() !== "") body.min_score = Number(q.minScore);
  if (q.maxScore.trim() !== "") body.max_score = Number(q.maxScore);
  if (q.meetsTarget !== "any") body.meets_target = q.meetsTarget === "yes";
  if (q.dateFrom) body.date_from = q.dateFrom;
  if (q.dateTo) body.date_to = q.dateTo;
  if (q.q.trim()) body.q = q.q.trim();
  if (q.runner !== "any") body.runner = q.runner;
  if (q.shared !== "any") body.shared = q.shared === "shared";
  return body;
}

export function evalQueryString(q: EvalQuery, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams();
  const f = evalFilterBody(q);
  for (const [k, v] of Object.entries(f)) {
    if (Array.isArray(v)) v.forEach((x) => p.append(k, String(x)));
    else p.set(k, String(v));
  }
  p.set("sort", q.sort);
  for (const [k, v] of Object.entries(extra)) p.set(k, v);
  return p.toString();
}

export interface EvalPage {
  items: EvalRow[];
  nextCursor: string | null;
  total: number;
  selectable: number;
  exportable: number;
  totalCapped: boolean;
  countCap: number;
}

export async function fetchEvaluationsPage(
  q: EvalQuery,
  opts: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<EvalPage> {
  const extra: Record<string, string> = { limit: String(opts.limit ?? 40) };
  if (opts.cursor) extra.cursor = opts.cursor;
  const page = await get<{
    items: BeEvalRow[];
    next_cursor: string | null;
    total: number;
    selectable: number;
    exportable: number;
    total_capped: boolean;
    count_cap: number;
  }>(`/api/v1/evaluations/page?${evalQueryString(q, extra)}`, opts.signal);
  return {
    items: page.items.map(mapEvalRow),
    nextCursor: page.next_cursor,
    total: page.total,
    selectable: page.selectable,
    exportable: page.exportable,
    totalCapped: page.total_capped,
    countCap: page.count_cap,
  };
}

export async function refreshEvaluationRows(ids: string[], signal?: AbortSignal): Promise<EvalRow[]> {
  if (ids.length === 0) return [];
  const rows = await authApiFetch<BeEvalRow[]>("/api/v1/evaluations/refresh", {
    method: "POST",
    body: { ids: ids.slice(0, 100) },
    signal,
  });
  return rows.map(mapEvalRow);
}

/** "Select all matching" is a server-side selection: the filter (+ the rows the user un-ticked), never a list of ids. */
export type BulkTarget = { ids: string[] } | { filter: EvalQuery; excludeIds: string[] };

export async function bulkDeleteEvaluations(target: BulkTarget): Promise<{ deleted: number; skipped: number }> {
  const body =
    "ids" in target
      ? { ids: target.ids }
      : { filter: evalFilterBody(target.filter), exclude_ids: target.excludeIds };
  return send("POST", "/api/v1/evaluations/bulk-delete", body);
}

// ---------------------------------------------------------------------------------------------------------------
// Admin: users
// ---------------------------------------------------------------------------------------------------------------

export interface AdminUser {
  id: string;
  username: string | null;
  email: string;
  name: string;
  role: "admin" | "user";
  isActive: boolean;
  emailVerified: boolean;
  createdAt: string;
}

interface BeAdminUser {
  id: string;
  username: string | null;
  email: string;
  name: string;
  role: "admin" | "user";
  is_active: boolean;
  email_verified: boolean;
  created_at: string;
}

function mapAdminUser(u: BeAdminUser): AdminUser {
  return {
    id: u.id,
    username: u.username,
    email: u.email,
    name: u.name,
    role: u.role,
    isActive: u.is_active,
    emailVerified: u.email_verified,
    createdAt: u.created_at,
  };
}

export async function listAdminUsers(
  opts: { q?: string; role?: string; active?: string; skip?: number; limit?: number },
  signal?: AbortSignal,
): Promise<{ items: AdminUser[]; total: number }> {
  const p = new URLSearchParams({ limit: String(opts.limit ?? 50), skip: String(opts.skip ?? 0) });
  if (opts.q?.trim()) p.set("q", opts.q.trim());
  if (opts.role && opts.role !== "all") p.set("role", opts.role);
  if (opts.active === "active") p.set("active", "true");
  if (opts.active === "inactive") p.set("active", "false");
  const r = await get<{ items: BeAdminUser[]; total: number }>(`/api/v1/admin/users?${p}`, signal);
  return { items: r.items.map(mapAdminUser), total: r.total };
}

export async function createAdminUser(input: {
  email: string;
  name: string;
  username: string;
  role: "admin" | "user";
  password: string;
}): Promise<AdminUser> {
  return mapAdminUser(
    await send<BeAdminUser>("POST", "/api/v1/admin/users", {
      email: input.email.trim(),
      name: input.name.trim(),
      username: input.username.trim() || null,
      role: input.role,
      password: input.password,
    }),
  );
}

export async function updateAdminUser(
  id: string,
  patch: { email?: string; name?: string; username?: string | null; role?: "admin" | "user" },
): Promise<AdminUser> {
  const body: Record<string, unknown> = {};
  if (patch.email !== undefined) body.email = patch.email.trim();
  if (patch.name !== undefined) body.name = patch.name.trim();
  if (patch.role !== undefined) body.role = patch.role;
  if (patch.username !== undefined) {
    if (patch.username === null || patch.username.trim() === "") body.clear_username = true;
    else body.username = patch.username.trim();
  }
  return mapAdminUser(await send<BeAdminUser>("PATCH", `/api/v1/admin/users/${id}`, body));
}

export async function setAdminUserPassword(id: string, password: string): Promise<void> {
  await send("POST", `/api/v1/admin/users/${id}/password`, { password });
}

export async function setAdminUserActive(id: string, active: boolean): Promise<AdminUser> {
  return mapAdminUser(await send<BeAdminUser>("POST", `/api/v1/admin/users/${id}/${active ? "reactivate" : "deactivate"}`));
}

export async function deleteAdminUser(id: string): Promise<void> {
  await send("DELETE", `/api/v1/admin/users/${id}`);
}

export async function changeMyPassword(currentPassword: string, newPassword: string): Promise<void> {
  await send("POST", "/api/v1/me/password", { current_password: currentPassword, new_password: newPassword });
}
