import type { ChatTurnEvent, ScorecardDraft } from "./types";

/**
 * Pure helpers behind the chat workspace's live (polled) state — kept free of React and of any
 * network code so the merge/dedup rules can be unit-tested with plain node
 * (`npm run test:unit`, see tests/chat-turn-state.test.mjs).
 */

/** User-facing text for a failed/cancelled background turn (backend `turn_error_code`). The user's
 * own message stays in the transcript either way, and unsent live-preview edits are kept. */
export function turnFailureMessage(code: string): string {
  switch (code) {
    case "bedrock_unavailable":
      return "The AI assistant is temporarily unavailable, so nothing was changed or saved. Your draft and any unsent edits are kept here — try again in a few minutes.";
    case "interrupted":
      return "The server restarted while the assistant was working on your message, so nothing was changed. Your draft and any unsent edits are kept here — please try again.";
    case "timeout":
      return "The assistant took too long on that request and was stopped, so nothing was changed. Try a smaller request (for example one category at a time).";
    case "cancelled":
      return "You stopped that request, so nothing was changed. Your draft and any unsent edits are kept here — send a new message or try again.";
    default:
      return "Something went wrong reaching the assistant. Nothing was saved — please try again.";
  }
}

/** Stable string identity of a draft's content (ignores the session id) — used to skip no-op
 * re-renders when a poll returns the same interim draft as the last one. */
export function draftSignature(d: ScorecardDraft): string {
  return JSON.stringify([d.name, d.domain, d.purposeStatement, d.scope, d.targetScore, d.kpis, d.scoringFormula]);
}

/** True when two trace snapshots are identical (same events, same order) — polls usually return
 * the same list, and re-rendering the trace card for that is wasted work. */
export function sameEvents(a: ChatTurnEvent[], b: ChatTurnEvent[]): boolean {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].id !== b[i].id || a[i].message !== b[i].message || a[i].eventType !== b[i].eventType) return false;
  }
  return true;
}

const HEADER_KEYS = ["name", "domain", "purposeStatement", "scope", "targetScore"] as const;

/**
 * What the Live preview shows WHILE a turn runs: the backend's interim draft layered over the
 * user's local draft, without ever clobbering the user's own unsent edits.
 *
 *  - `local` is what the user sees/edits, `server` the last authoritative draft it was derived
 *    from (they differ exactly where the user has unsent edits), `interim` the polled draft.
 *  - A field the user edited (local !== server) keeps the user's value; every other field takes
 *    the interim value when it has one (a missing/empty interim value never blanks a field).
 *  - KPIs are all-or-nothing: if the user touched the KPI list it stays theirs, otherwise a
 *    non-empty interim list replaces it.
 * Returns `local` itself (same reference) when there is nothing to layer, so memoized children
 * do not re-render.
 */
export function mergeInterimDraft(
  local: ScorecardDraft,
  server: ScorecardDraft,
  interim: ScorecardDraft | null,
): ScorecardDraft {
  if (!interim) return local;
  const merged: ScorecardDraft = { ...local };
  const mutable = merged as unknown as Record<string, unknown>;
  for (const key of HEADER_KEYS) {
    const userEdited = local[key] !== server[key];
    const incoming = interim[key];
    if (!userEdited && incoming !== null && incoming !== undefined && incoming !== "") mutable[key] = incoming;
  }
  const kpisEdited = JSON.stringify(local.kpis) !== JSON.stringify(server.kpis);
  if (!kpisEdited && interim.kpis.length > 0) merged.kpis = interim.kpis;
  const formulaEdited = JSON.stringify(local.scoringFormula ?? null) !== JSON.stringify(server.scoringFormula ?? null);
  if (!formulaEdited && interim.scoringFormula) merged.scoringFormula = interim.scoringFormula;
  return draftSignature(merged) === draftSignature(local) ? local : merged;
}

