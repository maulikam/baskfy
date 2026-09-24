import type { Metadata } from "next";

import { FnoRiskCaveat, ScanOnly } from "@/components/options/caveats";
import { PageHeader } from "@/components/shell/page-header";
import { OptionsSubNav, SectionTabs } from "@/components/shell/section-tabs";
import { formatTradeDate } from "@/lib/format";
import { fetchOptionsBacktest, fetchOptionsJournal } from "@/lib/options/fetch";
import type {
  OptionsBacktestRun,
  OptionsSummary,
  SleeveGroupCode,
} from "@/lib/options/types";
import {
  GROUP_NAME,
  PAPER_UNIT,
  SLEEVE_NAME,
  SLEEVE_SHORT,
  fractionPct,
  inr,
  reasonText,
  sizingText,
} from "@/lib/options/view";
import { PAGES } from "@/lib/vocabulary";

/**
 * `/options/journal` — `docs/options/05` §2's journal: per sleeve, `04` §12's summary with
 * **real and simulated apart and paper-one-lot apart** (the API never pools them, OP1.6), the R
 * spread, the closes by reason, each sleeve's paper-period progress (`02` §3.2), and the backtest
 * cards per sleeve per tier with **each tier's caveat from its own row** — or an honest "not run
 * yet". Read-only: nothing here writes.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: PAGES["/options/journal"].title,
  description: PAGES["/options/journal"].blurb,
};

const R_BINS: readonly { label: string; low: number; high: number }[] = [
  { label: "below −1 R", low: -Infinity, high: -1 },
  { label: "−1 to 0 R", low: -1, high: 0 },
  { label: "0 to 1 R", low: 0, high: 1 },
  { label: "1 R and above", low: 1, high: Infinity },
];

function histogram(
  values: readonly string[],
): { label: string; count: number }[] {
  const numbers = values.map(Number).filter(Number.isFinite);
  return R_BINS.map((bin) => ({
    label: bin.label,
    count: numbers.filter((n) => n >= bin.low && n < bin.high).length,
  }));
}

function SummaryCard({
  summary,
  minSessions,
}: {
  summary: OptionsSummary;
  minSessions: number;
}) {
  const bins = histogram(summary.r_values);
  const most = Math.max(1, ...bins.map((bin) => bin.count));
  return (
    <article
      className="space-y-2 rounded-lg border border-border/70 bg-card p-4"
      data-testid="options-summary"
    >
      <h3 className="text-sm font-semibold">
        {SLEEVE_NAME[summary.sleeve]} ({SLEEVE_SHORT[summary.sleeve]}) ·{" "}
        {summary.simulated ? "paper" : "real"} ·{" "}
        {sizingText(summary.sizing_mode)}
      </h3>
      {summary.count < minSessions ? (
        <p className="text-xs text-warning" data-testid="options-sample-banner">
          {summary.count} of the {minSessions} sessions this sleeve needs before
          its numbers mean much — read them as a count, not a result.
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4 tabular-nums">
        <div>
          <dt className="text-xs text-muted-foreground">Traded</dt>
          <dd>
            {summary.traded} of {summary.count}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Win rate</dt>
          <dd>
            {summary.win_rate === null
              ? "no trades yet"
              : fractionPct(summary.win_rate)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Expectancy</dt>
          <dd>
            {summary.expectancy_r === null
              ? "no trades yet"
              : `${summary.expectancy_r} R · ${inr(summary.expectancy_inr)}`}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Worst drawdown</dt>
          <dd>
            {summary.max_drawdown_r} R · {inr(summary.max_drawdown_inr)}
          </dd>
        </div>
      </dl>
      <ul className="space-y-0.5 text-xs" aria-label="R spread">
        {bins.map((bin) => (
          <li key={bin.label} className="flex items-center gap-2">
            <span className="w-28 text-muted-foreground">{bin.label}</span>
            <span
              className="h-2 rounded bg-accent"
              style={{ width: `${(bin.count / most) * 8}rem` }}
              aria-hidden="true"
            />
            <span className="tabular-nums">{bin.count}</span>
          </li>
        ))}
      </ul>
      {summary.by_closed_reason.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          Closed by:{" "}
          {summary.by_closed_reason
            .map(
              (bucket) =>
                `${reasonText(bucket.label).toLowerCase()} ${bucket.count} (${bucket.mean_r} R)`,
            )
            .join(" · ")}
        </p>
      ) : null}
    </article>
  );
}

function BacktestCard({ run }: { run: OptionsBacktestRun }) {
  return (
    <article
      className="space-y-2 rounded-lg border border-border/70 bg-card p-4"
      data-testid="options-backtest-card"
    >
      <h3 className="text-sm font-semibold">
        {SLEEVE_NAME[run.sleeve]} · Tier {run.tier} ·{" "}
        {formatTradeDate(run.date_from)} to {formatTradeDate(run.date_to)}
      </h3>
      <p
        className="whitespace-pre-line rounded-md bg-warning-muted p-2 text-xs text-foreground"
        data-testid="options-backtest-caveat"
      >
        {run.caveats}
      </p>
      <p className="text-sm tabular-nums">
        {run.sessions} sessions · {run.signals} signals · {run.traded} traded
        {run.expectancy_r !== null ? ` · expectancy ${run.expectancy_r} R` : ""}
        {run.win_rate !== null
          ? ` · win rate ${fractionPct(run.win_rate)}`
          : ""}
      </p>
    </article>
  );
}

export default async function OptionsJournalPage() {
  const [journal, backtest] = await Promise.all([
    fetchOptionsJournal(),
    fetchOptionsBacktest(),
  ]);
  const groups: SleeveGroupCode[] = ["O1M", "O1W", "O2", "O3"];
  return (
    <div className="space-y-8">
      <PageHeader
        title={PAGES["/options/journal"].title}
        blurb={PAGES["/options/journal"].blurb}
        meta={<ScanOnly />}
      />
      <OptionsSubNav />
      <SectionTabs section="options" />
      <FnoRiskCaveat />

      <section aria-labelledby="options-progress-heading" className="space-y-3">
        <h2
          id="options-progress-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Paper period, per strategy
        </h2>
        <ul
          className="grid gap-2 sm:grid-cols-2"
          data-testid="options-progress"
        >
          {groups.map((group) => {
            const p = journal?.progress.find((row) => row.group === group);
            const done = p?.sessions_done ?? 0;
            const needed = p?.sessions_needed ?? 0;
            return (
              <li
                key={group}
                className="rounded-lg border border-border/70 bg-card p-3 text-sm"
              >
                <p className="font-medium">{GROUP_NAME[group]}</p>
                <p className="tabular-nums text-muted-foreground">
                  {done} of {needed} {PAPER_UNIT[group]}, {p?.traded ?? 0} of{" "}
                  {p?.traded_needed ?? 0} traded
                </p>
                <div
                  className="mt-1 h-1.5 w-full rounded bg-muted"
                  aria-hidden="true"
                >
                  <div
                    className="h-1.5 rounded bg-accent"
                    style={{
                      width: `${needed ? Math.min(100, (done / needed) * 100) : 0}%`,
                    }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
        <p className="text-xs text-muted-foreground">
          The paper periods begin once the desk runs the sleeves on the live
          chain. A skipped day counts as a session; a day the desk was down does
          not.
        </p>
      </section>

      <section
        aria-labelledby="options-summaries-heading"
        className="space-y-3"
      >
        <h2
          id="options-summaries-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Journal
        </h2>
        {!journal || journal.summaries.length === 0 ? (
          <p className="text-sm" data-testid="options-journal-empty">
            No options session has been journalled yet. The journal fills once
            the desk runs a sleeve on paper; scans alone write nothing here.
          </p>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {journal.summaries.map((summary) => (
              <SummaryCard
                key={`${summary.sleeve}-${summary.simulated}-${summary.sizing_mode}`}
                summary={summary}
                minSessions={journal.min_sessions[summary.sleeve] ?? 0}
              />
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="options-backtest-heading" className="space-y-3">
        <h2
          id="options-backtest-heading"
          className="text-sm font-medium uppercase tracking-wide text-muted-foreground"
        >
          Backtests
        </h2>
        {!backtest || backtest.runs.length === 0 ? (
          <p className="text-sm" data-testid="options-backtest-empty">
            Not run yet. Each sleeve gets its own backtest per tier, each with
            its own caveat; the tier that prices real option quotes waits on the
            paper period&rsquo;s data.
          </p>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {backtest.runs.map((run) => (
              <BacktestCard key={run.id} run={run} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
