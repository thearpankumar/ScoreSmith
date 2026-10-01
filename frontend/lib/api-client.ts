import type {
  ChatMessage,
  ChatRole,
  ChatSession,
  ChatSessionStatus,
  ChatTurnEvent,
  ClarifyingQuestion,
  CreateEvaluationInput,
  DraftKpi,
  Evaluation,
  EvaluationKpiResult,
  EvaluationStatus,
  Guideline,
  KpiNode,
  RagBandKey,
  Scorecard,
  ScorecardDraft,
  ScorecardFilters,
  ScorecardVersion,
  SimilarScorecardSuggestion,
  User,
  UserRole,
} from "./types";

/**
 * Real API-client: every function below calls the FastAPI backend (see
 * `backend/app/api/v1/`) and maps its Pydantic response shapes onto the frontend's
 * camelCase types. Components only ever import from this file, never from
 * `mock-data.ts` (kept around only as illustrative sample-shape reference / a fallback
 * for `getCachedEvaluation`'s browser-only cache).
 *
 * --- Base URL: client vs. server-side fetch ---
 * `NEXT_PUBLIC_API_BASE_URL` is exposed to the browser bundle and must resolve the way
 * the *host machine* sees the backend (`http://localhost:8000` per infra/.env.example).
 * But every page here is a Next.js Server Component that also calls these functions
 * during SSR, running *inside the frontend container* — there, "localhost" means the
 * frontend container itself, not the backend one. Server-side calls instead use
 * `INTERNAL_API_BASE_URL` (`http://backend:8000`, the Compose service DNS name; see
 * infra/docker-compose.yml). `apiBase()` picks the right one via `typeof window`.
 *
 * --- Dev auth stub ---
 * `backend/app/deps.py::get_current_user` trusts a raw `X-User-Id` header. There's no
 * login flow yet (real OIDC is deferred per the plan), so we resolve a stable "current
 * user" by looking up the seed script's designer account by email
 * (`backend/app/scripts/generate_scenarios.py` -> `designer@qualityscorecard.local`)
 * rather than hardcoding its (randomly-generated-at-seed-time) UUID, so this keeps
 * working across `python -m app.scripts.seed --reset` runs.
 *
 * --- Bedrock-unavailable errors ---
 * Endpoints backed by AWS Bedrock (`POST /chat/sessions[...]`, `POST
 * /scorecards/suggest-similar`, `POST /evaluations/{id}/run`) return HTTP 502 with a
 * `{"detail": "..."}` body when Bedrock is unreachable (see
 * `backend/app/ai/bedrock_client.py::BedrockUnavailableError`, surfaced as 502 by each
 * router — see `chat.py`, `scorecards.py`, `evaluations.py`). `apiFetch` turns that into
 * a typed `BedrockUnavailableError` here so components can show a specific, friendly
 * message instead of a generic failure or an uncaught crash.
 */

// ---------------------------------------------------------------------------
// Base URL + low-level fetch plumbing
// ---------------------------------------------------------------------------

function apiBase(): string {
  if (typeof window === "undefined") {
    return process.env.INTERNAL_API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
  }
  return process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";
}

/** Kept for compatibility with anything that wants "the" base URL (e.g. debugging). */
export const API_BASE_URL = apiBase();

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Mirrors the backend's `BedrockUnavailableError` (always surfaced as HTTP 502). */
export class BedrockUnavailableError extends ApiError {
  constructor(message: string) {
    super(message, 502);
    this.name = "BedrockUnavailableError";
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** Attaches the dev-auth-stub X-User-Id header (see module docstring). */
  auth?: boolean;
}

async function apiFetch<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.auth) {
    headers["X-User-Id"] = await getDevUserId();
  }

  let res: Response;
  try {
    res = await fetch(`${apiBase()}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    });
  } catch (err) {
    // Next.js throws its own internal sentinel error out of `fetch(..., { cache:
    // "no-store" })` during static generation, to signal "this route needs dynamic
    // rendering, retry it as a dynamic request" — it is not a real fetch failure. It's
    // tagged with `digest === "DYNAMIC_SERVER_USAGE"`. Re-wrapping it into a generic
    // ApiError here (as this catch used to do unconditionally) hid that tag from Next,
    // which made `next build` hard-fail instead of marking the route dynamic. Every page
    // that calls into this client also now sets `export const dynamic = "force-dynamic"`
    // (see e.g. app/charts/page.tsx) so this shouldn't fire in practice, but rethrowing
    // it unmodified here too is the robust, defense-in-depth fix at the source.
    if (
      err &&
      typeof err === "object" &&
      "digest" in err &&
      typeof (err as { digest?: unknown }).digest === "string" &&
      (err as { digest: string }).digest.startsWith("DYNAMIC_SERVER_USAGE")
    ) {
      throw err;
    }
    throw new ApiError(
      `Could not reach the backend at ${apiBase()}${path} (${err instanceof Error ? err.message : String(err)}). Is it running?`,
      0,
    );
  }

  if (res.status === 204) return undefined as T;

  const raw = await res.text();
  let payload: unknown;
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = raw;
    }
  }

  if (!res.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in (payload as Record<string, unknown>)
        ? String((payload as Record<string, unknown>).detail)
        : typeof payload === "string" && payload
          ? payload
          : res.statusText || `Request failed (${res.status})`;
    if (res.status === 502) {
      throw new BedrockUnavailableError(detail);
    }
    throw new ApiError(detail, res.status);
  }

  return payload as T;
}

// ---------------------------------------------------------------------------
// Backend response shapes (snake_case, mirroring backend/app/schemas/*.py)
// ---------------------------------------------------------------------------

interface BeUser {
  id: string;
  email: string;
  name: string;
  org_id: string | null;
  role: string;
  auth_provider_id: string | null;
  created_at: string;
  updated_at: string;
}

interface BeScorecard {
  id: string;
  name: string;
  owner_id: string;
  domain: string | null;
  purpose_statement: string | null;
  scope: string | null;
  target_score: number | null;
  status: string;
  current_version_id: string | null;
  created_at: string;
  updated_at: string;
}

interface BeGuideline {
  id: string;
  kpi_node_id: string;
  score_level: number;
  qualitative_text: string;
  quantitative_criteria: Record<string, unknown> | null;
}

interface BeKpiNode {
  id: string;
  scorecard_version_id: string;
  parent_id: string | null;
  path: string;
  level: number;
  name: string;
  // Nullable: a category/grouping node carries no weight of its own — see backend
  // migration 0008_category_nodes_no_weight.
  weight: number | null;
  display_order: number;
  included_in_scoring: boolean;
  guidelines?: BeGuideline[];
}

interface BeScorecardVersion {
  id: string;
  scorecard_id: string;
  version_number: number;
  guideline_notes: string | null;
  created_by: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  scoring_formula: string | null;
  kpi_nodes?: BeKpiNode[];
}

interface BeEvaluationKpiResult {
  id: string;
  evaluation_id: string;
  kpi_node_id: string;
  score: number;
  matched_guideline_level: number | null;
  reasoning_text: string | null;
  evidence_quotes: unknown;
}

