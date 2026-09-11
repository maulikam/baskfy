"use client";

import { useMemo } from "react";

import { CombinedChart } from "@/components/portfolio/combined-chart";
import {
  Figure,
  MetricValue,
  NotYetMeasured,
  Panel,
  TradeDate,
} from "@/components/portfolio/detail/primitives";
import {
  BLOCKED_PERFORMANCE,
  bestAndWorst,
  calendarYears,
  drawdownEpisodes,
  flowSummary,
  monthlyReturns,
  returnBasis,
  rollingReturns,
} from "@/lib/portfolio/detail-tabs";
import type { PortfolioSummary } from "@/lib/portfolio/detail-view";
import type { NavRange, NavSeries } from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * The Performance tab: the chart, then the cuts of it a chart cannot answer.
 *
 * The chart itself is `CombinedChart`, which already draws value, return, the rebased benchmark
 * and the drawdown overlay with the range pills. Redrawing it here would be a second chart free to
 * disagree with the first about what a return is, which is the exact confusion its own header
 * comment exists to prevent.
 *
 * ## Every derived series reads the wealth index, never the value line
 *
 * `NavSeriesOut.drawdown[].index` is flow-adjusted; `points[].value` is not. A month in which
 * ₹50,000 was assigned shows as a large gain on the value line and as whatever the market did on
 * the index, and only the second is a return. The monthly grid, the calendar years and the
 * rolling windows all read the index.
 *
 * ## The part-window guard
 *
 * A rolling window longer than the series is reported as having no observations, with the count
 * of sessions there actually are. A "one-year rolling return" measured over four months is not a
 * one-year return and a reader cannot tell from the number. The same guard sits on the compound
 * annual rate, which is refused below a year rather than extrapolated.
 *
 * ## Deposits are not performance, and the separation is done properly
 *
 * The obvious separation is to subtract the net flow from the value change, and that is only
 * right when every flow lands at the start of the window. So the flows panel reports the rupees
 * that moved and points at the index for the return, which does the separation session by
 * session. It says so rather than leaving a reader to assume.
 */

export interface PerformanceTabProps {
  summary: PortfolioSummary;
  nav: NavSeries | null;
  /** The sentence to print instead of a chart when the NAV read failed. */
  unavailableReason: string | null;
  onRangeChange?: ((range: NavRange) => void) | undefined;
  chartLoading?: boolean;
}

