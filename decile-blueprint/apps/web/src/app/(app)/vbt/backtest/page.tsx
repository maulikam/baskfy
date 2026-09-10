import type { Metadata } from "next";

import { Answer, Mark } from "@/components/shell/answer";
import { PageHeader } from "@/components/shell/page-header";
import { SectionTabs } from "@/components/shell/section-tabs";
import {
  fetchBacktest,
  type VbtBacktestBook,
  type VbtBacktestRun,
} from "@/lib/vbt/fetch";
import { PAGES } from "@/lib/vocabulary";

import { BacktestCaveats } from "../_components/caveats";
import { EquityCurve } from "../_components/equity-curve";

/**
 * `/vbt/backtest` — the Backtest tab of `docs/vbt/05` §2.
 *
 * The caveats are **above** the numbers and they are a component, not a footer (house rule 9).
 * That ordering is the page's only real design decision: these caveats are not fine print about
 * a result, they are the conditions under which the result means anything, and a reader who has
 * seen 18.2% before reading them has already formed the belief they exist to prevent.
 *
 * Below them, the study's published numbers beside whatever this box last re-ran, and a drift
 * banner when the two are more than a CAGR point apart (VB9). Drift is not a rounding
 * disagreement — it means the bars changed or the code did, and until somebody explains which,
 * the published number should not be used.
 *
 * **Read-only**, like the rest of the hub.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/vbt/backtest"].title,
  description: PAGES["/vbt/backtest"].blurb,
};

function number(
  value: number | string | null | undefined,
  suffix = "",
): string {
  if (value === null || value === undefined) return "—";
  const asNumber = typeof value === "string" ? Number(value) : value;
  return Number.isFinite(asNumber) ? `${asNumber.toFixed(1)}${suffix}` : "—";
}

/**
 * One statistic out of a run's **primary** book.
 *
 * `vb_backtest_run.stats` is keyed by book — `full`, `gate_off`, `raw_scan` — because `04` §11
 * reports three over one detection pass. `full` is the strategy; the other two exist to measure
 * what the gate and the filters are each worth.
 */
function stat(run: VbtBacktestRun, key: string): number | string | null {
  const value = run.stats?.full?.[key];
  return typeof value === "number" || typeof value === "string" ? value : null;
}

function book(run: VbtBacktestRun, label: "full" | "gate_off" | "raw_scan"): VbtBacktestBook | null {
  return run.stats?.[label] ?? null;
}

/** `01` §3's ablation, recomputed: what the gate and the filters were worth on these bars. */
function contribution(run: VbtBacktestRun, against: "gate_off" | "raw_scan"): number | null {
  const full = book(run, "full")?.cagr_pct;
  const other = book(run, against)?.cagr_pct;
  return typeof full === "number" && typeof other === "number" ? full - other : null;
}

/** `05` §2's three books: the strategy, the strategy without its gate, and the raw scan. */
const SOURCE_LABEL: Record<string, string> = {
  PLANT: "Re-run from the data plant's bars",
  full: "The strategy",
  gate_off: "Without the breadth gate",
  raw_scan: "The raw Chartink scan",
};