interface BeEvaluation {
  id: string;
  scorecard_version_id: string;
  name: string;
  evaluated_by: string;
  input_reference: Record<string, unknown> | null;
  status: string;
  final_weighted_score: number | null;
  rag_band: string | null;
  submitted_at: string | null;
  domain: string | null;
  created_at: string;
  kpi_results?: BeEvaluationKpiResult[];
}

interface BeChatSession {
  id: string;
  user_id: string;
  status: string;
  /** Real, AI-generated (or, for a "Refine with assistant" session, deterministically
   * derived) title — see backend/app/models/chat_session.py::ChatSession.title. Null only
   * very briefly (right after session creation, before the title call resolves) or if
   * title generation itself failed — see mapChatSession's fallback. */
  title: string | null;
  context_summary: string | null;
  target_scorecard_id: string | null;
  created_at: string;
  last_activity_at: string;
}

interface BeChatMessage {
  id: string;
  session_id: string;
  role: string;
  content: string;
  tool_calls: unknown;
  created_at: string;
}

interface BeChatTurnEvent {
  id: string;
  session_id: string;
  turn_started_at: string;
  actor: string;
  event_type: string;
  message: string;
  round: number;
  created_at: string;
}

interface BeClarifyingQuestion {
  question: string;
  options: string[];
  missing_fields: string[];
}

interface BeKpiDraft {
  name: string;
  weight: number | null;
  level: number;
  parent_name: string | null;
  included_in_scoring?: boolean;
  guidelines: Record<string, { qualitative_text: string; quantitative_criteria?: unknown }>;
}

interface BeScorecardDraft {
  name: string | null;
  purpose: string | null;
  domain: string | null;
  audience: string | null;
  target_score: number | null;
  kpis: BeKpiDraft[];
  scoring_formula?: string | null;
}

interface BeChatTurn {
  session_id: string;
  status: string;
  draft: BeScorecardDraft;
  question: BeClarifyingQuestion | null;
  /** Populated when status === "awaiting_similar_choice" (see
   * backend/app/ai/scorecard_builder.py's check_similarity/suggest_similar nodes) —
   * reuse-suggestion cards surfaced by the LangGraph chat graph itself, before
   * propose_kpis ever runs. */
  similar_suggestions: BeSuggestSimilarResult[] | null;
  assistant_message: string | null;
  materialized_scorecard_id: string | null;
  materialized_scorecard_version_id: string | null;
  /** True when a turn is currently running server-side for this session (see
   * backend/app/models/chat_session.py::ChatSession.turn_in_progress /
   * pending_turn_started_at) — the durable, refresh-surviving "still working" marker. */
  turn_in_progress?: boolean;
  /** See BeChatSession.title — carried on every turn response (not just GET) so a
   * brand-new session's own first, blocking POST already returns its real title, with no
   * second round-trip needed. */
  title?: string | null;
}

interface BeSuggestSimilarResult {
  scorecard_id: string;
  scorecard_version_id: string;
  name: string;
  domain: string | null;
  similarity: number;
  purpose_statement: string | null;
}

// ---------------------------------------------------------------------------
// Shared caches: users (for the dev-auth stub + display-name enrichment)
// ---------------------------------------------------------------------------

/** Seeded by backend/app/scripts/generate_scenarios.py::run_all_scenarios. */
const DEV_USER_EMAIL = "designer@qualityscorecard.local";

let usersPromise: Promise<BeUser[]> | null = null;

async function getAllUsers(): Promise<BeUser[]> {
  if (!usersPromise) {
    usersPromise = apiFetch<BeUser[]>("/api/v1/users?limit=200").catch((err) => {
      usersPromise = null; // allow a retry on the next call rather than caching a failure
      throw err;
    });
  }
  return usersPromise;
}

async function getUserNameMap(): Promise<Map<string, string>> {
  const users = await getAllUsers();
  return new Map(users.map((u) => [u.id, u.name]));
}

let devUserIdPromise: Promise<string> | null = null;

async function getDevUserId(): Promise<string> {
  if (!devUserIdPromise) {
    devUserIdPromise = getAllUsers()
      .then((users) => {
        const match = users.find((u) => u.email === DEV_USER_EMAIL);
        if (!match) {
          throw new ApiError(
            `Dev auth stub: no seeded user with email "${DEV_USER_EMAIL}" was found. Run ` +
              `"python -m app.scripts.seed" against this database first.`,
            404,
          );
        }
        return match.id;
      })
      .catch((err) => {
        devUserIdPromise = null;
        throw err;
      });
  }
  return devUserIdPromise;
}

function mapUser(be: BeUser): User {
  return {
    id: be.id,
    name: be.name,
    email: be.email,
    // Backend `role` is a free-form string (default "member" — see
    // backend/app/schemas/user.py); the frontend's UserRole is a narrower display-only
    // union. Cast rather than validate: this is a dev-only stub with no real RBAC yet
    // (see SettingsForm), so an unrecognized role just displays as-is via `String(role)`.
    role: be.role as UserRole,
    orgId: be.org_id,
  };
}

/** Settings page: the current dev user's full profile (Settings > profile form), via the
 * same designer@qualityscorecard.local resolution the dev-auth stub already uses
 * elsewhere in this file (see getDevUserId/DEV_USER_EMAIL) — not a second mechanism. */
export async function getCurrentUser(): Promise<User> {
  const users = await getAllUsers();
  const match = users.find((u) => u.email === DEV_USER_EMAIL);
  if (!match) {
    throw new ApiError(
      `Dev auth stub: no seeded user with email "${DEV_USER_EMAIL}" was found. Run ` +
        `"python -m app.scripts.seed" against this database first.`,
      404,
    );
  }
  return mapUser(match);
}

/** Settings page "Save changes": `PATCH /api/v1/users/{id}` for the current dev user. */
export async function updateCurrentUser(patch: { name?: string; email?: string }): Promise<User> {
  const id = await getDevUserId();
  const updated = await apiFetch<BeUser>(`/api/v1/users/${id}`, { method: "PATCH", auth: true, body: patch });
  usersPromise = null; // invalidate the cached user list so name/email enrichment picks up the change
  return mapUser(updated);
}

// ---------------------------------------------------------------------------
// Mapping helpers: backend (snake_case) -> frontend (camelCase) shapes
// ---------------------------------------------------------------------------

const RAG_BAND_MAP: Record<string, RagBandKey> = {
  band_10_9: "excellent",
  band_8: "good",
  band_7: "acceptable",
  band_6: "needs-improvement",
  band_5: "weak",
  band_4: "poor",
  band_3_0: "critical",
};

function mapRagBand(band: string | null): RagBandKey {
  return (band && RAG_BAND_MAP[band]) || "critical";
}

function countLeafKpis(nodes: BeKpiNode[]): number {
  const parentIds = new Set(nodes.map((n) => n.parent_id).filter((id): id is string => id != null));
  return nodes.filter((n) => !parentIds.has(n.id)).length;
}

