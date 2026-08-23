"use client";

import type { FragilityRunOut } from "@baskfy/api-client";

import { UNKNOWN } from "@/lib/backtests/metrics";
import { formatPercent } from "@/lib/format";

/**
 * docs/10 §"What to show the user (honesty features)":
 *
 *     "A 'fragility' readout: the same config re-run with +/-1 rebalance-day offset and +/-25%
 *      costs, so users can see whether the result survives small perturbations. **Most won't.
 *      That is the point.**"
 *
 * ## Why this panel shows how much each variant could see (M45.7)
 *
 * That last sentence is the trap. A variant whose screen returned *nothing* also diverges wildly
 * from the base run — the engine selects no names and the book sits in cash — and the panel used
 * to render that as a wide CAGR spread. A hole in the data was displayed as exactly the finding
 * the documentation tells the reader to expect, so nobody would look twice at it.
 *
 * The engine has counted this since M39 (`BacktestResult.blind_rebalances`) and the API has
 * served it since M43. It had never reached a person.
 *
 * Measured against the live database on 2026-08-23: of the rebalance dates the coverage guard
 * passes, **100% have both the +1 and the -1 offset blind** — `factor_daily` and
 * `index_member_daily` are weekly series (the dominant gap between sampled dates is five
 * sessions), so a neighbouring trading day has no factor rows by construction. The offset probe
 * cannot see anything on this data plant, and until factors are daily it never will.
 *
 * So the panel does not average across variants that saw nothing. A spread computed over them is
 * not a weaker finding, it is a wrong number.
 */
/**
 * `blind_pct` and `rebalances` are served by the API (`fragility_payload`, M43) and declared on
 * `FragilityRunOut` in `schemas.py` (M45.7), but `packages/api-client/src/generated/schema.ts` is
 * regenerated on the concurrent SC branch and not here — running `make client` against this
 * working tree would sweep that session's in-progress endpoints into this commit.
 *
 * So the type is widened locally rather than hand-edited into a generated file. Once the client
 * is regenerated the intersection is a no-op and this alias can go.
 */
type FragilityRun = FragilityRunOut & {
  blind_pct?: number;
  rebalances?: number;
};

export interface FragilityPanelProps {
  runs: readonly FragilityRun[];
}

/** A variant that missed even one rebalance is not a perturbation of the strategy. */
function sawEverything(run: FragilityRun): boolean {
  return (run.blind_pct ?? 0) === 0;
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

  const comparable = runs.filter(sawEverything);
  const blind = runs.filter((run) => !sawEverything(run));
  const cagrs = comparable
    .map((run) => run.cagr)
    .filter((value): value is number => typeof value === "number");
  const spread = cagrs.length > 1 ? Math.max(...cagrs) - Math.min(...cagrs) : null;
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
      <div className="overflow-x-auto rounded-lg border border-border">
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
              <th scope="col" className="px-3 py-2 text-right font-medium">
                Saw nothing
              </th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => {
              const blindPct = run.blind_pct ?? 0;
              const usable = blindPct === 0;
              return (
                <tr
                  key={run.label}
                  className={`border-t border-border ${
                    run.label === "base" ? "bg-muted/30" : ""
                  } ${usable ? "" : "text-muted-foreground"}`}
                >
                  <th scope="row" className="px-3 py-2 text-left font-normal">
                    {run.description}
                    {usable ? null : (
                      <span className="ml-2 rounded bg-warning/10 px-1.5 py-0.5 text-xs font-medium text-warning">
                        not comparable
                      </span>
                    )}
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
                  <td className="px-3 py-2 text-right font-mono tabular-nums">
                    {formatPercent(blindPct, 0)}
                    {run.rebalances ? (
                      <span className="ml-1 text-xs text-muted-foreground">
                        of {run.rebalances}
                      </span>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {blind.length > 0 ? (
        <p className="rounded-lg border border-warning/30 bg-warning/5 p-3 text-sm">
          <strong className="font-semibold">
            {blind.length} of these {runs.length} runs decided with an empty screen.
          </strong>{" "}
          Those rebalances had no data to pick from, so the money sat in cash and the run ended
          somewhere far from the others. That gap is missing data, not a fragile strategy, and it is
          left out of the comparison below. Moving a rebalance by one trading day currently lands on
          a day this database has never scored, so the offset runs cannot tell you anything yet.
        </p>
      ) : null}
      {spread !== null && base?.cagr !== null && base?.cagr !== undefined ? (
        <p className="text-sm">
          Across the {comparable.length} runs that saw a full screen, CAGR spans{" "}
          <strong className="font-mono tabular-nums">{formatPercent(spread * 100, 2)}</strong> — the
          headline number is {formatPercent(base.cagr * 100, 2)}, and perturbing the run moves it by
          that much.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">
          Nothing here can be compared: fewer than two of the five runs saw a full screen, so there
          is no honest spread to state.
        </p>
      )}
    </section>
  );
}
