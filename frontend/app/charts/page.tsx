import { ChartsLibraryClient } from "@/components/charts-library/ChartsLibraryClient";
import { listScorecardDomains, listScorecardOwners, listScorecards } from "@/lib/api-client";

// Forced dynamic: live backend fetch on every request (see lib/api-client.ts apiFetch docstring).
export const dynamic = "force-dynamic";

export default async function ChartsLibraryPage() {
  const [scorecards, domains, owners] = await Promise.all([
    listScorecards(),
    listScorecardDomains(),
    listScorecardOwners(),
  ]);

  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <h1 className="text-2xl font-semibold text-ink">Charts library</h1>
        <p className="mt-1 text-sm text-ink-muted">Browse, filter, and open a saved scorecard.</p>
      </div>
      <ChartsLibraryClient scorecards={scorecards} domains={domains} owners={owners} />
    </div>
  );
}
