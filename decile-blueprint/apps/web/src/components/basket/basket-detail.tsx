"use client";

import { useMemo, useState } from "react";

import { formatFact, visibleFactColumns } from "@/lib/basket/holding-facts";
import type { MaterializedBasket } from "@/lib/basket/materialize";
import { cn } from "@/lib/utils";

function pct(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function rupees(value: number): string {
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

/**
 * Shared basket detail — weights, holdings table sized to a chosen investment amount (Tree 6 §5.1).
 */
export function BasketDetail({
  basket,
  warnings,
  className,
  amount,
}: {
  basket: MaterializedBasket;
  warnings?: React.ReactNode;
  className?: string;
  /**
   * When given, the amount is owned by the caller and this component renders no input of its own.
   * `/basket/[slug]` and `/baskets/featured` omit it and keep the local box; a screen basket
   * passes it, because there the amount also decides what gets saved.
   */
  amount?: number | undefined;
}) {
  const [localInvest, setLocalInvest] = useState(basket.notional);
  const controlled = amount !== undefined;
  const invest = controlled ? amount : localInvest;

  const rows = useMemo(
    () =>
      controlled
        ? basket.holdings
        : basket.holdings.map((h) => ({ ...h, amount: Math.floor(invest * h.weight) })),
    [basket.holdings, controlled, invest],
  );
  const factColumns = useMemo(
    () => visibleFactColumns(rows.map((row) => row.facts)),
    [rows],
  );

  return (
    <div className={cn("flex w-full min-w-0 flex-col gap-5", className)}>
      <header className="space-y-1">
        <h2 className="text-lg font-semibold tracking-tight">{basket.name}</h2>
        <p className="max-w-prose text-sm text-muted-foreground">{basket.thesis}</p>
        {basket.asOf ? (
          <p className="text-xs text-muted-foreground">As of {basket.asOf}</p>
        ) : null}
      </header>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat label="Stocks" value={String(basket.holdings.length)} />
        <Stat label="Into stocks" value={rupees(controlled ? basket.deployed : invest)} />
        <Stat
          label="Kept as cash"
          value={controlled ? rupees(basket.cash) : `${basket.cashPct.toFixed(1)}%`}
          hint={
            controlled
              ? basket.cashPct <= 0
                ? "Only the rounding remainder"
                : `${basket.cashPct.toFixed(1)}% plus the rounding remainder`
              : undefined
          }
        />
        <Stat label="Min. amount" value={rupees(basket.minInvestment)} />
      </section>

      {controlled ? null : (
        <label className="flex max-w-xs flex-col gap-1 text-sm">
          <span className="font-medium">Investment size</span>
          <input
            type="number"
            min={basket.minInvestment}
            step={1000}
            value={invest}
            onChange={(e) => setLocalInvest(Math.max(0, Number(e.target.value) || 0))}
            className="rounded-md border border-border bg-background px-3 py-2 tabular-nums"
          />
        </label>
      )}

      {warnings}

      <div className="w-full min-w-0 overflow-hidden rounded-xl border border-border/70 bg-card">
        <div className="w-full overflow-x-auto">
          <table className="w-full min-w-full table-auto text-sm tabular-nums">
            <caption className="sr-only">Holdings for {basket.name}</caption>
            <thead>
              <tr className="border-b border-border bg-muted/50 text-left text-xs font-medium text-muted-foreground">
                <th className="px-4 py-2.5">Rank</th>
                <th className="w-full px-4 py-2.5">Stock</th>
                {factColumns.map((column) => (
                  <th
                    key={column.key}
                    className="px-4 py-2.5 text-right"
                    title={column.title}
                  >
                    {column.label}
                  </th>
                ))}
                <th className="px-4 py-2.5 text-right">Weight</th>
                <th className="px-4 py-2.5 text-right">Price</th>
                <th className="px-4 py-2.5 text-right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.symbol}
                  className="border-b border-border/60 last:border-0 hover:bg-muted/40"
                >
                  <td className="px-4 py-2.5 text-muted-foreground">{row.rank}</td>
                  <td className="px-4 py-2.5 font-medium">
                    {row.symbol}
                    {row.name !== row.symbol ? (
                      <span className="ml-2 text-xs font-normal text-muted-foreground">
                        {row.name}
                      </span>
                    ) : null}
                  </td>
                  {factColumns.map((column) => (
                    <td key={column.key} className="px-4 py-2.5 text-right">
                      {formatFact(column.key, row.facts[column.key])}
                    </td>
                  ))}
                  <td className="px-4 py-2.5 text-right">{pct(row.weight)}</td>
                  <td className="px-4 py-2.5 text-right">
                    {row.price === null ? "—" : row.price.toFixed(2)}
                  </td>
                  <td className="px-4 py-2.5 text-right">{rupees(row.amount)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string | undefined;
}) {
  return (
    <div className="rounded-xl border border-border/70 bg-card p-4">
      <div className="eyebrow">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold tabular-nums">{value}</div>
      {hint ? <div className="mt-1 text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  );
}
