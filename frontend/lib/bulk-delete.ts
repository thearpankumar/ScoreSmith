// Bulk delete runner: calls the per-evaluation delete with limited concurrency, reports progress, and collects
// partial failures so the UI can drop the successes and keep the failures selected. Pure (the delete function is
// injected), so node:test can drive it.

export interface BulkDeleteFailure {
  id: string;
  message: string;
}

export interface BulkDeleteResult {
  succeeded: string[];
  failed: BulkDeleteFailure[];
}

export async function runBulkDelete(
  ids: readonly string[],
  deleteOne: (id: string) => Promise<void>,
  opts: { concurrency?: number; onProgress?: (done: number, total: number) => void } = {},
): Promise<BulkDeleteResult> {
  const limit = Math.max(1, Math.min(opts.concurrency ?? 4, ids.length || 1));
  const succeeded: string[] = [];
  const failed: BulkDeleteFailure[] = [];
  let next = 0;
  let done = 0;

  async function worker() {
    while (next < ids.length) {
      const id = ids[next++];
      try {
        await deleteOne(id);
        succeeded.push(id);
      } catch (err) {
        failed.push({ id, message: err instanceof Error && err.message ? err.message : "Couldn't delete this evaluation." });
      }
      done += 1;
      opts.onProgress?.(done, ids.length);
    }
  }

  await Promise.all(Array.from({ length: limit }, worker));
  return { succeeded, failed };
}

/** One-line summary of a partial failure: the count plus the distinct reasons (max 3). */
export function summarizeFailures(failed: readonly BulkDeleteFailure[]): string {
  const reasons = [...new Set(failed.map((f) => f.message))].slice(0, 3).join(" · ");
  return `${failed.length} evaluation${failed.length === 1 ? "" : "s"} couldn't be deleted${reasons ? `: ${reasons}` : "."}`;
}
