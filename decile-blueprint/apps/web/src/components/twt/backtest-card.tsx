import { REASONS } from "@/lib/twt/copy";
import type { TwtBacktest, TwtBacktestRun } from "@/lib/twt/fetch";
import { percent } from "@/lib/twt/numbers";
import { backtestFigures, gateComparison, latestFinished } from "@/lib/twt/view";

import { BacktestCaveats } from "./caveats";
import { EquityCurve } from "./equity-curve";
import { FigureCell, FigureValue } from "./figure";

/**
 * `docs/twt/05` §3 — what the rule did, with the conditions on it read first.
 *
 * TWO RESULTS, SIDE BY SIDE, NEVER MIXED
 * --------------------------------------
 * One run is computed from this product's own price history; the other reproduces the research
 * against the export it was written from. `05` §3: never mixed, never averaged. They answer
 * different questions — "does our data produce the study's result" and "does the study's result
 * reproduce at all" — and an average of the two answers neither.
 *
 * Each column is the latest **finished** run of its kind, never the latest started: a run in
 * flight, or a failed re-run, must not displace the last good number (`03` §9).
 *
 * THE DRIFT WARNING IS WHY THIS PAGE EXISTS
 * -----------------------------------------
 * When a fresh run's annual return moves more than a point from the published one, the page says
 * so and names both numbers. A silent drift — the published figure quietly becoming a different
 * figure while the page still reads like a settled fact — is the failure this card was built to
 * prevent.
 */
export function BacktestCard({ backtest }: { backtest: TwtBacktest | null }) {
  const plant = latestFinished(backtest, "PLANT");
  const research = latestFinished(backtest, "RESEARCH_EXPORT");

  return (
    <div className="space-y-8">
      <BacktestCaveats />

      {plant === null && research === null ? (
        <p className="max-w-[80ch] text-sm text-muted-foreground" data-testid="twt-backtest-empty">
          No completed run has been recorded yet, so there is nothing to show. A run that is still
          going, or one that failed, is deliberately not displayed here: the last settled result is
          the only number worth reading, and there is not one yet.
        </p>
      ) : (
        <div className="grid gap-6 md:grid-cols-2">
          <RunColumn
            heading="Measured on our own price history"
            explanation="The rule replayed over the prices this product stores and serves."
            run={plant}
          />
          <RunColumn
            heading="Measured on the research data"
            explanation="The same rule replayed over the data the original study was written from."
            run={research}
          />
        </div>
      )}
    </div>
  );
}

function RunColumn({
  heading,
  explanation,
  run,
}: {
  heading: string;
  explanation: string;
  run: TwtBacktestRun | null;
}) {
  const figures = backtestFigures(run);
  const comparison = gateComparison(run);
  const drift = run?.drift ?? null;
  const flagged = drift?.flagged === true;

  return (
    <section className="space-y-4 rounded-lg border border-border/70 bg-card p-4">
      <div>
        <h2 className="text-sm font-semibold">{heading}</h2>
        <p className="mt-1 text-xs text-muted-foreground">{explanation}</p>
      </div>

      {run === null ? (
        <p className="text-sm text-muted-foreground" data-testid="twt-run-unavailable">
          {REASONS.noFinishedRun}. Nothing is shown here rather than a figure from a run that did
          not finish.
        </p>
      ) : (
        <>
          {flagged ? (
            <p
              className="rounded-md border border-warning/40 bg-warning-muted p-3 text-sm text-foreground"
              data-testid="twt-drift"
            >
              <strong>This run disagrees with the published result.</strong> It produced an annual
              return of{" "}
              {drift?.run_cagr_pct === undefined
                ? "a different figure"
                : percent(drift.run_cagr_pct, 1)}{" "}
              where the study published{" "}
              {drift?.published_cagr_pct === undefined
                ? "another"
                : percent(drift.published_cagr_pct, 1)}
              . Until that difference is explained, treat neither as settled.
            </p>
          ) : null}

          <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
            {figures.map((entry) => (
              <div key={entry.label}>
                <dt className="text-xs uppercase tracking-wide text-muted-foreground">
                  {entry.label}
                </dt>
                <dd className="text-base font-medium">
                  <FigureValue figure={entry.figure} />
                </dd>
              </div>
            ))}
          </dl>

          <div
            className="rounded-md border border-border/60 p-3"
            data-testid="twt-gate-comparison"
          >
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              What the entry gate was worth
            </h3>
            <div className="mt-2 grid grid-cols-2 gap-4">
              <FigureCell label="With the gate" figure={comparison.withGate} />
              <FigureCell label="Ignoring the gate" figure={comparison.withoutGate} />
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              The same rule, run twice: once refusing new entries when too little of the market was
              rising, once entering regardless. The difference between the two is the only argument
              for the gate.
            </p>
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              How the money grew
            </h3>
            <div className="mt-2">
              <EquityCurve
                points={run.stats?.equity_curve ?? []}
                label={`${heading}: how the invested amount changed over the test`}
              />
            </div>
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Year by year
            </h3>
            {(run.stats?.yearly ?? []).length === 0 ? (
              <p className="mt-2 text-xs text-muted-foreground">
                This run did not record a year-by-year breakdown.
              </p>
            ) : (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Year
                      </th>
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Return
                      </th>
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Trades
                      </th>
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Winners
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(run.stats?.yearly ?? []).map((year) => (
                      <tr key={year.year} className="border-b border-border/40">
                        <td className="py-1.5 pr-3 tabular-nums">{year.year}</td>
                        <td className="py-1.5 pr-3 tabular-nums">
                          {percent(year.return_pct, 1, { sign: true })}
                        </td>
                        <td className="py-1.5 pr-3 tabular-nums">{year.trades}</td>
                        <td className="py-1.5 pr-3 tabular-nums">
                          {percent(year.win_rate_pct, 0)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}