/** How many real KPIs and how many categories a draft holds. A node that other nodes hang under is a
 * category (a grouping header with no weight or guidelines); only leaves are KPIs, i.e. what gets weighted
 * and scored. Counting every node as a "KPI" over-reports a categorised scorecard (114 nodes = 97 KPIs +
 * 17 categories). */
export function draftKpiCounts(kpis: ScorecardDraft["kpis"]): { kpis: number; categories: number } {
  const parents = new Set(kpis.map((k) => k.parentId).filter((p): p is string => p !== null));
  const categories = kpis.filter((k) => parents.has(k.id)).length;
  return { kpis: kpis.length - categories, categories };
}

/** Number of KPIs (leaf rows) that already have a weight vs the total — drives the "weighting…"
 * hint in the preview while the backend is still filling weights in. */
export function weightedLeafProgress(kpis: ScorecardDraft["kpis"]): { weighted: number; total: number } {
  const parents = new Set(kpis.map((k) => k.parentId).filter((p): p is string => p !== null));
  const leaves = kpis.filter((k) => !parents.has(k.id) && k.includedInScoring);
  return { weighted: leaves.filter((k) => k.weight !== null).length, total: leaves.length };
}

/** The chat workspace appends a "[Edits I made directly in the live preview ...]" block to the
 * user's message so the assistant can apply inline edits (see ChatWorkspace.describeDraftEdits).
 * The persisted message keeps it; the transcript shows only what the user actually typed. */
export function stripEditSummary(content: string): string {
  const i = content.indexOf("\n\n[Edits I made directly in the live preview");
  return i === -1 ? content : content.slice(0, i);
}

/**
 * Plain-language summary of how the locally-edited draft differs from the last server draft, or null
 * when there are no edits. Appended to the next chat message (as "[Edits I made directly in the live
 * preview ...]") so the assistant applies it with its own tools.
 *
 * Wording matters: the backend only lets the assistant change/remove/rename/reweight a KPI the USER
 * originally pinned (typed in their own list) when the user's latest message NAMES that KPI and
 * contains a change-intent word (`_verified_user_changes` in scorecard_builder.py). So every KPI edit
 * is one explicit line using the KPI's ORIGINAL name plus an imperative verb the server recognizes
 * (change / rename / remove / exclude / move / add) — never a bare "the list should be exactly ...".
 * Only KPIs the user actually touched are named, so an unrelated pinned KPI is never "verified".
 */
