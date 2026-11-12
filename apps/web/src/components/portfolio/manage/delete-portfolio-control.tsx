"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { canConfirmDelete, deleteDialogCopy, type DeleteTarget } from "@/lib/portfolio/delete-portfolio";
import { ApiError } from "@/lib/api/errors";
import { useDeletePortfolio } from "@/lib/portfolios/queries";

/** The API's refusal for a portfolio that still records holdings (has-holdings, 409). */
function refusedForHoldings(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 409) return false;
  const reason = (error.problem as { reason?: unknown } | null)?.reason;
  return reason === "has-holdings" || /holding/i.test(error.message);
}

/** "Delete portfolio" on the detail page: confirm, delete Baskfy's record, go to the overview. */
export function DeletePortfolioControl({ target }: { target: DeleteTarget }) {
  const router = useRouter();
  const remove = useDeletePortfolio();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  // The page can list fewer holdings than the API counts (rows with no quantity, a broker pile).
  // When the API says holdings remain, ask for the typed name and resend with the confirm.
  const [escalated, setEscalated] = useState(false);
  const effective: DeleteTarget = escalated
    ? { ...target, holdingsCount: Math.max(target.holdingsCount, 1) }
    : target;
  const copy = deleteDialogCopy(effective);
  const ready = canConfirmDelete(effective, typed) && !remove.isPending;

  function close(next: boolean) {
    setOpen(next);
    if (!next) {
      setTyped("");
      setEscalated(false);
      remove.reset();
    }
  }

  async function confirm() {
    if (!ready) return;
    try {
      await remove.mutateAsync({ id: target.id, confirmHoldings: copy.needsTypedName });
    } catch (error) {
      if (!copy.needsTypedName && refusedForHoldings(error)) setEscalated(true);
      return; // remove.error renders the reason below
    }
    setOpen(false);
    router.push("/portfolio/overview");
    router.refresh();
  }

  return (
    <section
      aria-label="Delete this portfolio"
      data-testid="delete-portfolio-section"
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-negative/30 bg-card px-4 py-3"
    >
      <p className="max-w-[70ch] text-xs text-muted-foreground">
        Deleting removes this portfolio from Baskfy only. Nothing is sold and no order is placed.
      </p>
      <Button
        type="button"
        variant="destructive"
        size="sm"
        data-testid="delete-portfolio-open"
        onClick={() => setOpen(true)}
      >
        <Trash2 aria-hidden="true" />
        Delete portfolio
      </Button>

      <Dialog open={open} onOpenChange={close}>
        <DialogContent className="max-w-md" data-testid="delete-portfolio-dialog">
          <DialogTitle className="text-base font-semibold">{copy.title}</DialogTitle>
          <DialogDescription asChild>
            <div className="mt-2 space-y-2 text-sm text-muted-foreground">
              <p data-testid="delete-portfolio-holdings">{copy.holdingsLine}</p>
              <p>{copy.brokerLine}</p>
              <p className="font-medium text-foreground" data-testid="delete-portfolio-undo">
                {copy.undoLine}
              </p>
            </div>
          </DialogDescription>

          {copy.needsTypedName ? (
            <label className="mt-4 block text-xs font-medium">
              Type “{target.name}” to confirm
              <Input
                className="mt-1"
                value={typed}
                autoComplete="off"
                data-testid="delete-portfolio-typed"
                onChange={(event) => setTyped(event.target.value)}
              />
            </label>
          ) : null}

          {remove.error ? (
            <p role="alert" className="mt-3 text-sm text-negative" data-testid="delete-portfolio-error">
              {escalated && refusedForHoldings(remove.error)
                ? "Baskfy still records holdings for this portfolio that this page does not list. Type its name to delete them with it."
                : remove.error.message}
            </p>
          ) : null}

          <div className="mt-4 flex gap-2">
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={!ready}
              data-testid="delete-portfolio-confirm"
              onClick={() => void confirm()}
            >
              {remove.isPending ? "Deleting…" : "Delete portfolio"}
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => close(false)}>
              Keep it
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}
