"use client";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { EMPTY_CELL, formatNumber, formatPercent } from "@/lib/format";
import type { InvestorSnapshot } from "@/lib/investments/fetch";

/**
 * Show-Details — docs/smallcase/05 §6.9 / 04 §4 investor math labels (SC4).
 * Labels mirror `InvestorSnapshot` / curated_accounting field names.
 */

function rupees(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 2 })}`;
}

export interface ShowDetailsModalProps {
  basketName: string;
  snapshot: InvestorSnapshot | null;
  triggerLabel?: string;
}

export function ShowDetailsModal({
  basketName,
  snapshot,
  triggerLabel = "Show details",
}: ShowDetailsModalProps) {
  const rows: Array<{ label: string; term: string; value: string; hint?: string }> = [
    {
      label: "Current investment",
      term: "current_investment",
      value: rupees(snapshot?.current_investment),
      hint: "Money put in minus the cost basis of exited quantity",
    },
    {
      label: "Money put in",
      term: "money_put_in",
      value: rupees(snapshot?.money_put_in),
      hint: "Sum of buy-side cash (invest / invest-more / SIP)",
    },
    {
      label: "Current value",
      term: "current_value",
      value: rupees(snapshot?.current_value),
    },
    {
      label: "Current returns",
      term: "current_returns",
      value:
        snapshot == null
          ? EMPTY_CELL
          : `${rupees(snapshot.current_returns)} (${formatPercent(snapshot.current_returns_pct)})`,
    },
    {
      label: "Realized returns",
      term: "realized_pnl",
      value: rupees(snapshot?.realized_pnl),
    },
    {
      label: "Dividends",
      term: "dividends",
      value: rupees(snapshot?.dividends),
    },
    {
      label: "XIRR",
      term: "xirr",
      value:
        snapshot == null
          ? EMPTY_CELL
          : snapshot.xirr_displayable && snapshot.xirr != null
            ? formatPercent(snapshot.xirr, 2)
            : "Hidden until the first buy is more than a year old",
      hint: "Shown only when first investment is >365 days old (04 §4)",
    },
  ];

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          {triggerLabel}
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogTitle>Investment details</DialogTitle>
        <DialogDescription className="text-sm text-muted-foreground">
          Accounting for {basketName}. Labels match the SC4 investor ledger — figures are
          read-only.
        </DialogDescription>
        <dl className="mt-4 space-y-3">
          {rows.map((row) => (
            <div key={row.term} className="flex items-baseline justify-between gap-4">
              <dt className="text-sm text-muted-foreground">
                <span className="text-foreground">{row.label}</span>
                {row.hint ? (
                  <span className="mt-0.5 block text-xs leading-snug">{row.hint}</span>
                ) : null}
              </dt>
              <dd className="shrink-0 text-sm font-medium tabular-nums">{row.value}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  );
}
