/** Pure helpers for editing a scorecard's target score (no React, unit-tested). */

export const TARGET_MIN = 0.1;
export const TARGET_MAX = 10;

export type TargetParse = { ok: true; value: number } | { ok: false; error: string };

/**
 * Validates the text typed into the target field. The backend column is Numeric(4,2) with
 * a 0..10 CHECK; a target of 0 would mean "no target" (colours fall back to the default),
 * so the editor demands 0.1-10 and keeps at most two decimals.
 */
export function parseTargetInput(raw: string): TargetParse {
  const text = raw.trim().replace(",", ".");
  if (text === "") return { ok: false, error: `Enter a target between ${TARGET_MIN} and ${TARGET_MAX}.` };
  if (!/^\d+(\.\d+)?$/.test(text)) return { ok: false, error: "Enter a number, for example 7 or 6.5." };
  const n = Number(text);
  if (!Number.isFinite(n) || n < TARGET_MIN || n > TARGET_MAX) {
    return { ok: false, error: `The target must be between ${TARGET_MIN} and ${TARGET_MAX}.` };
  }
  return { ok: true, value: Math.round(n * 100) / 100 };
}

/**
 * Who may change the target. Unlike the KPI structure (frozen once published), the target
 * is a scalar header field that only changes how results are coloured, so it stays
 * editable on draft AND published scorecards. Archived scorecards are read-only, and only
 * the owner may edit (the dev auth stub has no RBAC, so this is a UX rule).
 */
export function targetEditPermission(
  scorecard: { status: string; ownerId: string; ownerName: string; myRole?: string },
  currentUserId: string | null,
): { canEdit: boolean; lockedReason?: string } {
  // The owner and accepted collaborators ("editor") may change the target; anyone else cannot even open the chart.
  if (currentUserId && scorecard.ownerId !== currentUserId && scorecard.myRole !== "editor") {
    return { canEdit: false, lockedReason: `Only the owner, ${scorecard.ownerName}, can change the target.` };
  }
  if (scorecard.status === "archived") {
    return { canEdit: false, lockedReason: "Archived scorecards are read-only. Restore it to draft to change the target." };
  }
  return { canEdit: true };
}
