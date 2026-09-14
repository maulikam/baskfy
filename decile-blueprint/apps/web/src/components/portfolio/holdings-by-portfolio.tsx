import { EMPTY_CELL, formatNumber } from "@/lib/format";
import type { AggregatedHolding } from "@/lib/portfolio/organize";
import { formatMoney, formatMoneyMove, formatRate, toneFor } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * `/portfolio/holdings` — "By portfolio", one table per portfolio.
 *
 * It was a list of symbol, quantity and value (14 Sep 2026: "this needs more stats columns").
 * Every figure here comes from the API's holding row, so the table cannot compute a P&L the
 * portfolio page would disagree with. Weight is the one derived number, and it is a share of this
 * table's own value total, which is visible in the footer.
 *
 * A figure the API could not compute renders the em dash with its reason as a tooltip, never a
 * zero: a holding with no purchase price has no P&L, not a P&L of nothing.
 */

type Move = AggregatedHolding["todays_pnl"];

function sum(values: readonly (string | null | undefined)[]): number | null {
  let total = 0;
  for (const value of values) {
    if (value === null || value === undefined || value === "") return null;
    total += Number(value);
  }
  return total;
}

function MoveCell({ move, testId }: { move: Move; testId: string }) {
  const amount = move?.amount ?? null;
  return (
    <td
      className="px-3 py-2 text-right tabular-nums"
      data-testid={testId}
      title={amount === null ? (move?.unavailable_reason ?? move?.label) : move?.label}
    >
      <span className={cn("block", toneFor(amount))}>{formatMoneyMove(amount)}</span>
      <span className={cn("block text-[11px]", toneFor(move?.pct))}>
        {amount === null ? "" : formatRate(move?.pct ?? null)}
      </span>
    </td>
  );
}

function TotalMoveCell({ amount, base }: { amount: number | null; base: number | null }) {
  const pct = amount === null || base === null || base === 0 ? null : amount / base;
  return (
    <td className="px-3 py-2 text-right font-medium tabular-nums">
      <span className={cn("block", toneFor(amount))}>{formatMoneyMove(amount)}</span>
      <span className={cn("block text-[11px]", toneFor(pct))}>{formatRate(pct)}</span>
    </td>
  );
}

const HEADERS = [
  "Stock",
  "Qty",
  "Avg price",
  "Price",
  "Invested",
  "Value",
  "Weight",
  "P&L",
  "Today",
  "This week",
] as const;

export function HoldingsByPortfolioTable({
  name,
  rows,
}: {
  name: string;
  rows: readonly AggregatedHolding[];
}) {
  const ordered = [...rows].sort((a, b) => Number(b.value ?? -1) - Number(a.value ?? -1));
  const totalValue = sum(ordered.map((row) => row.value));
  const totalInvested = sum(ordered.map((row) => row.invested));
  const totalPnl = sum(ordered.map((row) => row.total_pnl?.amount));
  const today = sum(ordered.map((row) => row.todays_pnl?.amount));
  const week = sum(ordered.map((row) => row.week_pnl?.amount));

  return (
    <div className="rounded-xl border border-border bg-card" data-testid="holdings-portfolio-table">
      <h3 className="px-4 pt-3 text-sm font-medium">{name}</h3>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[60rem] text-sm">
          <caption className="sr-only">Holdings in {name}, with cost, value and moves.</caption>
          <thead>
            <tr className="border-b border-border text-xs text-muted-foreground">
              {HEADERS.map((header, index) => (
                <th
                  key={header}
                  scope="col"
                  className={cn("px-3 py-2 font-normal", index === 0 ? "text-left" : "text-right")}
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/60">
            {ordered.map((row) => {
              const weight =
                totalValue && row.value != null ? Number(row.value) / totalValue : null;
              return (
                <tr key={row.instrument.instrument_id} data-testid="holding-row">
                  <th scope="row" className="px-3 py-2 text-left font-medium">
                    {row.instrument.symbol}
                  </th>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatNumber(row.quantity)}
                  </td>
                  <td
                    className="px-3 py-2 text-right tabular-nums"
                    title={row.avg_price == null ? "No purchase price on record" : undefined}
                  >
                    {row.avg_price == null ? EMPTY_CELL : formatNumber(row.avg_price, { decimals: 2 })}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {row.price == null ? EMPTY_CELL : formatNumber(row.price, { decimals: 2 })}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatMoney(row.invested ?? null)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatMoney(row.value ?? null)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                    {weight === null ? EMPTY_CELL : formatNumber(weight * 100, { decimals: 1, suffix: "%" })}
                  </td>
                  <MoveCell move={row.total_pnl} testId="holding-pnl" />
                  <MoveCell move={row.todays_pnl} testId="holding-today" />
                  <MoveCell move={row.week_pnl} testId="holding-week" />
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-border bg-muted/20" data-testid="holdings-portfolio-total">
              <td colSpan={4} className="px-3 py-2 text-xs text-muted-foreground">
                {ordered.length} {ordered.length === 1 ? "holding" : "holdings"}
              </td>
              <td className="px-3 py-2 text-right font-medium tabular-nums">
                {formatMoney(totalInvested)}
              </td>
              <td className="px-3 py-2 text-right font-medium tabular-nums">
                {formatMoney(totalValue)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                {totalValue ? "100%" : EMPTY_CELL}
              </td>
              <TotalMoveCell amount={totalPnl} base={totalInvested} />
              <TotalMoveCell amount={today} base={totalValue === null || today === null ? null : totalValue - today} />
              <TotalMoveCell amount={week} base={totalValue === null || week === null ? null : totalValue - week} />
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}