function mapScorecard(be: BeScorecard, ownerName: string, kpiCount: number): Scorecard {
  return {
    id: be.id,
    name: be.name,
    domain: be.domain ?? "General",
    ownerId: be.owner_id,
    ownerName,
    purposeStatement: be.purpose_statement ?? "",
    scope: be.scope ?? "",
    targetScore: be.target_score ?? 0,
    status: be.status as Scorecard["status"],
    currentVersionId: be.current_version_id ?? "",
    kpiCount,
    createdAt: be.created_at,
    updatedAt: be.updated_at,
  };
}

function mapGuideline(g: BeGuideline): Guideline {
  return {
    id: g.id,
    kpiNodeId: g.kpi_node_id,
    scoreLevel: g.score_level,
    qualitativeText: g.qualitative_text,
    // Frontend models quantitative criteria as free text; backend stores it as a JSONB
    // object (e.g. {"metric": "defect_rate", "op": "<=", "value": 2}) — stringify it
    // rather than reshaping the Guideline type for one field.
    quantitativeCriteria: g.quantitative_criteria ? formatQuantitativeCriteria(g.quantitative_criteria) : undefined,
  };
}

/** Human-readable form of a quantitative-criteria JSONB object, e.g.
 * `{"metric": "defect_rate", "operator": "<=", "value": 2}` -> `defect_rate <= 2`. */
function formatQuantitativeCriteria(c: Record<string, unknown>): string {
  // Free-text criteria entered through the Overview tab's structure editor are stored as
  // {"description": "..."} (see upsertGuideline) — show the text itself.
  if (typeof c.description === "string" && Object.keys(c).length === 1) return c.description;
  const op = c.operator ?? c.op;
  if (c.metric !== undefined && op !== undefined && c.value !== undefined) {
    return `${String(c.metric)} ${String(op)} ${String(c.value)}`;
  }
  const entries = Object.entries(c);
  return entries.length > 0 ? entries.map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(", ") : "";
}

function mapKpiNode(n: BeKpiNode): KpiNode {
  return {
    id: n.id,
    scorecardVersionId: n.scorecard_version_id,
    parentId: n.parent_id,
    path: n.path,
    level: n.level as 1 | 2 | 3 | 4,
    name: n.name,
    weight: n.weight,
    displayOrder: n.display_order,
    includedInScoring: n.included_in_scoring,
    guidelines: n.guidelines ? n.guidelines.map(mapGuideline) : undefined,
  };
}

function mapScorecardVersion(v: BeScorecardVersion): ScorecardVersion {
  return {
    id: v.id,
    scorecardId: v.scorecard_id,
    versionNumber: v.version_number,
    guidelineNotes: v.guideline_notes ?? "",
    createdBy: v.created_by,
    createdAt: v.created_at,
    isActive: v.is_active,
    scoringFormula: v.scoring_formula,
    kpiNodes: (v.kpi_nodes ?? []).map(mapKpiNode),
  };
}

