/**
 * Domain types for the Quality Scorecard System frontend.
 *
 * These mirror the backend data model sketched in the approved plan
 * (`scorecards`, `scorecard_versions`, `kpi_nodes`, `kpi_guidelines`,
 * `evaluations`, `evaluation_kpi_results`, `chat_sessions`, `chat_messages`)
 * closely enough that swapping `lib/api-client.ts` from mock data to real
 * `fetch` calls against the FastAPI backend should not require changing
 * these shapes. Field names are camelCase on the frontend; the API client
 * is the seam where snake_case <-> camelCase translation would happen.
 */

export type ScorecardStatus = "draft" | "published" | "archived";

export type UserRole = "designer" | "evaluator" | "admin";

export interface User {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  orgId?: string | null;
}

/** Top-level scorecard record. Points at the currently active version. */
export interface Scorecard {
  id: string;
  name: string;
  domain: string;
  ownerId: string;
  ownerName: string;
  purposeStatement: string;
  scope: string;
  targetScore: number; // 0-10
  status: ScorecardStatus;
  currentVersionId: string;
  kpiCount: number;
  createdAt: string; // ISO date
  updatedAt: string; // ISO date
}

/** A single immutable version of a scorecard's KPI tree + guidelines. */
export interface ScorecardVersion {
  id: string;
  scorecardId: string;
  versionNumber: number;
  guidelineNotes: string;
  createdBy: string;
  createdAt: string;
  isActive: boolean;
  kpiNodes: KpiNode[]; // flat list; use buildKpiTree() to nest by parentId
  /** Null = the classic weighted-average formula (default, unchanged). Non-null = a
   * custom expression referencing kpi["KPI Name"] scores — see
   * backend/app/ai/scoring_formula.py. */
  scoringFormula: string | null;
}

/** Level 1-4 node in the KPI hierarchy. Materialized as an ltree `path` server-side. */
export interface KpiNode {
  id: string;
  scorecardVersionId: string;
  parentId: string | null;
  path: string; // e.g. "1.2.1" mirroring the ltree path
  level: 1 | 2 | 3 | 4;
  name: string;
  description?: string;
  /**
   * Percentage points. `null` for a category/grouping node (one or more OTHER KpiNodes
   * reference it as their `parentId`) — categories are purely organizational and carry no
   * weight of their own. Only LEAF nodes (no children) have a real weight, and every leaf
   * in the same scorecard version sums to 100 TOGETHER (not per immediate parent group —
   * see backend migration 0008_category_nodes_no_weight / `lib/kpi-tree.ts`).
   */
  weight: number | null;
  displayOrder: number;
  guidelines?: Guideline[]; // only present on leaf (scored) KPIs
  /** When false, this KPI is tracked/scored but excluded from the sibling
   * weight-sum-to-100 rule AND from the default weighted-average formula (still counted
   * by a custom scoring_formula if one references it). Defaults true. */
  includedInScoring: boolean;
}

/** One of the 11 (0-10) qualitative + quantitative guideline levels for a KPI. */
export interface Guideline {
  id: string;
  kpiNodeId: string;
  scoreLevel: number; // 0-10
  qualitativeText: string;
  quantitativeCriteria?: string;
}

export type EvaluationStatus = "pending" | "in_progress" | "completed" | "failed";

export interface Evaluation {
  id: string;
  scorecardId: string;
  scorecardVersionId: string;
  scorecardName: string;
  domain: string;
  name: string;
  evaluatedBy: string;
  evaluatedByName: string;
  inputSummary: string; // short description of the evaluated input/document
  status: EvaluationStatus;
  finalWeightedScore: number; // 0-10
  targetScore: number;
  ragBand: RagBandKey;
  submittedAt: string;
  kpiResults: EvaluationKpiResult[];
  /**
   * Set by the api-client mapping layer when `POST /evaluations/{id}/run` fails with a
   * `BedrockUnavailableError` (502) — the evaluation row itself is still created and
   * persisted (status "failed"), just never scored. Lets result pages show a specific,
   * friendly explanation instead of a table of zeros. Absent for normal evaluations.
   */
  judgeError?: string;
}

export interface EvaluationKpiResult {
  id: string;
  evaluationId: string;
  kpiNodeId: string;
  kpiName: string;
  kpiPath: string;
  level: 1 | 2 | 3 | 4;
  weight: number;
  score: number; // 0-10
  matchedGuidelineLevel: number;
  matchedGuidelineText: string;
  reasoningText: string;
  evidenceQuotes: string[];
}

export type RagBandKey =
  | "excellent"
  | "good"
  | "acceptable"
  | "needs-improvement"
  | "weak"
  | "poor"
  | "critical";

// ---------------------------------------------------------------------------
// Chat / scorecard-builder types
// ---------------------------------------------------------------------------

/**
 * Mirrors the backend's `chat_session_status` enum exactly (`app/models/enums.py::
 * ChatSessionStatus`) — the real API is the source of truth here, so this is a plain
 * pass-through rather than the finer-grained "drafting/clarifying/reviewing" states the
 * original mock UI invented (those had no backend counterpart to drive them).
 */
