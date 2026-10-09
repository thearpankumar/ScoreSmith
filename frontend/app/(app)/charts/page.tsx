import { Suspense } from "react";

import { ChartsLibraryClient } from "@/components/charts-library/ChartsLibraryClient";
import { TrashLink } from "@/components/trash/TrashLink";
import { PendingInvitations } from "@/components/sharing/PendingInvitations";
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
      <div className="flex flex-wrap items-start justify-between gap-3 md:pr-14">
        <div className="min-w-0">
          {/* mt-1.5: centres the 32px heading line on the 44px Trash button and notification bell beside it */}
          <h1 className="mt-1.5 text-2xl font-semibold text-ink">Charts library</h1>
          <p className="mt-1 text-sm text-ink-muted">Browse, filter, and open a saved scorecard.</p>
        </div>
        <TrashLink className="ml-auto" />
      </div>
      <Suspense fallback={null}>
        <PendingInvitations />
      </Suspense>
      <ChartsLibraryClient scorecards={scorecards} domains={domains} owners={owners} />
    </div>
  );
}
