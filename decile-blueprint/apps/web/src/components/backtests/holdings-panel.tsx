"use client";

import type { BacktestHoldingOut } from "@baskfy/api-client";
import { useMemo, useState } from "react";

import { EmptyState } from "@/components/data/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber } from "@/lib/format";

/**
 * docs/08 §Backtests: "per-period holdings". docs/10 §Artefacts: "per-rebalance holdings with
 * weights".
 *
 * Both weights are shown. `target` is what the rank-buffer rule and the weighting scheme asked
 * for; `actual` is what whole-share rounding produced (docs/10 §5). On a small account holding an
 * expensive share the two differ by a lot, and showing only the target would describe a portfolio
 * the simulation never held.
 */
export interface HoldingsPanelProps {
  holdings: readonly BacktestHoldingOut[];
  loading: boolean;
}

export function HoldingsPanel({ holdings, loading }: HoldingsPanelProps) {
  const dates = useMemo(
    () => [...new Set(holdings.map((holding) => holding.rebalance_date))].sort().reverse(),
    [holdings],
  );
  const [selected, setSelected] = useState<string>("");
  const active = selected || dates[0] || "";
  const rows = holdings.filter((holding) => holding.rebalance_date === active);

  if (loading) return <Skeleton className="h-64 w-full" />;
  if (dates.length === 0) {
    return (
      <EmptyState
        title="No holdings"
        reason="This backtest never took a position, so there is no rebalance to show."
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <label htmlFor="rebalance-date" className="text-sm text-muted-foreground">
          Rebalance
        </label>
        <Select
          id="rebalance-date"
          value={active}
          onChange={(event) => setSelected(event.target.value)}
          className="w-auto"
        >
          {dates.map((date) => (
            <option key={date} value={date}>
              {date}
            </option>
          ))}
        </Select>
        <span className="text-xs text-muted-foreground">
          Decided on {active}, filled at the next open.
        </span>
      </div>

      <div className="max-h-[28rem] overflow-auto rounded-lg border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Holdings after the rebalance on {active}</caption>
          <thead className="sticky top-0 bg-muted text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">Rank</th>
              <th scope="col" className="px-3 py-2 font-medium">Symbol</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Qty</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Price</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Value</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Target</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Actual</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((holding) => (
              <tr key={holding.symbol} className="border-t border-border">
                <td className="px-3 py-1.5 font-mono tabular-nums text-muted-foreground">
                  {holding.rank ?? "—"}
                </td>
                <td className="px-3 py-1.5 font-medium">{holding.symbol}</td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(holding.quantity, { decimals: 0 })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(holding.price, { decimals: 2 })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(holding.value, { decimals: 0 })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums text-muted-foreground">
                  {formatNumber(Number(holding.target_weight) * 100, { decimals: 2, suffix: "%" })}
                </td>
                <td className="px-3 py-1.5 text-right font-mono tabular-nums">
                  {formatNumber(Number(holding.actual_weight) * 100, { decimals: 2, suffix: "%" })}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
