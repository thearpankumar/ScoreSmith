"use client";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * Confirm-before-leaving dialog for the live-preview "unsent edits" guard (see
 * `lib/useUnsavedChangesGuard.ts`). Reuses the design system's Dialog primitive rather
 * than a native `window.confirm` — consistent styling/theming, keyboard/focus handling,
 * and a real "Stay" vs "Leave" choice instead of a browser-chrome popup.
 */
export function UnsavedChangesDialog({
  open,
  onConfirmLeave,
  onCancel,
}: {
  open: boolean;
  onConfirmLeave: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Leave without sending your edits?</DialogTitle>
          <DialogDescription>
            You have unsent edits in the live preview. They aren&apos;t saved until the assistant applies them —
            leaving this chat now will discard them.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onCancel}>
            Stay here
          </Button>
          <Button type="button" variant="destructive" onClick={onConfirmLeave}>
            Leave and discard edits
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
