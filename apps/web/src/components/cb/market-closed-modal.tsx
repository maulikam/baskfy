"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * Closed-market dialog for surfaces that still open one.
 * AFH 5.3: the disabled notification stub is gone — there is no centre to deliver it.
 */

export interface MarketClosedModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  reopenAt?: string | null;
}

export function MarketClosedModal({ open, onOpenChange, reopenAt }: MarketClosedModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>Market is closed</DialogTitle>
        <DialogDescription className="text-sm leading-relaxed text-muted-foreground">
          Orders can only be confirmed while the exchange session is open
          {reopenAt ? ` — next open around ${reopenAt}` : ""}. You can still build a plan; the
          desk will refuse execution until the session is live.
        </DialogDescription>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
