import { authApiFetch } from "./api-client";

/**
 * Client for the chart trash (`/api/v1/scorecards/trash*`, backend/app/api/v1/trash.py). Same plumbing as
 * collab-client.ts: `authApiFetch` (cookies, CSRF, single-flight refresh), snake_case on the wire, camelCase here.
 * "Delete" on a chart card itself is `deleteScorecard` in api-client.ts (owner: trash, collaborator: leave).
 */

export interface TrashedChart {
  id: string;
  name: string;
  domain: string | null;
  deletedAt: string;
  daysLeft: number;
  collaboratorCount: number;
  evaluationCount: number;
}

interface BeTrashed {
  id: string;
  name: string;
  domain: string | null;
  deleted_at: string;
  days_left: number;
  collaborator_count: number;
  evaluation_count: number;
}

export interface TrashResult {
  done: string[];
  notFound: string[];
}

/** Fired on `window` whenever the trash content changes so the Trash button on the Charts page can refresh its badge. */
export const TRASH_CHANGED_EVENT = "qs:trash-changed";

const BASE = "/api/v1/scorecards/trash";

export async function listTrash(signal?: AbortSignal): Promise<TrashedChart[]> {
  const rows = await authApiFetch<BeTrashed[]>(BASE, { signal });
  return rows.map((r) => ({
    id: r.id,
    name: r.name,
    domain: r.domain,
    deletedAt: r.deleted_at,
    daysLeft: r.days_left,
    collaboratorCount: r.collaborator_count,
    evaluationCount: r.evaluation_count,
  }));
}

function mapResult(r: { done: string[]; not_found: string[] }): TrashResult {
  return { done: r.done, notFound: r.not_found };
}

export async function restoreFromTrash(ids: string[]): Promise<TrashResult> {
  return mapResult(await authApiFetch(`${BASE}/restore`, { method: "POST", body: { ids } }));
}

export async function purgeFromTrash(ids: string[]): Promise<TrashResult> {
  return mapResult(await authApiFetch(`${BASE}/purge`, { method: "POST", body: { ids } }));
}

export async function emptyTrash(): Promise<TrashResult> {
  return mapResult(await authApiFetch(`${BASE}/empty`, { method: "POST" }));
}
