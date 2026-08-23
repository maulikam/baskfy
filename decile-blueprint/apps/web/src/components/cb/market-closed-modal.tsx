"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * Closed-market state for every order-shaped CTA (05-ui-spec).
 * Notify-me is a stub until SC6/SC9 notification wiring exists.
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
          <Button type="button" variant="secondary" size="sm" disabled title="Arrives with notifications">
            Notify me
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
