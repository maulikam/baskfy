import { EMPTY_CELL, formatNumber } from "@/lib/format";
import type { BookTotals } from "@/lib/portfolios/book";

function rupees(value: string | null): string {
  if (value === null || value === "") return EMPTY_CELL;
  return `₹${formatNumber(value, { decimals: 0 })}`;
}

/**
 * The roll-up across every portfolio. Basket marks and allocation capital are shown as two
 * figures because
 * they are two ledgers — adding them is not a combined NAV.
 */
export function BookOverall({ totals }: { totals: BookTotals }) {
  const boxLabel =
    totals.boxCount === 0
      ? "No portfolios yet"
      : `${totals.boxCount} portfolio${totals.boxCount === 1 ? "" : "s"}`;

  return (
    <section
      aria-label="Overall totals"
      data-testid="book-overall"
      className="rounded-xl border border-border/70 bg-card p-4 sm:p-5"
    >
      <p className="eyebrow">Overall</p>
      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        <div>
          <p className="text-xs text-muted-foreground">Current value in baskets you hold</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">
            {rupees(totals.basketValue)}
          </p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Capital assigned across allocations</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums tracking-tight">
            {rupees(totals.sleeveCapital)}
          </p>
        </div>
      </div>
      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
        {boxLabel}. Basket marks and allocation capital are tracked separately — adding them is
        not a combined figure.
        {totals.hasLiveMarks
          ? " Returns on a portfolio are price returns, not total returns including dividends."
          : ""}
      </p>
    </section>
  );
}
