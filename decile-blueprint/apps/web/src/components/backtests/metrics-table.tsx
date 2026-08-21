"use client";

import type { BacktestOut } from "@baskfy/api-client";

import {
  METRIC_ROWS,
  UNKNOWN,
  formatMetric,
  metricsOf,
  rollingOf,
} from "@/lib/backtests/metrics";
import { formatPercent } from "@/lib/format";

/**
 * docs/08 §Backtests: "metrics table". docs/10 §Outputs lists what is in it.
 *
 * Every metric carries a one-line explanation rather than only a label, because half of these are
 * terms of art — "Calmar", "information ratio" — and a table of unexplained ratios is a table
 * nobody acts on. The rolling twelve-month distribution gets its own block underneath: it is a
 * distribution, not a scalar, and squeezing it into one cell would be the least useful place to
 * put the single most honest number on the page (how often a year in this strategy lost money).
 */
export interface MetricsTableProps {
  backtest: BacktestOut;
}

export function MetricsTable({ backtest }: MetricsTableProps) {
  const bag = metricsOf(backtest);
  const rolling = rollingOf(bag);

  return (
    <section className="space-y-4" aria-labelledby="backtest-metrics">
      <h2 id="backtest-metrics" className="text-sm font-semibold">
        Metrics
      </h2>
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">Performance metrics for this backtest</caption>
          <thead className="bg-muted/50 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Metric
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Value
              </th>
              <th scope="col" className="hidden px-3 py-2 font-medium sm:table-cell">
                What it means
              </th>
            </tr>
          </thead>
          <tbody>
            {METRIC_ROWS.map((row) => (
              <tr key={row.key} className="border-t border-border">
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  {row.label}
                </th>
                <td className="px-3 py-2 text-right font-mono tabular-nums">
                  {formatMetric(bag[row.key], row.unit)}
                </td>
                <td className="hidden px-3 py-2 text-xs text-muted-foreground sm:table-cell">
                  {row.hint}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h3 className="text-sm font-medium">Rolling 12-month returns</h3>
        {rolling === null ? (
          <p className="mt-2 text-sm text-muted-foreground">
            This run is shorter than a year, so there is no twelve-month window to roll.
          </p>
        ) : (
          <>
            <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
              <Stat label="Worst" value={rolling.min} />
              <Stat label="5th pct" value={rolling.p05} />
              <Stat label="Median" value={rolling.median} />
              <Stat label="95th pct" value={rolling.p95} />
              <Stat label="Best" value={rolling.max} />
            </dl>
            <p className="mt-3 text-xs text-muted-foreground">
              Across {rolling.count.toLocaleString("en-IN")} overlapping twelve-month windows,{" "}
              {rolling.negative_share === null
                ? UNKNOWN
                : formatPercent(rolling.negative_share * 100, 1)}{" "}
              of them lost money.
            </p>
          </>
        )}
      </div>
    </section>
  );
}

function Stat({ label, value }: { label: string; value: number | null }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-mono text-sm tabular-nums">
        {value === null ? UNKNOWN : formatPercent(value * 100, 1)}
      </dd>
    </div>
  );
}