export default async function VbtBacktestPage() {
  const backtest = await fetchBacktest();
  const published = backtest?.published ?? null;
  const runs = backtest?.runs ?? [];
  const drifted = runs.filter((run) => run.drift?.flagged);

  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/vbt/backtest"].title}
        blurb={PAGES["/vbt/backtest"].blurb}
      />
      <SectionTabs section="vbt" />

      <BacktestCaveats />

      {drifted.length > 0 ? (
        <div
          role="alert"
          data-testid="drift-banner"
          className="rounded-lg border border-rose-600/50 bg-rose-50/60 p-4 text-sm dark:bg-rose-950/20"
        >
          <p className="font-medium text-rose-800 dark:text-rose-300">
            This run is{" "}
            {Math.abs(Number(drifted[0]?.drift?.cagr_pct_delta ?? 0)).toFixed(1)}{" "}
            points away from the published number.
          </p>
          <p className="mt-1 max-w-[80ch] text-foreground/90">
            The bars changed, or the code did. Do not use the published number
            until this is explained.
          </p>
        </div>
      ) : null}

      <Answer
        footnote={
          "Modelled results from historical bars. Past behaviour of a rule over one country, one " +
          "regime and nine years is not a forecast, and nothing on this page is advice."
        }
      >
        {published ? (
          <>
            Over {published.years.toFixed(1)} years the rule returned{" "}
            <Mark>{published.cagr_pct.toFixed(1)}% a year</Mark> with a worst
            drawdown of <Mark>{published.max_drawdown_pct.toFixed(1)}%</Mark>,
            across{" "}
            <Mark>{published.trades.toLocaleString("en-IN")} trades</Mark>{" "}
            &mdash; but the in-sample half returned{" "}
            {published.in_sample_cagr_pct.toFixed(1)}% and the out-of-sample
            half {published.out_of_sample_cagr_pct.toFixed(1)}%, and that spread
            matters more than the average of the two.
          </>
        ) : (
          <>The study&rsquo;s numbers are not available.</>
        )}
      </Answer>

      {published ? (
        <section aria-label="The study against what ran here" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            The study, and what last ran here
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[48rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium" />
                  <th className="py-2 pr-3 font-medium">The study</th>
                  {runs.map((run) => (
                    <th key={run.id} className="py-2 pr-3 font-medium">
                      {SOURCE_LABEL[run.source] ?? run.source}
                      <span className="block font-normal normal-case text-muted-foreground">
                        {run.finished_at
                          ? new Date(run.finished_at).toISOString().slice(0, 10)
                          : "—"}
                      </span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ["CAGR", published.cagr_pct, "cagr_pct", "%"],
                    [
                      "Max drawdown",
                      published.max_drawdown_pct,
                      "max_drawdown_pct",
                      "%",
                    ],
                    ["Trades", published.trades, "trades", ""],
                    ["Win rate", published.win_rate_pct, "win_rate_pct", "%"],
                    [
                      "Profit factor",
                      published.profit_factor,
                      "profit_factor",
                      "",
                    ],
                    [
                      "Average hold",
                      published.avg_hold_sessions,
                      "avg_hold_sessions",
                      " sessions",
                    ],
                    ["Exposure", published.exposure_pct, "exposure_pct", "%"],
                  ] as const
                ).map(([label, studyValue, key, suffix]) => (
                  <tr key={key} className="border-b border-border/40">
                    <th scope="row" className="py-2 pr-3 text-left font-normal">
                      {label}
                    </th>
                    <td className="py-2 pr-3 tabular-nums">
                      {number(studyValue, suffix)}
                    </td>
                    {runs.map((run) => (
                      <td key={run.id} className="py-2 pr-3 tabular-nums">
                        {number(stat(run, key), suffix)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {runs.length === 0 ? (
            <p className="text-sm text-muted-foreground" data-testid="no-runs">
              The backtest has not been re-run here yet, so there is nothing to
              compare the study against.
            </p>
          ) : null}
        </section>
      ) : null}

      {runs.length > 0 && book(runs[0]!, "full")?.equity_curve?.length ? (
        <section aria-label="Equity curve" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            What the run did, session by session
          </h2>
          <EquityCurve data={book(runs[0]!, "full")!.equity_curve!} />
        </section>
      ) : null}

      {runs.length > 0 && book(runs[0]!, "full")?.yearly?.length ? (
        <section aria-label="Year by year" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            Year by year
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border/70 text-left text-xs uppercase tracking-wide text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">Year</th>
                  <th className="py-2 pr-3 font-medium">Return</th>
                  <th className="py-2 pr-3 font-medium">Trades</th>
                  <th className="py-2 pr-3 font-medium">Win rate</th>
                </tr>
              </thead>
              <tbody data-testid="yearly-table">
                {book(runs[0]!, "full")!.yearly!.map((row) => (
                  <tr key={row.year} className="border-b border-border/40">
                    <td className="py-2 pr-3 tabular-nums">{row.year}</td>
                    <td className="py-2 pr-3 tabular-nums">{row.return_pct.toFixed(1)}%</td>
                    <td className="py-2 pr-3 tabular-nums">{row.trades}</td>
                    <td className="py-2 pr-3 tabular-nums">{row.win_rate_pct.toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {runs.length > 0 && contribution(runs[0]!, "gate_off") !== null ? (
        <section aria-label="What each part is worth" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            What the gate and the filters were worth, on these bars
          </h2>
          <dl
            className="grid max-w-[46rem] grid-cols-2 gap-x-6 gap-y-1 text-sm"
            data-testid="contributions"
          >
            <dt className="text-muted-foreground">The breadth gate</dt>
            <dd className="tabular-nums">
              {contribution(runs[0]!, "gate_off")!.toFixed(1)} points of CAGR against the same
              strategy with the gate held open
            </dd>
            <dt className="text-muted-foreground">The six trend filters</dt>
            <dd className="tabular-nums">
              {contribution(runs[0]!, "raw_scan") === null
                ? "—"
                : `${contribution(runs[0]!, "raw_scan")!.toFixed(1)} points of CAGR against the raw scan, traded the same way`}
            </dd>
          </dl>
          <p className="max-w-[80ch] text-xs text-muted-foreground">
            The gate is worth more than its CAGR difference suggests: it buys most of its keep in
            drawdown, not in return. Compare the two drawdowns above before reading either number
            as the gate&rsquo;s value.
          </p>
        </section>
      ) : null}

      {published ? (
        <section aria-label="The two halves" className="space-y-2">
          <h2 className="text-sm font-medium uppercase tracking-wide text-muted-foreground">
            The two halves, which the headline averages away
          </h2>
          <dl className="grid max-w-[40rem] grid-cols-2 gap-x-6 gap-y-1 text-sm">
            <dt className="text-muted-foreground">In sample</dt>
            <dd className="tabular-nums">
              {published.in_sample_cagr_pct.toFixed(1)}% a year, worst drawdown{" "}
              {published.in_sample_dd_pct.toFixed(1)}%
            </dd>
            <dt className="text-muted-foreground">Out of sample</dt>
            <dd className="tabular-nums">
              {published.out_of_sample_cagr_pct.toFixed(1)}% a year, worst
              drawdown {published.out_of_sample_dd_pct.toFixed(1)}%
            </dd>
            <dt className="text-muted-foreground">Modelled fill rate</dt>
            <dd className="tabular-nums">
              {published.modelled_fill_rate_pct.toFixed(1)}%
            </dd>
            <dt className="text-muted-foreground">Window</dt>
            <dd className="tabular-nums">
              {published.start} to {published.end}
            </dd>
          </dl>
        </section>
      ) : null}
    </div>
  );
}
