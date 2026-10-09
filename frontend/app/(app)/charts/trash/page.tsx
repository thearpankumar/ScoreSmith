import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { TrashClient } from "@/components/trash/TrashClient";

// Forced dynamic like the rest of the app shell; the list itself is fetched on the client (see TrashClient).
export const dynamic = "force-dynamic";

export const metadata = { title: "Trash" };

export default function ChartsTrashPage() {
  return (
    <div className="flex flex-col gap-4 py-2">
      <div>
        <Link
          href="/charts"
          className="-ml-2 inline-flex min-h-11 items-center gap-1.5 rounded-full px-2 text-sm text-ink-muted transition-colors hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Back to Charts library
        </Link>
        <h1 className="mt-1 text-2xl font-semibold text-ink">Trash</h1>
      </div>
      <TrashClient />
    </div>
  );
}
