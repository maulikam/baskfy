"use client";

import type { FragilityRunOut } from "@decile/api-client";

import { UNKNOWN } from "@/lib/backtests/metrics";
import { formatPercent } from "@/lib/format";

/**
 * docs/10 §"What to show the user (honesty features)":
 *
 *     "A 'fragility' readout: the same config re-run with +/-1 rebalance-day offset and +/-25%
 *      costs, so users can see whether the result survives small perturbations. **Most won't.
 *      That is the point.**"
 *
 * The spread is stated in words as well as in a table, because a column of five CAGRs is only
 * legible to someone who already knows what they are looking for. The sentence underneath says
 * how far apart the best and worst perturbation landed, which is the entire finding.
 */
export interface FragilityPanelProps {
  runs: readonly FragilityRunOut[];
}

export function FragilityPanel({ runs }: FragilityPanelProps) {
  if (runs.length === 0) {
    return (
      <section className="rounded-lg border border-border bg-card p-4" aria-labelledby="fragility">
        <h2 id="fragility" className="text-sm font-semibold">
          Fragility
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          This run was queued without the fragility probe, so there is nothing to compare against.
        </p>
      </section>
    );
  }

  const cagrs = runs
    .map((run) => run.cagr)
    .filter((value): value is number => typeof value === "number");
  const spread =
    cagrs.length > 1 ? Math.max(...cagrs) - Math.min(...cagrs) : null;
  const base = runs.find((run) => run.label === "base");

  return (
    <section className="space-y-3" aria-labelledby="fragility">
      <h2 id="fragility" className="text-sm font-semibold">
        Fragility
      </h2>
      <p className="text-sm text-muted-foreground">
        The same configuration, re-run four more ways: every cost component moved by a quarter, and
        every rebalance moved by one trading day. If the result only exists at one exact setting,
        it is not a result.
      </p>
      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <caption className="sr-only">The same backtest under four perturbations</caption>
          <thead className="bg-muted/50 text-left text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th scope="col" className="px-3 py-2 font-medium">
                Variant
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                CAGR
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Total return
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Max drawdown
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Fills
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.label}
                className={`border-t border-border ${run.label === "base" ? "bg-muted/30" : ""}`}
              >
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  {run.description}
                </th>
                <td className="px-3 py-2 text-right font-mono tabular-nums">
                  {run.cagr === null || run.cagr === undefined
                    ? UNKNOWN
                    : formatPercent(run.cagr * 100, 2)}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums">
                  {run.total_return === null || run.total_return === undefined
                    ? UNKNOWN
                    : formatPercent(run.total_return * 100, 2)}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums">
                  {run.max_drawdown === null || run.max_drawdown === undefined
                    ? UNKNOWN
                    : formatPercent(run.max_drawdown * 100, 2)}
                </td>
                <td className="px-3 py-2 text-right font-mono tabular-nums">{run.trades}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {spread !== null && base?.cagr !== null && base?.cagr !== undefined ? (
        <p className="text-sm">
          Across the five runs, CAGR spans{" "}
          <strong className="font-mono tabular-nums">{formatPercent(spread * 100, 2)}</strong> — the
          headline number is {formatPercent(base.cagr * 100, 2)}, and moving the rebalance day by
          one or the costs by a quarter moves it by that much.
        </p>
      ) : null}
    </section>
  );
}