export function describeDraftEdits(server: ScorecardDraft, local: ScorecardDraft): string | null {
  const changes: string[] = [];
  const fmt = (v: string | number | null) => (v === null || v === "" ? "(cleared)" : `"${v}"`);
  if ((server.name ?? "") !== (local.name ?? "")) changes.push(`- Scorecard name: ${fmt(local.name)}`);
  if ((server.purposeStatement ?? "") !== (local.purposeStatement ?? ""))
    changes.push(`- Purpose: ${fmt(local.purposeStatement)}`);
  if ((server.scope ?? "") !== (local.scope ?? "")) changes.push(`- Scope / audience: ${fmt(local.scope)}`);
  if (server.targetScore !== local.targetScore) changes.push(`- Target score: ${fmt(local.targetScore)}`);

  const serverById = new Map(server.kpis.map((k) => [k.id, k]));
  const localById = new Map(local.kpis.map((k) => [k.id, k]));
  const nameOf = (byId: Map<string, ScorecardDraft["kpis"][number]>, id: string | null) =>
    id ? (byId.get(id)?.name ?? null) : null;
  // Only LEAF KPIs carry a weight — a category has none of its own (backend migration 0008).
  const serverParents = new Set(server.kpis.map((k) => k.parentId).filter((id): id is string => id !== null));
  const localParents = new Set(local.kpis.map((k) => k.parentId).filter((id): id is string => id !== null));

  const removed = server.kpis.filter((k) => !localById.has(k.id));
  if (removed.length > 0) changes.push(`- Remove KPIs: ${removed.map((k) => `"${k.name}"`).join(", ")}`);

  let weightsTouched = false;
  for (const k of local.kpis) {
    const before = serverById.get(k.id);
    if (!before) {
      const parent = nameOf(localById, k.parentId);
      changes.push(
        `- Add a new KPI "${k.name}"${parent ? ` under "${parent}"` : ""}` +
          (localParents.has(k.id) ? " (category)" : ` with weight ${k.weight ?? 0}%`) +
          (k.includedInScoring === false ? " [excluded from the weighted score]" : ""),
      );
      weightsTouched = weightsTouched || !localParents.has(k.id);
      continue;
    }
    if (before.name !== k.name) changes.push(`- Rename the KPI "${before.name}" to "${k.name}"`);
    if (before.parentId !== k.parentId) {
      changes.push(
        `- Move the KPI "${before.name}" from ${nameOf(serverById, before.parentId) ? `"${nameOf(serverById, before.parentId)}"` : "the top level"} to ${nameOf(localById, k.parentId) ? `"${nameOf(localById, k.parentId)}"` : "the top level"}`,
      );
    }
    if (!localParents.has(k.id) && !serverParents.has(k.id) && (before.weight ?? 0) !== (k.weight ?? 0)) {
      changes.push(`- Change the weight of "${before.name}" from ${before.weight ?? 0}% to ${k.weight ?? 0}%`);
      weightsTouched = true;
    }
    if (before.includedInScoring !== k.includedInScoring) {
      changes.push(
        k.includedInScoring
          ? `- Change "${before.name}": count it in the weighted score again (set included_in_scoring to true)`
          : `- Exclude "${before.name}" from the weighted score (set included_in_scoring to false)`,
      );
      weightsTouched = true;
    }
  }
  if (changes.some((c) => c.startsWith("- Remove KPIs"))) weightsTouched = true;
  if (weightsTouched) {
    const total = local.kpis
      .filter((k) => !localParents.has(k.id) && k.includedInScoring)
      .reduce((sum, k) => sum + (k.weight ?? 0), 0);
    changes.push(
      `- (The scored KPI weights in my preview currently total ${Math.round(total * 100) / 100}%; keep every other KPI as it is and scale only as needed so the scored total stays 100%.)`,
    );
  }

  // A scoring-formula edit made in the preview is applied through the assistant's own
  // `update_scoring_formula` tool, never a direct draft field write.
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

/** The backend appends the closing question to the assistant's reply text AND returns it as a
 * structured question (rendered as the "Quick question" card with option chips). Drop the duplicate
 * trailing copy from the bubble so the question is not shown twice; the reply is kept whole when it
 * does not end with the question (or would become empty). */
export function stripTrailingQuestion(content: string, question: string): string {
  const q = question.trim();
  const c = content.trimEnd();
  if (!q) return content;
  if (c.endsWith(q)) {
    const rest = c.slice(0, c.length - q.length).trimEnd();
    return rest.length > 0 ? rest : content;
  }
  // The reply's closing question can be worded differently from the structured one ("...as-is, or adjust
  // anything (weights, ...)?" vs "...as-is, or adjust something first?"): drop a final single-line
  // paragraph that is a question, provided real content remains above it.
  const cut = c.lastIndexOf("\n\n");
  if (cut > 0) {
    const last = c.slice(cut).trim();
    if (last.endsWith("?") && !last.includes("\n")) return c.slice(0, cut).trimEnd();
  }
  return content;
}

/** "Try again" re-posts the user's last message, and the backend persists every POST, so a retried
 * message appears twice in a row (the failed attempt got no reply). Show it once: drop a user message
 * that is immediately followed by another user message with identical content. */
export function dedupeRetriedMessages<T extends { role: string; content: string }>(messages: T[]): T[] {
  return messages.filter((m, i) => {
    const next = messages[i + 1];
    return !(m.role === "user" && next && next.role === "user" && next.content === m.content);
  });
}
