"use client";

import type { TradeOut } from "@baskfy/api-client";

import { EmptyState } from "@/components/data/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber } from "@/lib/format";

/**
 * docs/08 §Backtests: "trade log". docs/10 §Artefacts fixes the columns:
 * "date, symbol, side, qty, price, cost, reason ∈ `enter|exit|rebalance|delist`".
 *
 * The `reason` column is the one worth reading. A `delist` row is a position the engine was
 * forced out of because the instrument stopped trading (docs/10 §8) — the single line of evidence
 * that this backtest did not quietly drop its failures.
 */
export interface TradeLogProps {
  trades: readonly TradeOut[];
  loading: boolean;
  truncated: boolean;
}

const REASON_LABELS: Record<string, string> = {
  enter: "Enter",
  exit: "Exit",
  rebalance: "Rebalance",
  delist: "Delisted",
};

export function TradeLog({ trades, loading, truncated }: TradeLogProps) {
  if (loading) return <Skeleton className="h-64 w-full" />;
  if (trades.length === 0) {
    return (
      <EmptyState
        title="No fills"
        reason="This configuration never traded — the screen returned nothing on every rebalance date, or the book could not afford a single whole share."
      />
    );
  }

  return (
    <div className="space-y-2">
      <div className="max-h-[28rem] overflow-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Every fill this backtest executed</caption>
          <thead className="sticky top-0 bg-muted text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">Date</th>
              <th scope="col" className="px-3 py-2 font-medium">Symbol</th>
              <th scope="col" className="px-3 py-2 font-medium">Side</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Qty</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Price</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Cost</th>
              <th scope="col" className="px-3 py-2 font-medium">Reason</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((trade, index) => (
              <tr key={`${trade.date}-${trade.symbol}-${index}`} className="border-t border-border">
                <td className="px-3 py-1.5 font-mono tabular-nums">{trade.date}</td>
                <td className="px-3 py-1.5 font-medium">{trade.symbol}</td>
                <td
                  className={`px-3 py-1.5 ${trade.side === "buy" ? "text-positive" : "text-negative"}`}
                >
                  {trade.side === "buy" ? "Buy" : "Sell"}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(trade.quantity, { decimals: 0 })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(trade.price, { decimals: 2 })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(trade.cost, { decimals: 2 })}
                </td>
                <td className="px-3 py-1.5">
                  <span
                    className={
                      trade.reason === "delist"
                        ? "rounded bg-warning-muted px-1.5 py-0.5 text-xs text-warning"
                        : "text-xs text-muted-foreground"
                    }
                  >
                    {REASON_LABELS[trade.reason] ?? trade.reason}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated ? (
        <p className="text-xs text-muted-foreground">
          Showing the first {trades.length} fills. Download the CSV for the whole log.
        </p>
      ) : null}
    </div>
  );
}
