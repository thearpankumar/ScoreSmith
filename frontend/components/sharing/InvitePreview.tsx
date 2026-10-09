"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Loader2, Target, User, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { acceptInvitation, declineInvitation, previewInvitation, type InvitationPreview } from "@/lib/collab-client";
import { cn } from "@/lib/utils";

/** The flat KPI list as an indented read-only outline (parents first, siblings in display order). */
export function outline(kpis: InvitationPreview["kpis"]): Array<InvitationPreview["kpis"][number] & { depth: number }> {
  const byParent = new Map<string | null, InvitationPreview["kpis"]>();
  for (const k of kpis) {
    const list = byParent.get(k.parentId) ?? [];
    list.push(k);
    byParent.set(k.parentId, list);
  }
  const out: Array<InvitationPreview["kpis"][number] & { depth: number }> = [];
  const walk = (parent: string | null, depth: number) => {
    for (const k of (byParent.get(parent) ?? []).sort((a, b) => a.displayOrder - b.displayOrder)) {
      out.push({ ...k, depth });
      walk(k.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

/**
 * Read-only compact widget of a shared chart, shown BEFORE the receiver accepts: name, owner, target and the KPI tree
 * with weights. No edit controls exist here - the server only serves it to the pending invitee.
 */
export function InvitePreview({
  invitationId,
  onResolved,
  onBack,
}: {
  invitationId: string;
  /** Called after accept / decline succeeded (or the invitation turned out to be resolved already). */
  onResolved: (result: "accepted" | "declined" | "gone") => void;
  onBack?: () => void;
}) {
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"accept" | "decline" | null>(null);

  useEffect(() => {
    let cancelled = false;
    previewInvitation(invitationId)
      .then((p) => !cancelled && setPreview(p))
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.code === "invitation_not_pending")) {
          setError("This invitation is no longer available.");
        } else setError(err instanceof Error ? err.message : "Could not load the preview.");
      });
    return () => {
      cancelled = true;
    };
  }, [invitationId]);

  const rows = useMemo(() => (preview ? outline(preview.kpis) : []), [preview]);

  async function respond(kind: "accept" | "decline") {
    setBusy(kind);
    setError(null);
    try {
      if (kind === "accept") await acceptInvitation(invitationId);
      else await declineInvitation(invitationId);
      onResolved(kind === "accept" ? "accepted" : "declined");
    } catch (err) {
      if (err instanceof ApiError && err.code === "invitation_not_pending") onResolved("gone");
      else setError(err instanceof Error ? err.message : "Something went wrong. Try again.");
    } finally {
      setBusy(null);
    }
  }

  if (!preview) {
    return (
      <div className="flex flex-col gap-3 py-2" aria-busy={!error}>
        {onBack && (
          <Button type="button" variant="ghost" size="sm" className="min-h-11 self-start" onClick={onBack}>
            Back
          </Button>
        )}
        {error ? (
          <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            {error}
          </p>
        ) : (
          <p className="flex items-center gap-2 text-sm text-ink-muted">
            <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading preview…
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-col gap-3">
      {onBack && (
        <Button type="button" variant="ghost" size="sm" className="min-h-11 self-start" onClick={onBack}>
          Back
        </Button>
      )}
      <div>
        <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">Shared with you · read-only preview</p>
        <h3 className="mt-0.5 break-words text-lg font-semibold leading-snug text-ink">{preview.name}</h3>
        <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
          <span className="flex items-center gap-1">
            <User className="size-3" aria-hidden /> {preview.ownerName}
          </span>
          {preview.domain && <span>{preview.domain}</span>}
          {preview.targetScore != null && (
            <span className="flex items-center gap-1">
              <Target className="size-3" aria-hidden /> Target {preview.targetScore.toFixed(1)}
            </span>
          )}
          {preview.versionNumber != null && <span>Version {preview.versionNumber}</span>}
        </p>
        {preview.purposeStatement && <p className="mt-2 text-sm text-ink">{preview.purposeStatement}</p>}
      </div>

      <div className="solid-panel max-h-[40vh] overflow-y-auto rounded-xl p-2" role="tree" aria-label="KPI structure">
        {rows.length === 0 ? (
          <p className="p-2 text-sm text-ink-muted">This chart has no KPIs yet.</p>
        ) : (
          <ul className="flex flex-col">
            {rows.map((k) => (
              <li
                key={k.id}
                role="treeitem"
                aria-level={k.depth + 1}
                aria-selected={false}
                className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-sm"
                style={{ paddingLeft: `${0.5 + k.depth * 1}rem` }}
              >
                <span className={cn("min-w-0 break-words", k.depth === 0 ? "font-semibold text-ink" : "text-ink")}>{k.name}</span>
                {k.weight != null && <span className="shrink-0 text-xs tabular-nums text-ink-muted">{k.weight}%</span>}
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-xs text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button type="button" variant="ghost" className="min-h-11" disabled={busy !== null} onClick={() => respond("decline")}>
          {busy === "decline" ? <Loader2 className="animate-spin" aria-hidden /> : <X aria-hidden />}
          Decline
        </Button>
        <Button type="button" className="min-h-11" disabled={busy !== null} onClick={() => respond("accept")}>
          {busy === "accept" ? <Loader2 className="animate-spin" aria-hidden /> : <Check aria-hidden />}
          Accept and add to my charts
        </Button>
      </div>
    </div>
  );
}
