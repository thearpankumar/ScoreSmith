"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { MailPlus } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { requestLiveRefresh } from "@/components/live/LiveStatusProvider";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { listMyInvitations, type Invitation } from "@/lib/collab-client";
import { timeAgo } from "@/lib/time";
import { InvitePreview } from "./InvitePreview";

/**
 * Charts-page banner: charts other people invited you to. "Preview" opens the read-only widget (also the target of
 * the `?invite=<id>` link in the notification), where you accept or decline.
 */
export function PendingInvitations() {
  const router = useRouter();
  const params = useSearchParams();
  const [items, setItems] = useState<Invitation[]>([]);
  const [openId, setOpenId] = useState<string | null>(params.get("invite"));

  const load = useCallback(async () => {
    try {
      setItems(await listMyInvitations());
    } catch {
      /* the banner is optional */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (items.length === 0 && !openId) return null;
  return (
    <>
      {items.length > 0 && (
        <GlassCard elevation={1} className="flex flex-col gap-2 p-3 sm:p-4" aria-label="Pending invitations">
          <p className="flex items-center gap-2 text-sm font-semibold text-ink">
            <MailPlus className="size-4" aria-hidden /> You have {items.length} pending invitation{items.length === 1 ? "" : "s"}
          </p>
          <ul className="flex flex-col gap-2">
            {items.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-white/60 px-3 py-2">
                <span className="min-w-0 text-sm text-ink">
                  <span className="font-medium">{i.inviter.name}</span> invited you to{" "}
                  <span className="break-words font-medium">{i.scorecardName}</span>
                  <span className="ml-1 text-xs text-ink-muted">{timeAgo(i.createdAt)}</span>
                </span>
                <Button type="button" size="sm" className="min-h-11 sm:min-h-8" onClick={() => setOpenId(i.id)}>
                  Preview
                </Button>
              </li>
            ))}
          </ul>
        </GlassCard>
      )}
      <Dialog open={!!openId} onOpenChange={(o) => !o && setOpenId(null)}>
        <DialogContent className="max-h-[90dvh] w-[calc(100vw-1.5rem)] max-w-xl overflow-y-auto p-4 sm:p-6">
          <DialogHeader className="pr-6">
            <DialogTitle>Chart invitation</DialogTitle>
            <DialogDescription>Look it over, then accept to add it to your charts or decline.</DialogDescription>
          </DialogHeader>
          {openId && (
            <InvitePreview
              invitationId={openId}
              onResolved={() => {
                setOpenId(null);
                void load();
                requestLiveRefresh();
                router.replace("/charts");
                router.refresh();
              }}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
