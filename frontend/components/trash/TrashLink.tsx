"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Trash2 } from "lucide-react";

import { listTrash, TRASH_CHANGED_EVENT } from "@/lib/trash-client";
import { cn } from "@/lib/utils";

/** "Trash" button at the top right of the Charts page, with a count badge while the trash is not empty. The count is
 *  fetched on the client (a failure just hides the badge) and refreshed when a chart is moved to the trash. */
export function TrashLink({ className }: { className?: string }) {
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    const refresh = () => {
      listTrash()
        .then((rows) => live && setCount(rows.length))
        .catch(() => live && setCount(null));
    };
    refresh();
    window.addEventListener(TRASH_CHANGED_EVENT, refresh);
    return () => {
      live = false;
      window.removeEventListener(TRASH_CHANGED_EVENT, refresh);
    };
  }, []);

  return (
    <Link
      href="/charts/trash"
      aria-label={count ? `Trash, ${count} ${count === 1 ? "chart" : "charts"}` : "Trash"}
      className={cn(
        "inline-flex min-h-11 items-center justify-center gap-2 rounded-full border border-hairline bg-solid px-4 text-sm font-medium text-ink transition-colors hover:bg-bg",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]",
        className,
      )}
    >
      <Trash2 className="size-4" aria-hidden />
      Trash
      {!!count && (
        <span className="min-w-5 rounded-full bg-lemon px-1.5 text-center text-xs font-semibold tabular-nums text-lemon-ink" aria-hidden>
          {count}
        </span>
      )}
    </Link>
  );
}
