"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Guards in-app (client-side) navigation away from unsaved state.
 *
 * `beforeunload` only fires for a full page reload/tab close — Next.js App Router
 * client-side navigation (clicking a `<Link>`, `router.push`, etc.) never triggers it, so
 * it silently discards unsaved state with no warning. As of Next.js 15.3+ (this project is
 * on 15.5), `next/link`'s `<Link>` component supports an `onNavigate` prop specifically for
 * this: it fires synchronously on click, before the client-side transition starts, and
 * calling `event.preventDefault()` inside it cancels the navigation — no manual
 * click-interception/history hacking needed. See:
 * https://nextjs.org/docs/app/api-reference/components/link#onnavigate
 *
 * Usage: pass `shouldWarn` (e.g. a `dirty` flag), spread `linkProps(href)` onto every
 * `<Link>` this guard should cover (in place of a plain `href`), and render
 * `<UnsavedChangesDialog {...dialogProps} .../>` once, anywhere in the tree.
 */
export function useUnsavedChangesGuard(shouldWarn: boolean) {
  const router = useRouter();
  const [pendingHref, setPendingHref] = useState<string | null>(null);

  const linkProps = useCallback(
    (href: string) => ({
      href,
      onNavigate: (event: { preventDefault: () => void }) => {
        if (!shouldWarn) return;
        event.preventDefault();
        setPendingHref(href);
      },
    }),
    [shouldWarn],
  );

  const confirmLeave = useCallback(() => {
    if (pendingHref) router.push(pendingHref);
    setPendingHref(null);
  }, [router, pendingHref]);

  const cancelLeave = useCallback(() => setPendingHref(null), []);

  return {
    linkProps,
    isConfirmOpen: pendingHref !== null,
    confirmLeave,
    cancelLeave,
  };
}