async function fetchBeScorecard(id: string): Promise<BeScorecard | undefined> {
  try {
    return await apiFetch<BeScorecard>(`/api/v1/scorecards/${id}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return undefined;
    throw err;
  }
}

/** Uses the additive flat `GET /api/v1/scorecard-versions/{id}` endpoint (added for this
 * integration — see backend/app/api/v1/kpi_nodes.py), so a version can be resolved
 * without already knowing its parent scorecard_id (e.g. from an evaluation row, which
 * only carries scorecard_version_id). */
async function fetchBeScorecardVersion(versionId: string): Promise<BeScorecardVersion | undefined> {
  try {
    return await apiFetch<BeScorecardVersion>(`/api/v1/scorecard-versions/${versionId}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return undefined;
    throw err;
  }
}

async function getOwnerName(ownerId: string): Promise<string> {
  const map = await getUserNameMap();
  return map.get(ownerId) ?? "Unknown";
}

// ---------------------------------------------------------------------------
// Scorecards
// ---------------------------------------------------------------------------

export async function listScorecards(filters?: ScorecardFilters): Promise<Scorecard[]> {
  const params = new URLSearchParams({ limit: "200" });
  if (filters?.domain && filters.domain !== "all") params.set("domain", filters.domain);
  if (filters?.owner && filters.owner !== "all") params.set("owner_id", filters.owner);

  const [rows, userMap] = await Promise.all([
    apiFetch<BeScorecard[]>(`/api/v1/scorecards?${params.toString()}`),
    getUserNameMap(),
  ]);

  const kpiCounts = await Promise.all(
    rows.map(async (r) => {
      if (!r.current_version_id) return 0;
      const version = await fetchBeScorecardVersion(r.current_version_id);
      return version ? countLeafKpis(version.kpi_nodes ?? []) : 0;
    }),
  );

  let results = rows.map((r, i) => mapScorecard(r, userMap.get(r.owner_id) ?? "Unknown", kpiCounts[i]));

  if (filters?.search) {
    const q = filters.search.toLowerCase();
    results = results.filter((s) => s.name.toLowerCase().includes(q) || s.purposeStatement.toLowerCase().includes(q));
  }
  if (filters?.status && filters.status !== "all") {
    results = results.filter((s) => s.status === filters.status);
  }

  return results;
}

export async function getScorecard(id: string): Promise<Scorecard | undefined> {
  const be = await fetchBeScorecard(id);
  if (!be) return undefined;
  const [ownerName, version] = await Promise.all([
    getOwnerName(be.owner_id),
    be.current_version_id ? fetchBeScorecardVersion(be.current_version_id) : Promise.resolve(undefined),
  ]);
  return mapScorecard(be, ownerName, version ? countLeafKpis(version.kpi_nodes ?? []) : 0);
}

/** versionId -> version_number for every version of a scorecard (History tab labels). */
export async function listScorecardVersionNumbers(scorecardId: string): Promise<Record<string, number>> {
  const versions = await apiFetch<BeScorecardVersion[]>(`/api/v1/scorecards/${scorecardId}/versions`);
  return Object.fromEntries(versions.map((v) => [v.id, v.version_number]));
}

export async function getScorecardVersion(versionId: string): Promise<ScorecardVersion | undefined> {
  const be = await fetchBeScorecardVersion(versionId);
  return be ? mapScorecardVersion(be) : undefined;
}

/** Convenience: fetch a scorecard together with its currently active version. */
export async function getScorecardWithVersion(
  id: string,
): Promise<{ scorecard: Scorecard; version: ScorecardVersion } | undefined> {
  const be = await fetchBeScorecard(id);
  if (!be || !be.current_version_id) return undefined;
  const [ownerName, versionBe] = await Promise.all([
    getOwnerName(be.owner_id),
    fetchBeScorecardVersion(be.current_version_id),
  ]);
  if (!versionBe) return undefined;
  return {
    scorecard: mapScorecard(be, ownerName, countLeafKpis(versionBe.kpi_nodes ?? [])),
    version: mapScorecardVersion(versionBe),
  };
}

// ---------------------------------------------------------------------------
// Scorecard structure editing (Overview tab "Edit structure" mode)
//
// Thin wrappers over the existing auth-gated CRUD routes in
// backend/app/api/v1/kpi_nodes.py + scorecards.py. Only LEAF kpi_nodes (no children) are
// weighted — a category/grouping node has none of its own (see migration
// 0008_category_nodes_no_weight). The DB enforces "every leaf in the scorecard version
// sums to 100 together" with a DEFERRED constraint trigger checked at each request's
// COMMIT, which shapes how the editor sequences calls (see KpiStructureTree):
//   - weights are only ever changed as a whole batch (bulk PATCH), covering every leaf
//     touched by an edit;
//   - a new (always-leaf) KPI starts at weight 0, or at 100 when it's the very first KPI
//     in an otherwise-empty scorecard;
//   - deleting a weighted leaf KPI first moves its weight onto its immediate siblings,
//     then deletes.
// ---------------------------------------------------------------------------

export async function renameKpiNode(nodeId: string, name: string): Promise<void> {
  await apiFetch(`/api/v1/kpi-nodes/${nodeId}`, { method: "PATCH", auth: true, body: { name } });
}

/** Atomic multi-node weight update (`PATCH /kpi-nodes/weights`) — every sibling group
 * touched must still sum to 100 when the request commits, or the backend returns 409. */
export async function updateKpiWeights(weights: Array<{ id: string; weight: number }>): Promise<void> {
  if (weights.length === 0) return;
  await apiFetch("/api/v1/kpi-nodes/weights", { method: "PATCH", auth: true, body: { weights } });
}

/** Clears a node's stored weight (a node that now has children is a category and carries
 * no weight of its own — see backend migration 0008_category_nodes_no_weight). */
export async function clearKpiNodeWeight(nodeId: string): Promise<void> {
  await apiFetch(`/api/v1/kpi-nodes/${nodeId}`, { method: "PATCH", auth: true, body: { weight: null } });
}

export async function createKpiNode(input: {
  versionId: string;
  parentId: string | null;
  level: number;
  name: string;
  weight: number;
  displayOrder: number;
}): Promise<void> {
  await apiFetch(`/api/v1/scorecard-versions/${input.versionId}/kpi-nodes/bulk`, {
    method: "POST",
    auth: true,
    body: {
      nodes: [
        {
          parent_id: input.parentId,
          level: input.level,
          name: input.name,
          weight: input.weight,
          display_order: input.displayOrder,
        },
      ],
    },
  });
}

export async function deleteKpiNode(nodeId: string): Promise<void> {
  await apiFetch(`/api/v1/kpi-nodes/${nodeId}`, { method: "DELETE", auth: true });
}

/** Create or update one guideline rung. `quantitativeText` undefined = leave the stored
 * criteria untouched (so structured criteria like {"metric","operator","value"} are never
 * clobbered by an edit that only changed the qualitative text); "" = clear it; any other
 * string is stored as {"description": text}. */
export async function upsertGuideline(input: {
  nodeId: string;
  guidelineId?: string;
  scoreLevel: number;
  qualitativeText: string;
  quantitativeText?: string;
}): Promise<void> {
  const body: Record<string, unknown> = { qualitative_text: input.qualitativeText };
  if (input.quantitativeText !== undefined) {
    const text = input.quantitativeText.trim();
    body.quantitative_criteria = text ? { description: text } : null;
  }
  if (input.guidelineId) {
    await apiFetch(`/api/v1/kpi-nodes/${input.nodeId}/guidelines/${input.guidelineId}`, {
      method: "PATCH",
      auth: true,
      body,
    });
  } else {
    await apiFetch(`/api/v1/kpi-nodes/${input.nodeId}/guidelines`, {
      method: "POST",
      auth: true,
      body: { ...body, score_level: input.scoreLevel },
    });
  }
}

export async function updateScorecardStatus(scorecardId: string, status: Scorecard["status"]): Promise<void> {
  await apiFetch(`/api/v1/scorecards/${scorecardId}`, { method: "PATCH", auth: true, body: { status } });
}

/** Hover-delete on a scorecard card in the Charts library grid (`DELETE
 * /api/v1/scorecards/{id}` — backend/app/api/v1/scorecards.py::delete_scorecard). */
export async function deleteScorecard(scorecardId: string): Promise<void> {
  await apiFetch(`/api/v1/scorecards/${scorecardId}`, { method: "DELETE", auth: true });
}

// ---------------------------------------------------------------------------
// Custom scoring formula (Part 2b) — backend/app/ai/scoring_formula.py
// ---------------------------------------------------------------------------

export interface FormulaValidation {
  valid: boolean;
  error: string | null;
  unusedKpis: string[];
}

interface BeFormulaValidation {
  valid: boolean;
  error: string | null;
  unused_kpis: string[];
}

/**
 * Real backend validation (`POST /scorecards/{id}/versions/{id}/validate-formula`) — the
 * SAME `app/ai/scoring_formula.py::validate` the PATCH endpoint below and the LLM
 * builder's `update_scoring_formula` tool use, so the editor's live green/red indicator
 * can never drift from what actually gets accepted/evaluated. Called on every keystroke
 * (debounced by the caller), so network errors are swallowed into a non-blocking
 * "can't validate right now" result rather than thrown.
 */
export async function validateScoringFormula(
  scorecardId: string,
  versionId: string,
  formula: string | null,
): Promise<FormulaValidation> {
  const res = await apiFetch<BeFormulaValidation>(
    `/api/v1/scorecards/${scorecardId}/versions/${versionId}/validate-formula`,
    { method: "POST", body: { formula } },
  );
  return { valid: res.valid, error: res.error, unusedKpis: res.unused_kpis };
}

/**
 * Same live validation as `validateScoringFormula` above, but for a chat draft that has
 * no `scorecard_id`/`version_id` yet (it isn't materialized until the user confirms — see
 * `backend/app/ai/draft_materialize.py`). Calls the generic
 * `POST /scorecards/validate-formula` endpoint, given the draft's current KPI names
 * directly rather than looking them up from a saved version — same underlying
 * `app/ai/scoring_formula.py::validate`, so this can never disagree with the chart-detail
 * page's own validation of the same expression once the scorecard is saved. Used by
 * `LivePreviewPanel`'s formula section (Issue 2 — see task notes) via `ScoringFormulaPanel`
 * /`ScoringFormulaBuilderDialog`'s injected `onValidate` prop.
 */
export async function validateScoringFormulaDraft(
  formula: string | null,
  kpiNames: string[],
): Promise<FormulaValidation> {
  const res = await apiFetch<BeFormulaValidation>("/api/v1/scorecards/validate-formula", {
    method: "POST",
    body: { formula, kpi_names: kpiNames },
  });
  return { valid: res.valid, error: res.error, unusedKpis: res.unused_kpis };
}

/**
 * Persists a (already-validated) custom formula — `null` clears it, reverting to the
 * default weighted average. Backend re-validates server-side regardless (see
 * `update_scorecard_version` in app/api/v1/scorecards.py) and returns 422 with the
 * specific error if it's somehow invalid, so a caller should still handle rejection.
 */
export async function updateScoringFormula(
  scorecardId: string,
  versionId: string,
  formula: string | null,
): Promise<void> {
  await apiFetch(`/api/v1/scorecards/${scorecardId}/versions/${versionId}`, {
    method: "PATCH",
    auth: true,
    body: { scoring_formula: formula },
  });
}

/** Toggles a single KPI's `included_in_scoring` flag (Part 2a). */
export async function updateKpiIncludedInScoring(nodeId: string, includedInScoring: boolean): Promise<void> {
  await apiFetch(`/api/v1/kpi-nodes/${nodeId}`, {
    method: "PATCH",
    auth: true,
    body: { included_in_scoring: includedInScoring },
  });
}

/** Dev-auth-stub "current user" id (see module docstring) — used for owner checks. */
export async function getCurrentUserId(): Promise<string> {
  return getDevUserId();
}

export async function listScorecardDomains(): Promise<string[]> {
  const rows = await apiFetch<BeScorecard[]>("/api/v1/scorecards?limit=200");
  return Array.from(new Set(rows.map((r) => r.domain).filter((d): d is string => !!d))).sort();
}

export async function listScorecardOwners(): Promise<Array<{ id: string; name: string }>> {
  const [rows, userMap] = await Promise.all([
    apiFetch<BeScorecard[]>("/api/v1/scorecards?limit=200"),
    getUserNameMap(),
  ]);
  const seen = new Map<string, string>();
  rows.forEach((r) => {
    if (!seen.has(r.owner_id)) seen.set(r.owner_id, userMap.get(r.owner_id) ?? "Unknown");
  });
  return Array.from(seen.entries()).map(([id, name]) => ({ id, name }));
}

function mapSimilarSuggestions(rows: BeSuggestSimilarResult[] | null | undefined): SimilarScorecardSuggestion[] {
  return (rows ?? []).map((r) => ({
    scorecardId: r.scorecard_id,
    scorecardName: r.name,
    domain: r.domain ?? "General",
    similarity: r.similarity,
    summary: r.purpose_statement ?? "",
  }));
}

/** Standalone "suggest similar scorecard" lookup (used e.g. outside the chat flow).
 * Calls Bedrock (Titan embeddings) via `POST /api/v1/scorecards/suggest-similar` —
 * throws `BedrockUnavailableError` when AWS credentials aren't configured; callers
 * should treat that as "no suggestion". The chat flow itself no longer calls this
 * directly (see `sendChatMessage`/`ChatWorkspace`) — the LangGraph chat graph now runs
 * the same similarity search server-side as its own `check_similarity` node, before
 * `propose_kpis` runs, and returns suggestions inline on the chat-turn response. */
export async function findSimilarScorecards(prompt: string): Promise<SimilarScorecardSuggestion[]> {
  const rows = await apiFetch<BeSuggestSimilarResult[]>("/api/v1/scorecards/suggest-similar", {
    method: "POST",
    body: { query: prompt, top_n: 3 },
  });
  return mapSimilarSuggestions(rows);
}

// ---------------------------------------------------------------------------
// Evaluations
// ---------------------------------------------------------------------------

function mapEvidenceQuotes(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((v) => String(v));
  if (value && typeof value === "object") return Object.values(value as Record<string, unknown>).map((v) => String(v));
  return [];
}

interface EnrichmentContext {
  versionById: Map<string, BeScorecardVersion>;
  scorecardById: Map<string, BeScorecard>;
  userNameById: Map<string, string>;
}

/** Batches the lookups an Evaluation row needs to become a full frontend `Evaluation`
 * (its owning scorecard/version + KPI names/paths/weights + the evaluator's display
 * name) — an evaluation only carries ids for all of these. Dedupes by id and fetches in
 * parallel so a list of evaluations doesn't do one request per field per row. */
async function buildEnrichmentContext(rows: BeEvaluation[]): Promise<EnrichmentContext> {
  const versionIds = Array.from(new Set(rows.map((r) => r.scorecard_version_id)));
  const [versions, userNameById] = await Promise.all([
    Promise.all(versionIds.map((id) => fetchBeScorecardVersion(id))),
    getUserNameMap(),
  ]);
  const versionById = new Map<string, BeScorecardVersion>();
  versions.forEach((v, i) => {
    if (v) versionById.set(versionIds[i], v);
  });

  const scorecardIds = Array.from(new Set(Array.from(versionById.values()).map((v) => v.scorecard_id)));
  const scorecards = await Promise.all(scorecardIds.map((id) => fetchBeScorecard(id)));
  const scorecardById = new Map<string, BeScorecard>();
  scorecards.forEach((s, i) => {
    if (s) scorecardById.set(scorecardIds[i], s);
  });

  return { versionById, scorecardById, userNameById };
}

function mapEvaluation(be: BeEvaluation, ctx: EnrichmentContext): Evaluation {
  const version = ctx.versionById.get(be.scorecard_version_id);
  const scorecard = version ? ctx.scorecardById.get(version.scorecard_id) : undefined;
  const kpiNodeById = new Map((version?.kpi_nodes ?? []).map((n) => [n.id, n]));

  const kpiResults: EvaluationKpiResult[] = (be.kpi_results ?? []).map((r) => {
    const node = kpiNodeById.get(r.kpi_node_id);
    const guideline = node?.guidelines?.find((g) => g.score_level === r.matched_guideline_level);
    return {
      id: r.id,
      evaluationId: r.evaluation_id,
      kpiNodeId: r.kpi_node_id,
      kpiName: node?.name ?? "Unknown KPI",
      kpiPath: node?.path ?? "",
      level: (node?.level ?? 1) as 1 | 2 | 3 | 4,
      weight: node?.weight ?? 0,
      score: r.score,
      matchedGuidelineLevel: r.matched_guideline_level ?? 0,
      matchedGuidelineText: guideline?.qualitative_text ?? "",
      reasoningText: r.reasoning_text ?? "",
      evidenceQuotes: mapEvidenceQuotes(r.evidence_quotes),
    };
  });

  const inputSummary =
    be.input_reference && typeof be.input_reference.summary === "string" ? be.input_reference.summary : "";

  return {
    id: be.id,
    scorecardId: scorecard?.id ?? "",
    scorecardVersionId: be.scorecard_version_id,
    scorecardName: scorecard?.name ?? "Unknown scorecard",
    domain: be.domain ?? scorecard?.domain ?? "General",
    name: be.name,
    evaluatedBy: be.evaluated_by,
    evaluatedByName: ctx.userNameById.get(be.evaluated_by) ?? "Unknown",
    inputSummary,
    status: be.status as EvaluationStatus,
    finalWeightedScore: be.final_weighted_score ?? 0,
    targetScore: scorecard?.target_score ?? 0,
    ragBand: mapRagBand(be.rag_band),
    submittedAt: be.submitted_at ?? be.created_at,
    kpiResults,
  };
}

export async function listEvaluations(filters?: { scorecardId?: string }): Promise<Evaluation[]> {
  let rows: BeEvaluation[];
  if (filters?.scorecardId) {
    // Evaluations reference a scorecard_version_id, not a scorecard_id directly (a
    // scorecard can have several versions, each independently evaluated) — fan out
    // across every version of this scorecard and merge.
    const versions = await apiFetch<BeScorecardVersion[]>(`/api/v1/scorecards/${filters.scorecardId}/versions`);
    const perVersion = await Promise.all(
      versions.map((v) =>
        apiFetch<BeEvaluation[]>(`/api/v1/evaluations?scorecard_version_id=${v.id}&limit=200`),
      ),
    );
    rows = perVersion.flat();
  } else {
    rows = await apiFetch<BeEvaluation[]>("/api/v1/evaluations?limit=200");
  }
  const ctx = await buildEnrichmentContext(rows);
  return rows.map((r) => mapEvaluation(r, ctx));
}

/** Hover-delete on an evaluation row in the Evaluations list (`DELETE
 * /api/v1/evaluations/{id}` — backend/app/api/v1/evaluations.py::delete_evaluation). */
export async function deleteEvaluation(id: string): Promise<void> {
  await apiFetch(`/api/v1/evaluations/${id}`, { method: "DELETE", auth: true });
}

export async function getEvaluation(id: string): Promise<Evaluation | undefined> {
  let be: BeEvaluation;
  try {
    be = await apiFetch<BeEvaluation>(`/api/v1/evaluations/${id}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return undefined;
    throw err;
  }
  const ctx = await buildEnrichmentContext([be]);
  return mapEvaluation(be, ctx);
}

/**
 * Creates a real evaluation row (`POST /api/v1/evaluations`), then runs the Bedrock
 * judge against it (`POST /api/v1/evaluations/{id}/run`) — matching the two real
 * backend calls this one mock function used to fake locally. If the judge run fails
 * because Bedrock is unavailable, the evaluation is NOT thrown away: the backend still
 * persists it (status "failed"), and this function returns that persisted evaluation
 * with `judgeError` set, so the caller can still navigate to a result page that
 * explains what happened (see EvaluationResultView's `notScored` branch) instead of
 * losing the user's input to an uncaught exception.
 */
export async function createEvaluation(input: CreateEvaluationInput): Promise<Evaluation> {
  const scorecard = await fetchBeScorecard(input.scorecardId);
  if (!scorecard || !scorecard.current_version_id) {
    throw new ApiError(`Scorecard ${input.scorecardId} has no active version to evaluate against.`, 422);
  }

  const created = await apiFetch<BeEvaluation>("/api/v1/evaluations", {
    method: "POST",
    auth: true,
    body: {
      scorecard_version_id: scorecard.current_version_id,
      name: input.name,
      evaluated_by: await getDevUserId(),
      input_reference: { summary: input.inputSummary },
      domain: scorecard.domain,
    },
  });

  let finalBe = created;
  let judgeError: string | undefined;
  try {
    finalBe = await apiFetch<BeEvaluation>(`/api/v1/evaluations/${created.id}/run`, {
      method: "POST",
      body: { input_text: input.inputText },
    });
  } catch (err) {
    if (!(err instanceof BedrockUnavailableError)) throw err;
    judgeError = err.message;
    // The run endpoint marks the row FAILED server-side even though the HTTP response
    // itself was an error — re-fetch to pick up that persisted status.
    finalBe = await apiFetch<BeEvaluation>(`/api/v1/evaluations/${created.id}`);
  }

  const ctx = await buildEnrichmentContext([finalBe]);
  const evaluation = mapEvaluation(finalBe, ctx);
  if (judgeError) evaluation.judgeError = judgeError;
  cacheEvaluation(evaluation);
  return evaluation;
}

export interface ManualKpiScore {
  kpiNodeId: string;
  score: number; // 0-10 integer, picked from the anchored guideline levels
  reasoning: string;
}

/**
 * Human-driven scoring path — never touches the AI judge / Bedrock. Uses the plain CRUD
 * routes in backend/app/api/v1/evaluations.py:
 *   1. `POST /evaluations` (status in_progress)
 *   2. `POST /evaluations/{id}/results` once per leaf KPI (`EvaluationKpiResultCreate`:
 *      kpi_node_id, score, matched_guideline_level, reasoning_text, evidence_quotes)
 *   3. `POST /evaluations/{id}/finalize` — computes and persists the final weighted
 *      score + RAG band SERVER-SIDE (via the same `app/ai/judge.py::compute_final_score`
 *      the AI judge path uses, so a scorecard's custom `scoring_formula`, if it has one,
 *      is honored here too — this used to be computed client-side and PATCHed directly,
 *      which could never see a custom formula; `finalWeightedScore`/`ragBand` below are
 *      now only a client-side LIVE PREVIEW via `computeWeightedFinalScore` — the value
 *      this function actually persists always comes back from `/finalize`).
 * If a step after (1) fails, the evaluation is marked "failed" best-effort so no
 * half-scored row sits looking "in progress" forever.
 */
export async function createManualEvaluation(input: {
  scorecardId: string;
  name: string;
  inputSummary: string;
  scores: ManualKpiScore[];
  finalWeightedScore: number;
  ragBand: RagBandKey;
}): Promise<Evaluation> {
  const scorecard = await fetchBeScorecard(input.scorecardId);
  if (!scorecard || !scorecard.current_version_id) {
    throw new ApiError(`Scorecard ${input.scorecardId} has no active version to evaluate against.`, 422);
  }

  const created = await apiFetch<BeEvaluation>("/api/v1/evaluations", {
    method: "POST",
    auth: true,
    body: {
      scorecard_version_id: scorecard.current_version_id,
      name: input.name,
      evaluated_by: await getDevUserId(),
      input_reference: { summary: input.inputSummary, mode: "manual" },
      status: "in_progress",
      domain: scorecard.domain,
    },
  });

  let finalBe: BeEvaluation;
  try {
    for (const s of input.scores) {
      await apiFetch(`/api/v1/evaluations/${created.id}/results`, {
        method: "POST",
        auth: true,
        body: {
          kpi_node_id: s.kpiNodeId,
          score: s.score,
          matched_guideline_level: Math.round(s.score),
          reasoning_text: s.reasoning.trim() || null,
          evidence_quotes: [],
        },
      });
    }
    finalBe = await apiFetch<BeEvaluation>(`/api/v1/evaluations/${created.id}/finalize`, {
      method: "POST",
      auth: true,
    });
  } catch (err) {
    try {
      await apiFetch(`/api/v1/evaluations/${created.id}`, { method: "PATCH", auth: true, body: { status: "failed" } });
    } catch {
      // best-effort only
    }
    throw err;
  }

  const ctx = await buildEnrichmentContext([finalBe]);
  const evaluation = mapEvaluation(finalBe, ctx);
  cacheEvaluation(evaluation);
  return evaluation;
}

/**
 * Browser-only legacy fallback for evaluations that fail to load from the real backend
 * (e.g. transient network issue in the browser tab). Normal operation never needs this
 * any more — real evaluations are persisted server-side and `getEvaluation` finds them
 * directly — but `createEvaluation` still best-effort mirrors the result here so
 * `EvaluationResultClientLoader` keeps working as a fallback.
 */
export function getCachedEvaluation(id: string): Evaluation | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.sessionStorage.getItem(`evaluation-cache:${id}`);
    return raw ? (JSON.parse(raw) as Evaluation) : undefined;
  } catch {
    return undefined;
  }
}

export function cacheEvaluation(evaluation: Evaluation): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(`evaluation-cache:${evaluation.id}`, JSON.stringify(evaluation));
  } catch {
    // sessionStorage can throw in private-browsing contexts; non-fatal.
  }
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

function truncate(s: string, n: number): string {
  const trimmed = s.trim();
  return trimmed.length > n ? `${trimmed.slice(0, n - 1)}…` : trimmed;
}

function mapChatSessionStatus(status: string): ChatSessionStatus {
  return status === "active" || status === "completed" || status === "abandoned" ? status : "active";
}

/**
 * Real, AI-generated title (see `backend/app/models/chat_session.py::ChatSession.title` /
 * `backend/app/ai/session_title.py`) — replaces the old client-side synthesis hack that
 * truncated the raw first message (kept here only as a graceful fallback for the brief
 * window right after session creation, before the title call resolves, or if title
 * generation itself failed; see that module's "never raise" contract).
 */
function titleOrFallback(be: BeChatSession): string {
  if (be.title && be.title.trim()) return be.title;
  if (be.context_summary && be.context_summary.trim()) return truncate(be.context_summary, 60);
  return "New chat";
}

function mapChatSession(be: BeChatSession): ChatSession {
  return {
    id: be.id,
    userId: be.user_id,
    title: titleOrFallback(be),
    status: mapChatSessionStatus(be.status),
    contextSummary: be.context_summary ?? "",
    targetScorecardId: be.target_scorecard_id,
    createdAt: be.created_at,
    lastActivityAt: be.last_activity_at,
  };
}

function mapClarifyingQuestion(id: string, q: BeClarifyingQuestion | null): ClarifyingQuestion | undefined {
  if (!q) return undefined;
  return {
    id,
    question: q.question,
    options: (q.options ?? []).map((label, i) => ({ id: `opt-${i}`, label })),
    allowOther: true,
    missingFields: q.missing_fields ?? [],
  };
}

function mapChatMessages(sessionId: string, rows: BeChatMessage[]): ChatMessage[] {
  return rows.map((m) => {
    const toolCalls = m.tool_calls as { question?: BeClarifyingQuestion } | null;
    return {
      id: m.id,
      sessionId,
      role: m.role as ChatRole,
      content: m.content,
      createdAt: m.created_at,
      clarifyingQuestion: mapClarifyingQuestion(`${m.id}-cq`, toolCalls?.question ?? null),
    };
  });
}

/** Best-effort name-based id: the backend draft references KPIs by name (see
 * `app/ai/draft_schema.py`), not by a stable id, so synthesize one for React keys /
 * parent-child linking in the live preview panel. */
function draftKpiId(name: string): string {
  return `draft-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function mapDraft(sessionId: string, be: BeScorecardDraft | null | undefined): ScorecardDraft {
  // A KPI referenced as some OTHER KPI's `parent_name` is a grouping/CATEGORY node (see
  // backend/app/ai/draft_schema.py's module docstring) — it never carries guidelines of
  // its own (only leaf KPIs are judged/scored — mirrors app/ai/judge.py::leaf_nodes), so
  // an empty `guidelines` object there does NOT mean "still just a rough proposal" the way
  // it would for a leaf KPI. Without this, every category header would be stuck showing
  // the "proposed" sparkle badge forever, even once it (and every KPI under it) is fully
  // specified.
  const categoryNames = new Set((be?.kpis ?? []).map((k) => k.parent_name).filter((p): p is string => !!p));
  const kpis: DraftKpi[] = (be?.kpis ?? []).map((k) => {
    const isCategory = categoryNames.has(k.name);
    const hasGuidelines = Object.keys(k.guidelines ?? {}).length > 0;
    return {
      id: draftKpiId(k.name),
      name: k.name,
      // `null` for a category (no weight of its own — see migration
      // 0008_category_nodes_no_weight) OR a leaf the model hasn't weighted yet; both are
      // genuinely "no value" rather than a real 0%, so it's passed through as-is.
      weight: k.weight,
      level: (k.level ?? 1) as 1 | 2 | 3 | 4,
      parentId: k.parent_name ? draftKpiId(k.parent_name) : null,
      includedInScoring: k.included_in_scoring ?? true,
      status: k.weight != null && (hasGuidelines || isCategory) ? "confirmed" : "proposed",
    };
  });
  return {
    sessionId,
    name: be?.name ?? null,
    domain: be?.domain ?? null,
    purposeStatement: be?.purpose ?? null,
    // draft_schema.py's `audience` has no dedicated `scorecards` column either (folded
    // into `scope` at materialization time — see draft_schema.py's own docstring); shown
    // here as the closest available preview of "scope" while drafting.
    scope: be?.audience ?? null,
    targetScore: be?.target_score ?? null,
    kpis,
    scoringFormula: be?.scoring_formula ?? null,
  };
}

function mapTurnEvent(be: BeChatTurnEvent): ChatTurnEvent {
  return {
    id: be.id,
    actor: be.actor,
    eventType: be.event_type,
    message: be.message,
    round: be.round ?? 1,
    createdAt: be.created_at,
  };
}

/**
 * The live-trace event log for the CURRENT/most recent turn (see
 * `backend/app/api/v1/chat.py::get_chat_turn_events`) — what `TurnTraceCard` renders in
 * place of the old generic "Assistant is thinking…" dots. Returns `[]` (not an error) for
 * a session that exists but has no turn-trace history yet (e.g. never sent a message, or
 * a seeded "Refine with assistant" session whose seed never runs through the API's
 * turn-marking wrapper — see `emit_turn_event`'s `turn_started_at=None` no-op contract).
 */
export async function getChatTurnEvents(sessionId: string): Promise<ChatTurnEvent[]> {
  const rows = await apiFetch<BeChatTurnEvent[]>(`/api/v1/chat/sessions/${sessionId}/turn-events`);
  return rows.map(mapTurnEvent);
}

export async function listChatSessions(): Promise<ChatSession[]> {
  const rows = await apiFetch<BeChatSession[]>("/api/v1/chat/sessions?limit=100");
  return rows.map(mapChatSession);
}

/** Hover-delete on a session row in the sidebar `SessionList` (`DELETE
 * /api/v1/chat/sessions/{id}` — backend/app/api/v1/chat.py::delete_chat_session). Removes
 * the chat_sessions row (cascading to its messages/turn-events) and its LangGraph
 * checkpoint state. */
export async function deleteChatSession(sessionId: string): Promise<void> {
  await apiFetch(`/api/v1/chat/sessions/${sessionId}`, { method: "DELETE", auth: true });
}

/**
 * "Refine with assistant": opens a NEW chat session whose draft is pre-populated from
 * the scorecard's current version (`POST /chat/sessions` with only
 * `target_scorecard_id` — see backend/app/api/v1/chat.py::_start_refine_session). That
 * call never touches Bedrock, so it succeeds even with no AWS credentials; only the
 * user's later messages need the assistant. Confirming in that session saves a new
 * version of this same scorecard. Returns the new session id.
 */
export async function startRefineSession(scorecardId: string): Promise<string> {
  const turn = await apiFetch<BeChatTurn>("/api/v1/chat/sessions", {
    method: "POST",
    auth: true,
    body: { target_scorecard_id: scorecardId },
  });
  return turn.session_id;
}

export async function getChatSession(sessionId: string): Promise<
  | {
      session: ChatSession;
      messages: ChatMessage[];
      draft: ScorecardDraft;
      /** Set once this session has actually saved a scorecard (status completed). */
      savedScorecardId?: string;
      /** Refresh-recovery (Part A): true when a turn is still running server-side for
       * this session — see BeChatTurn.turn_in_progress. */
      turnInProgress: boolean;
      /** Refresh-recovery (Part B, live turn trace): the CURRENT/most recent turn's
       * granular event log — see `getChatTurnEvents`. Fetched here too (not just via
       * polling) so a page load/refresh shows the in-progress trace immediately instead
       * of starting blank. */
      turnEvents: ChatTurnEvent[];
    }
  | undefined
> {
  let messagesBe: BeChatMessage[] | undefined;
  try {
    messagesBe = await apiFetch<BeChatMessage[]>(`/api/v1/chat/sessions/${sessionId}/messages`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return undefined;
    throw err;
  }

  const sessions = await listChatSessions();
  const session = sessions.find((s) => s.id === sessionId);
  if (!session) return undefined;

  const messages = mapChatMessages(sessionId, messagesBe);

  // The LangGraph-facing draft only exists once a turn has actually run through Bedrock
  // (see scorecard_builder.py) — a session created outside that flow (e.g. the seed
  // script's placeholder stub) has no checkpoint yet and this 404s. Fall back to an
  // empty draft rather than failing the whole page.
  let draft: ScorecardDraft;
  let savedScorecardId: string | undefined;
  let turnInProgress = false;
  try {
    const turn = await apiFetch<BeChatTurn>(`/api/v1/chat/sessions/${sessionId}`);
    draft = mapDraft(sessionId, turn.draft);
    savedScorecardId = turn.materialized_scorecard_id ?? undefined;
    turnInProgress = !!turn.turn_in_progress;
  } catch {
    draft = mapDraft(sessionId, null);
  }

  // Best-effort: the trace is a nicety on top of turnInProgress, not load-bearing — a
  // failure here must not fail the whole page load.
  let turnEvents: ChatTurnEvent[] = [];
  try {
    turnEvents = await getChatTurnEvents(sessionId);
  } catch {
    turnEvents = [];
  }

  return { session, messages, draft, savedScorecardId, turnInProgress, turnEvents };
}

/**
 * Lightweight poll target for refresh-recovery (Part A): just the "is a turn still
 * running server-side" flag, without re-fetching the whole message log every few
 * seconds. Once this flips false, callers should re-fetch via `getChatSession` to pick
 * up whatever the turn produced while the page was gone.
 */
export async function getChatTurnStatus(
  sessionId: string,
): Promise<{ turnInProgress: boolean; title: string | null }> {
  const turn = await apiFetch<BeChatTurn>(`/api/v1/chat/sessions/${sessionId}`);
  return { turnInProgress: !!turn.turn_in_progress, title: turn.title ?? null };
}

/** Drafts still in progress (active chat sessions). */
export async function listResumableDrafts(): Promise<ChatSession[]> {
  const sessions = await listChatSessions();
  return sessions.filter((s) => s.status === "active");
}

export interface SendChatMessageResult {
  /** The backend-assigned session id — differs from the request's `sessionId` the first
   * time a "new" session's first turn succeeds (see module docstring / ChatWorkspace). */
  sessionId: string;
  assistantMessage: ChatMessage;
  draft: ScorecardDraft;
  materializedScorecardId?: string;
  /** Reuse-suggestion cards from the backend's own `check_similarity` graph node (see
   * BeChatTurn.similar_suggestions) — present exactly when the graph paused with
   * status "awaiting_similar_choice", i.e. before any KPIs were generated. */
  similarSuggestions?: SimilarScorecardSuggestion[];
  /** The session's real title (see BeChatSession.title) — on a brand-new session's first
   * turn, this is generated fast, BEFORE the heavier graph work, and is already present
   * on this very response (see app/api/v1/chat.py::_generate_and_persist_title). Null for
   * a continuing turn's response too (the backend still echoes the unchanged title). */
  title: string | null;
}

/**
 * Sends one chat turn to the real LangGraph-backed builder.
 *
 * Signature change from the mock version (documented, not preserved as-is): the mock
 * took `{sessionId, turnIndex, currentDraft}` because it was a purely client-scripted
 * fake with no real state. The real backend is authoritative and stateful server-side
 * (a LangGraph checkpoint per session — see `scorecard_builder.py`), so it needs the
 * actual message text, not a turn counter, and returns the authoritative draft itself
 * rather than needing the caller's copy. `sessionId: "new"` triggers session creation
 * (`POST /chat/sessions`); any other value sends a follow-up turn
 * (`POST /chat/sessions/{id}/messages`). Both require the dev-auth header — see
 * `backend/app/deps.py::get_current_user`, applied uniformly to every mutating route.
 */
export async function sendChatMessage(params: {
  sessionId: string;
  message: string;
  /** Sidebar-live-update fix: for `sessionId === "new"` only — a client-generated UUID
   * the caller has already optimistically adopted (URL, sidebar placeholder, turn-events
   * poll — see ChatWorkspace.runAssistantTurn) before this request was even sent. Passed
   * through as `session_id` so the backend uses it as the real row's id (see
   * ChatSessionStart.session_id) instead of minting its own that the caller would have no
   * way to learn about until this call returns. */
  clientSessionId?: string;
}): Promise<SendChatMessageResult> {
  const { sessionId, message, clientSessionId } = params;
  const turn =
    sessionId === "new"
      ? await apiFetch<BeChatTurn>("/api/v1/chat/sessions", {
          method: "POST",
          auth: true,
          body: { message, session_id: clientSessionId },
        })
      : await apiFetch<BeChatTurn>(`/api/v1/chat/sessions/${sessionId}/messages`, {
          method: "POST",
          auth: true, // dev auth stub is now applied uniformly to every mutating route — see app/deps.py
          body: { message },
        });

  const realSessionId = turn.session_id;
  const assistantMessage: ChatMessage = {
    id: `${realSessionId}-a-${Date.now()}`,
    sessionId: realSessionId,
    role: "assistant",
    content: turn.assistant_message ?? turn.question?.question ?? "",
    createdAt: new Date().toISOString(),
    clarifyingQuestion: mapClarifyingQuestion(`${realSessionId}-cq-${Date.now()}`, turn.question),
  };

  return {
    sessionId: realSessionId,
    assistantMessage,
    draft: mapDraft(realSessionId, turn.draft),
    materializedScorecardId: turn.materialized_scorecard_id ?? undefined,
    similarSuggestions:
      turn.similar_suggestions && turn.similar_suggestions.length > 0
        ? mapSimilarSuggestions(turn.similar_suggestions)
        : undefined,
    title: turn.title ?? null,
  };
}