export type ChatSessionStatus = "active" | "completed" | "abandoned";

export interface ChatSession {
  id: string;
  userId: string;
  title: string;
  status: ChatSessionStatus;
  contextSummary: string;
  targetScorecardId: string | null;
  createdAt: string;
  lastActivityAt: string;
  /** A turn is running server-side right now (backend ChatSessionRead.turn_in_progress). */
  turnInProgress?: boolean;
}

/**
 * One granular step of the AI pipeline's live trace for the CURRENT/most recent turn —
 * mirrors the backend's `ChatTurnEventRead` (see `backend/app/schemas/chat.py` and
 * `GET /chat/sessions/{id}/turn-events`). `actor` is `"master"` (the orchestrator —
 * `research_kpis`'s category-deciding step, `propose_kpis`'s consolidate/propose/confirm
 * steps) or `"research_agent_{n}"` for one of up to `MAX_CATEGORIES` concurrently running
 * research workers, one per decided KPI category (see `backend/app/ai/scorecard_builder.py`).
 * Replaces the old
 * generic "Assistant is thinking…" indicator — `message` is the real, human-readable text
 * rendered directly by `TurnTraceCard`.
 *
 * `round` (default 1 — see migration `0007_chat_turn_event_round`) distinguishes WHICH
 * research round an event belongs to, now that `research_kpis` can run more than one
 * bounded round when the master isn't yet confident coverage is sufficient (see
 * `MAX_RESEARCH_ROUNDS`/`_assess_research_coverage` in `scorecard_builder.py`). `actor`
 * alone is ambiguous across rounds (it resets to `research_agent_1`, ... at the start of
 * every round) — `round` is what `TurnTraceCard` groups by to show each round distinctly.
 */
export interface ChatTurnEvent {
  id: string;
  actor: string;
  eventType: string;
  message: string;
  round: number;
  createdAt: string;
}

export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  sessionId: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  /** Present when the assistant is asking a structured clarifying question. */
  clarifyingQuestion?: ClarifyingQuestion;
  /** Present when the assistant is surfacing a reuse suggestion. */
  similarScorecardSuggestion?: SimilarScorecardSuggestion;
  /** Present on the assistant turn that saved (materialized) the draft as a real scorecard. */
  savedScorecard?: SavedScorecardRef;
}

export interface SavedScorecardRef {
  id: string;
  name: string;
  /** True when the save appended a new version to an existing scorecard (refine flow). */
  asNewVersion: boolean;
}

export interface ClarifyingQuestionOption {
  id: string;
  label: string;
}

/**
 * Chip-based clarifying question. Per the plan, the assistant must never ask
 * an open-ended question as plain prose: it always offers 2-5 short options
 * plus an "Other…" free-text escape hatch.
 */
export interface ClarifyingQuestion {
  id: string;
  question: string;
  options: ClarifyingQuestionOption[];
  allowOther: boolean;
  missingFields: string[];
}

export interface SimilarScorecardSuggestion {
  scorecardId: string;
  scorecardName: string;
  domain: string;
  similarity: number; // 0-1
  summary: string;
}

export type DraftKpiStatus = "proposed" | "confirmed";

export interface DraftKpi {
  id: string;
  name: string;
  /** `null` for a category/grouping node — see `KpiNode.weight`'s own docstring. */
  weight: number | null;
  level: 1 | 2 | 3 | 4;
  parentId: string | null;
  status: DraftKpiStatus;
  /** Mirrors KpiNode.includedInScoring — see that field's docstring. Defaults true. */
  includedInScoring: boolean;
  /** A scored leaf whose 0-10 guidelines are not written yet (live preview: "writing guidelines..."). */
  guidelinesPending?: boolean;
}

/** Live, in-progress scorecard draft shown in the Chat live preview panel. */
export interface ScorecardDraft {
  sessionId: string;
  name: string | null;
  domain: string | null;
  purposeStatement: string | null;
  scope: string | null;
  targetScore: number | null;
  kpis: DraftKpi[];
  /**
   * Custom scoring formula for this in-progress draft (mirrors
   * `ScorecardVersion.scoringFormula` — see `backend/app/ai/draft_schema.py`'s
   * `ScorecardDraft.scoring_formula`). `null` means the default weighted-average
   * behavior. Editable both by the assistant (via its `update_scoring_formula` tool,
   * bound on every chat turn) and directly in the live preview panel (see
   * LivePreviewPanel/ChatWorkspace's `describeDraftEdits`), same as every other field
   * here — local edits are unsent until folded into the next chat message.
   */
  scoringFormula: string | null;
}

// ---------------------------------------------------------------------------
// Filters used by the Charts library
// ---------------------------------------------------------------------------

export interface ScorecardFilters {
  search?: string;
  domain?: string;
  owner?: string;
  status?: ScorecardStatus | "all";
}

export interface CreateEvaluationInput {
  scorecardId: string;
  name: string;
  inputSummary: string;
  inputText: string;
}