export function PerformanceTab({
  summary,
  nav,
  unavailableReason,
  onRangeChange,
  chartLoading = false,
}: PerformanceTabProps) {
  const months = useMemo(() => monthlyReturns(nav), [nav]);
  const years = useMemo(() => calendarYears(nav), [nav]);
  const rolling = useMemo(() => rollingReturns(nav), [nav]);
  const episodes = useMemo(() => drawdownEpisodes(nav), [nav]);
  const extremes = useMemo(() => bestAndWorst(nav), [nav]);
  const flows = useMemo(() => flowSummary(nav), [nav]);
  const basis = useMemo(() => returnBasis(summary, nav), [summary, nav]);

  const strongest = useMemo(
    () => Math.max(1, ...months.map((month) => Math.abs(month.raw ?? 0))),
    [months],
  );

  return (
    <>
      <section aria-label="Value and return over time" className="flex flex-col gap-2">
        {nav ? (
          <CombinedChart
            chart={nav}
            {...(onRangeChange ? { onRangeChange } : {})}
            loading={chartLoading}
          />
        ) : (
          <p
            data-testid="performance-chart-unavailable"
            className="rounded-xl border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground"
          >
            {unavailableReason ??
              "No end-of-day valuations have been recorded for this portfolio yet, so there is nothing to plot."}
          </p>
        )}
      </section>

      <Panel
        title="Three ways to state the return, and what each answers"
        blurb="They are different numbers about the same portfolio, and which one is right depends on the question."
        testId="performance-basis"
      >
        <ul className="divide-y divide-border/60">
          {basis.map((entry) => (
            <li
              key={entry.id}
              data-testid={`performance-basis-${entry.id}`}
              className="flex flex-wrap items-start justify-between gap-x-6 gap-y-1 px-4 py-3"
            >
              <div className="min-w-[18rem] flex-1">
                <p className="text-sm font-medium">{entry.figure.label}</p>
                <p className="mt-0.5 max-w-[72ch] text-xs leading-snug text-muted-foreground">
                  <span className="font-medium text-foreground">How it is worked out:</span>{" "}
                  {entry.how}
                </p>
                <p className="mt-0.5 max-w-[72ch] text-xs leading-snug text-muted-foreground">
                  <span className="font-medium text-foreground">Use it for:</span> {entry.useFor}
                </p>
              </div>
              <div className="shrink-0 text-right">
                <MetricValue metric={entry.figure} kind="percent" signed className="text-lg font-semibold" />
              </div>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel
        title="Money you moved, kept apart from what the market did"
        blurb={flows.note}
        testId="performance-flows"
      >
        <div className="flex flex-wrap divide-x divide-border">
          <div className="min-w-[10rem] flex-1">
            <Figure metric={flows.deposits} />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={flows.withdrawals} />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={flows.net} signed />
          </div>
          <div className="min-w-[10rem] flex-1">
            <Figure metric={flows.valueChange} signed />
          </div>
        </div>
      </Panel>

      <Panel
        title="Month by month"
        blurb="Each cell is the wealth index at that month's last session against the previous month's. The first month of the window is absent on purpose: a part month is not the month."
        testId="performance-heatmap"
      >
        {months.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            The valuation series does not span two month ends yet, so there is no monthly grid to
            draw. It will fill in as the series is built.
          </p>
        ) : (
          <div className="overflow-x-auto px-4 py-3">
            <table className="w-full min-w-[44rem] border-separate border-spacing-0.5 text-xs">
              <caption className="sr-only">
                Monthly returns by calendar year, with the year compounded from the months shown.
              </caption>
              <thead>
                <tr>
                  <th scope="col" className="px-2 py-1 text-left font-medium text-muted-foreground">
                    Year
                  </th>
                  {MONTH_HEADINGS.map((heading) => (
                    <th
                      key={heading}
                      scope="col"
                      className="px-1 py-1 text-center font-medium text-muted-foreground"
                    >
                      {heading}
                    </th>
                  ))}
                  <th scope="col" className="px-2 py-1 text-right font-medium text-muted-foreground">
                    Year
                  </th>
                </tr>
              </thead>
              <tbody>
                {years.map((year) => (
                  <tr key={year.year} data-testid={`performance-year-${year.year}`}>
                    <th scope="row" className="px-2 py-1 text-left font-medium tabular-nums">
                      {year.year}
                      {year.partial ? (
                        <span className="ml-1 font-normal text-muted-foreground">part</span>
                      ) : null}
                    </th>
                    {MONTH_NUMBERS.map((monthNumber) => {
                      const cell = year.months.find((month) => month.month === monthNumber);
                      if (cell === undefined) {
                        return (
                          <td
                            key={monthNumber}
                            className="px-1 py-1 text-center text-[0.625rem] text-muted-foreground"
                            title="The valuation series does not cover this month."
                          >
                            <span aria-hidden="true">no data</span>
                            <span className="sr-only">
                              The valuation series does not cover this month.
                            </span>
                          </td>
                        );
                      }
                      return (
                        <td
                          key={monthNumber}
                          data-testid={`performance-month-${cell.key}`}
                          className="px-1 py-1 text-center"
                        >
                          <span
                            title={`${cell.label}: ${cell.figure.value === null ? cell.figure.unavailable : `${cell.figure.value}%`}`}
                            className={cn(
                              "block rounded px-1 py-1 tabular-nums",
                              shadeFor(cell.raw, strongest),
                            )}
                          >
                            <MetricValue
                              metric={cell.figure}
                              kind="percent"
                              signed
                              compact={cell.figure.value === null}
                              short="no close"
                            />
                          </span>
                        </td>
                      );
                    })}
                    <td className="px-2 py-1 text-right font-semibold">
                      <MetricValue
                        metric={year.figure}
                        kind="percent"
                        signed
                        compact={year.figure.value === null}
                        short="no months"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="border-t border-border px-4 py-2.5 text-xs leading-snug text-muted-foreground">
          Colour shows size against the strongest month in the window, and the number is in every
          cell, so the grid is readable without it. A year the window does not fully cover is
          marked and covers only the months shown.
        </p>
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Best and worst"
          blurb="The single days and the whole months at each end of the window."
          testId="performance-extremes"
        >
          <div className="flex flex-wrap divide-x divide-border">
            <div className="min-w-[9rem] flex-1">
              <Figure metric={extremes.bestDay} kind="percent" signed />
            </div>
            <div className="min-w-[9rem] flex-1">
              <Figure metric={extremes.worstDay} kind="percent" signed />
            </div>
          </div>
          <div className="flex flex-wrap divide-x divide-border border-t border-border">
            <div className="min-w-[9rem] flex-1">
              <Figure metric={extremes.bestMonth} kind="percent" signed />
            </div>
            <div className="min-w-[9rem] flex-1">
              <Figure metric={extremes.worstMonth} kind="percent" signed />
            </div>
          </div>
        </Panel>

        <Panel
          title="Rolling returns"
          blurb="What a holder who started on any day in the window would have had after each stretch. A window longer than the series is stated as such rather than measured over whatever is there."
          testId="performance-rolling"
        >
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-xs text-muted-foreground">
                <th scope="col" className="px-4 py-2 text-left font-medium">
                  Stretch
                </th>
                <th scope="col" className="px-3 py-2 text-right font-medium">
                  Latest
                </th>
                <th scope="col" className="px-3 py-2 text-right font-medium">
                  Best
                </th>
                <th scope="col" className="px-3 py-2 text-right font-medium">
                  Worst
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  Windows
                </th>
              </tr>
            </thead>
            <tbody>
              {rolling.map((window) => (
                <tr
                  key={window.id}
                  data-testid={`performance-rolling-${window.id}`}
                  className="border-b border-border/50 last:border-0"
                >
                  <th scope="row" className="px-4 py-2 text-left text-sm font-normal">
                    {window.label}
                    <span className="block text-xs text-muted-foreground">
                      {window.sessions} sessions
                    </span>
                  </th>
                  <td className="px-3 py-2 text-right">
                    <MetricValue
                      metric={window.latest}
                      kind="percent"
                      signed
                      compact={window.latest.value === null}
                      short="series too short"
                    />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <MetricValue
                      metric={window.best}
                      kind="percent"
                      signed
                      compact={window.best.value === null}
                      short="series too short"
                    />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <MetricValue
                      metric={window.worst}
                      kind="percent"
                      signed
                      compact={window.worst.value === null}
                      short="series too short"
                    />
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-muted-foreground">
                    {window.observations}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <Panel
        title="Every fall, and whether it came back"
        blurb="Peak to trough episodes in this window, deepest first. One that has not recovered is marked ongoing rather than closed at the last session."
        testId="performance-drawdowns"
      >
        {episodes.length === 0 ? (
          <p className="px-4 py-6 text-sm text-muted-foreground">
            This portfolio has not been below its own high water mark at any point in the window we
            have, so there is no fall to describe.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    From its high on
                  </th>
                  <th scope="col" className="px-3 py-2 text-left font-medium">
                    Bottomed on
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Depth
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Days down
                  </th>
                  <th scope="col" className="px-4 py-2 text-left font-medium">
                    Back to the high
                  </th>
                </tr>
              </thead>
              <tbody>
                {episodes.map((episode) => (
                  <tr
                    key={episode.key}
                    data-testid={`performance-drawdown-${episode.key}`}
                    className="border-b border-border/50 last:border-0"
                  >
                    <td className="px-4 py-2">
                      {episode.peakOn === null ? (
                        <span className="text-xs text-muted-foreground">
                          a high set before this window
                        </span>
                      ) : (
                        <TradeDate iso={episode.peakOn} />
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <TradeDate iso={episode.troughOn} />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <MetricValue
                        metric={episode.depth}
                        kind="percent"
                        signed
                        compact={episode.depth.value === null}
                        short="unreadable"
                      />
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {episode.toTrough === null ? (
                        <span className="text-xs text-muted-foreground">
                          not countable, the high is outside this window
                        </span>
                      ) : (
                        episode.toTrough
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs">
                      {episode.recoveredOn === null ? (
                        <span className="text-warning">Still below it as of the last session</span>
                      ) : (
                        <>
                          <TradeDate iso={episode.recoveredOn} />
                          {episode.toRecovery === null ? null : (
                            <span className="ml-1 text-muted-foreground">
                              after {episode.toRecovery} days
                            </span>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <NotYetMeasured
        items={BLOCKED_PERFORMANCE}
        heading="What this tab cannot plot yet"
        intro="Three of the brief's performance asks need data Baskfy does not keep. Each names what it would take."
        testId="performance-blocked"
      />
    </>
  );
}

const MONTH_HEADINGS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

const MONTH_NUMBERS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] as const;

/**
 * Shade by size against the strongest month in the window, never by an absolute scale.
 *
 * An absolute scale makes a quiet year look like a blank grid and a volatile one look like a
 * warning. The number is in every cell regardless, so the colour is a second reading of the same
 * fact rather than the only one.
 */
function shadeFor(value: number | null, strongest: number): string {
  if (value === null || value === 0) return "bg-muted/40 text-muted-foreground";
  const share = Math.min(1, Math.abs(value) / strongest);
  const step = share > 0.66 ? 3 : share > 0.33 ? 2 : 1;
  if (value > 0) {
    return step === 3
      ? "bg-positive/30 text-positive"
      : step === 2
        ? "bg-positive/20 text-positive"
        : "bg-positive/10 text-positive";
  }
  return step === 3
    ? "bg-negative/30 text-negative"
    : step === 2
      ? "bg-negative/20 text-negative"
      : "bg-negative/10 text-negative";
}
