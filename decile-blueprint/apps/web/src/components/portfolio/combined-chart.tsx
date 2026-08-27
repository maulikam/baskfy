"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { AreaClosed, LinePath } from "@visx/shape";
import { useId, useMemo, useState } from "react";

import { useAmounts } from "@/components/portfolio/amounts";
import { ReturnValue } from "@/components/portfolio/return-value";
import { formatNumber, formatTradeDate } from "@/lib/format";
import {
  formatRate,
  fromFigure,
  fromRate,
  type NavRange,
  type NavSeries,
} from "@/lib/portfolio/overview";
import { cn } from "@/lib/utils";

/**
 * §6.3 — the combined chart over the EOD NAV series.
 *
 * ## Why this is a new component and not `explore/performance-chart`
 *
 * Both draw a line with visx, and visx is what §6.3 gets: `@visx/curve`, `@visx/scale` and
 * `@visx/shape` are already the locked chart stack (`docs/02`), the backtest equity curve and the
 * breadth history are built from them, and §6.3 needs no primitive they do not have. Nothing new
 * is installed.
 *
 * What could not be reused is the *controls*. `PerformanceChart` offers 1M/1Y/3Y/5Y/MAX with a
 * SIP toggle; §6.3 asks for **1M/3M/1Y/3Y/All** and says plainly that 1D and 1W wait for intraday
 * (§5.1). Bending one component to serve both would have put a 1D pill one prop away from a
 * screen that has no intraday data to draw it from.
 *
 * ## The three toggles, and what each series actually is
 *
 * * **Value (₹)** plots `chart.points[].value` — the official end-of-day mark, cash included.
 * * **Return (%)** plots the **wealth index** carried on `chart.drawdown[].index`, not the change
 *   in value. Those are different numbers whenever money moved: assigning ₹50,000 to a portfolio
 *   raises its value by ₹50,000 and its return by nothing (§4.4). Deriving a percentage from the
 *   value line would have credited the user's own deposit to the strategy, which is exactly the
 *   confusion §5.2 separates XIRR from TWR to avoid.
 * * **Benchmark** is rebased so the two lines start together — in return mode both start at 0%,
 *   in value mode the index is scaled to the portfolio's opening value. An index level and a
 *   rupee balance share no axis, and two axes would let the eye compare two arbitrary scalings;
 *   rebasing is the one presentation under which "did I beat it?" is answered by which line is
 *   higher. The caption says so rather than leaving it implied.
 * * **Drawdown** hangs as its own small area below, from zero, y-axis inverted — the same shape
 *   and the same reasoning as `backtests/drawdown-chart`.
 *
 * Nothing here is drawn from a stub. With fewer than two marks there is no line to draw, and the
 * panel says which day it has rather than inventing a second one.
 */

const RANGES: readonly { key: NavRange; label: string }[] = [
  { key: "1M", label: "1M" },
  { key: "3M", label: "3M" },
  { key: "1Y", label: "1Y" },
  { key: "3Y", label: "3Y" },
  { key: "ALL", label: "All" },
];

const WIDTH = 900;
const HEIGHT = 280;
const DRAWDOWN_HEIGHT = 130;
const MARGIN = { top: 12, right: 16, bottom: 26, left: 60 };
const MIN_POINTS = 2;
const BASE = 100;

type ChartMode = "VALUE" | "RETURN";

interface Sample {
  index: number;
  date: string;
  /** Portfolio series in the active mode's units: rupees, or percent from the start. */
  portfolio: number;
  /** Benchmark, rebased into the same units. `null` where the index has no print. */
  benchmark: number | null;
}

interface DrawdownSample {
  index: number;
  date: string;
  /** Percent below the high-water mark, negative. */
  drawdown: number;
}

/** The portfolio series in the active mode, plus the benchmark rebased onto it. */
export function samplesFor(chart: NavSeries, mode: ChartMode): Sample[] {
  const wealth = chart.drawdown ?? [];
  const value = chart.points ?? [];
  const benchmarkPoints = chart.benchmark?.points ?? [];
  const benchmarkByDate = new Map(benchmarkPoints.map((point) => [point.on, Number(point.value)]));
  const benchmarkBase = benchmarkPoints[0] ? Number(benchmarkPoints[0].value) : null;

  if (mode === "RETURN") {
    const first = wealth[0] ? Number(wealth[0].index) : null;
    if (first === null || first === 0) return [];
    return wealth.map((point, index) => {
      const level = benchmarkByDate.get(point.on);
      return {
        index,
        date: point.on,
        portfolio: (Number(point.index) / first - 1) * BASE,
        benchmark:
          level === undefined || benchmarkBase === null || benchmarkBase === 0
            ? null
            : (level / benchmarkBase - 1) * BASE,
      };
    });
  }

  const openingValue = value[0] ? Number(value[0].value) : null;
  return value.map((point, index) => {
    const level = benchmarkByDate.get(point.on);
    return {
      index,
      date: point.on,
      portfolio: Number(point.value),
      benchmark:
        level === undefined || benchmarkBase === null || benchmarkBase === 0 || openingValue === null
          ? null
          : (level / benchmarkBase) * openingValue,
    };
  });
}

function drawdownSamples(chart: NavSeries): DrawdownSample[] {
  return (chart.drawdown ?? []).map((point, index) => ({
    index,
    date: point.on,
    drawdown: Number(point.drawdown) * BASE,
  }));
}

export interface CombinedChartProps {
  chart: NavSeries;
  /** Called when the reader picks a range. Omitted while the payload carries one range only. */
  onRangeChange?: (range: NavRange) => void;
  /** True while a new range is being fetched — the drawn series is the previous one. */
  loading?: boolean;
}

export function CombinedChart({ chart, onRangeChange, loading = false }: CombinedChartProps) {
  const titleId = useId();
  const drawdownTitleId = useId();
  const [mode, setMode] = useState<ChartMode>("VALUE");
  const [showBenchmark, setShowBenchmark] = useState(true);
  const [showDrawdown, setShowDrawdown] = useState(false);
  const { visible: amountsVisible } = useAmounts();

  const samples = useMemo(() => samplesFor(chart, mode), [chart, mode]);
  const falls = useMemo(() => drawdownSamples(chart), [chart]);
  const benchmarkName = chart.benchmark?.name ?? null;
  const withBenchmark = samples.filter((sample) => sample.benchmark !== null);
  const benchmarkDrawn = showBenchmark && withBenchmark.length >= MIN_POINTS;

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom;
  const values = samples.flatMap((sample) =>
    benchmarkDrawn && sample.benchmark !== null
      ? [sample.portfolio, sample.benchmark]
      : [sample.portfolio],
  );
  const xScale = scaleLinear<number>({
    domain: [0, Math.max(samples.length - 1, 1)],
    range: [0, innerWidth],
  });
  const yScale = scaleLinear<number>({
    domain: values.length > 0 ? [Math.min(...values), Math.max(...values)] : [0, 1],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(4);
  const first = samples[0];
  const last = samples[samples.length - 1];

  const axisLabel = (tick: number): string =>
    mode === "RETURN"
      ? `${formatNumber(tick, { decimals: 0, signed: true })}%`
      : amountsVisible
        ? `₹${formatNumber(tick, { decimals: 0 })}`
        : "•••";

  return (
    <section
      aria-label="Combined performance"
      data-testid="combined-chart"
      className="space-y-3 rounded-xl border border-border/70 bg-card p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">Everything you hold, day by day</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">
            One mark per trading day, at the official close. There is no intraday line yet.
          </p>
        </div>
        <div role="group" aria-label="Chart range" className="flex flex-wrap gap-1">
          {RANGES.map((range) => (
            <button
              key={range.key}
              type="button"
              aria-pressed={chart.range === range.key}
              disabled={onRangeChange === undefined || loading}
              onClick={() => onRangeChange?.(range.key)}
              data-testid={`chart-range-${range.key}`}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
                chart.range === range.key
                  ? "border-accent bg-accent-muted text-accent"
                  : "border-border/70 bg-background text-muted-foreground hover:text-foreground",
                (onRangeChange === undefined || loading) && "cursor-not-allowed opacity-60",
              )}
            >
              {range.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-xs">
        <div role="group" aria-label="Chart units" className="flex gap-1">
          <button
            type="button"
            aria-pressed={mode === "VALUE"}
            onClick={() => setMode("VALUE")}
            data-testid="chart-mode-value"
            className={cn(
              "rounded-md border px-2.5 py-1 font-medium",
              mode === "VALUE"
                ? "border-accent bg-accent-muted text-accent"
                : "border-border/70 text-muted-foreground",
            )}
          >
            Value
          </button>
          <button
            type="button"
            aria-pressed={mode === "RETURN"}
            onClick={() => setMode("RETURN")}
            data-testid="chart-mode-return"
            className={cn(
              "rounded-md border px-2.5 py-1 font-medium",
              mode === "RETURN"
                ? "border-accent bg-accent-muted text-accent"
                : "border-border/70 text-muted-foreground",
            )}
          >
            Return
          </button>
        </div>
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={showBenchmark}
            onChange={(event) => setShowBenchmark(event.target.checked)}
            data-testid="chart-benchmark-toggle"
            className="size-3.5 accent-[var(--accent)]"
          />
          <span>Compare with {benchmarkName ?? "the market"}</span>
        </label>
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={showDrawdown}
            onChange={(event) => setShowDrawdown(event.target.checked)}
            data-testid="chart-drawdown-toggle"
            className="size-3.5 accent-[var(--accent)]"
          />
          <span>Show falls from the peak</span>
        </label>
      </div>

      <div className="flex flex-wrap items-end gap-6">
        <ReturnValue
          entry={fromRate(chart.total_return)}
          showReason
          data-testid="chart-total-return"
        />
        {chart.benchmark ? (
          <ReturnValue
            entry={fromFigure(chart.benchmark.benchmark)}
            data-testid="chart-benchmark-return"
          />
        ) : null}
        {chart.max_drawdown ? (
          <div title={`Peak ${formatTradeDate(chart.max_drawdown.peak_on)}, trough ${formatTradeDate(chart.max_drawdown.trough_on)}`}>
            <p className="text-sm tabular-nums text-negative">
              {formatRate(chart.max_drawdown.drawdown)}
            </p>
            <p className="text-[11px] leading-tight text-muted-foreground">Deepest fall</p>
          </div>
        ) : null}
      </div>

      {samples.length < MIN_POINTS ? (
        <p
          data-testid="chart-empty"
          className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground"
        >
          {samples.length === 0
            ? "No end-of-day valuations have been recorded yet, so there is nothing to plot."
            : `A line needs two marks. This range covers ${formatTradeDate(first?.date)} only.`}
        </p>
      ) : (
        <figure className="flex flex-col gap-2">
          <figcaption className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-accent" />
              {mode === "RETURN" ? "Your return" : "Your value"}
            </span>
            {benchmarkDrawn ? (
              <span className="flex items-center gap-1">
                <span
                  aria-hidden="true"
                  className="inline-block h-0.5 w-4 border-t border-dashed border-muted-foreground"
                />
                {benchmarkName ?? "Benchmark"}
                {mode === "RETURN" ? " (both from 0%)" : " (rebased to your opening value)"}
              </span>
            ) : null}
            {loading ? <span>Loading a new range…</span> : null}
          </figcaption>

          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            role="img"
            aria-labelledby={titleId}
            className="w-full"
          >
            <title id={titleId}>
              {`${mode === "RETURN" ? "Return" : "Value"} from ${formatTradeDate(first?.date)} to ${formatTradeDate(last?.date)}${
                benchmarkDrawn ? `, against ${benchmarkName ?? "the benchmark"}` : ""
              }.`}
            </title>
            <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
              {ticks.map((tick) => (
                <g key={tick} transform={`translate(0,${yScale(tick)})`}>
                  <line x1={0} x2={innerWidth} className="stroke-border" strokeWidth={0.5} />
                  <text
                    x={-8}
                    dy="0.32em"
                    textAnchor="end"
                    className="fill-muted-foreground text-[10px]"
                  >
                    {axisLabel(tick)}
                  </text>
                </g>
              ))}
              {benchmarkDrawn ? (
                <LinePath<Sample>
                  data={withBenchmark}
                  x={(sample) => xScale(sample.index)}
                  y={(sample) => yScale(sample.benchmark ?? 0)}
                  curve={curveMonotoneX}
                  className="stroke-muted-foreground"
                  strokeWidth={1.25}
                  strokeDasharray="4 3"
                  fill="none"
                />
              ) : null}
              <LinePath<Sample>
                data={samples}
                x={(sample) => xScale(sample.index)}
                y={(sample) => yScale(sample.portfolio)}
                curve={curveMonotoneX}
                className="stroke-accent"
                strokeWidth={1.75}
                fill="none"
              />
              <text
                x={0}
                y={innerHeight + 16}
                className="fill-muted-foreground text-[10px]"
                textAnchor="start"
              >
                {formatTradeDate(first?.date)}
              </text>
              <text
                x={innerWidth}
                y={innerHeight + 16}
                className="fill-muted-foreground text-[10px]"
                textAnchor="end"
              >
                {formatTradeDate(last?.date)}
              </text>
            </g>
          </svg>
        </figure>
      )}

      {showDrawdown ? (
        falls.length < MIN_POINTS ? (
          <p data-testid="drawdown-empty" className="text-xs text-muted-foreground">
            The falls-from-peak series needs two marks and this range has {falls.length}.
          </p>
        ) : (
          <figure data-testid="drawdown-panel" className="flex flex-col gap-1">
            <figcaption className="text-xs text-muted-foreground">
              How far below its own peak the portfolio sat, each day.
            </figcaption>
            <svg
              viewBox={`0 0 ${WIDTH} ${DRAWDOWN_HEIGHT}`}
              role="img"
              aria-labelledby={drawdownTitleId}
              className="w-full"
            >
              <title id={drawdownTitleId}>
                {`Falls from the peak, ${formatTradeDate(falls[0]?.date)} to ${formatTradeDate(
                  falls[falls.length - 1]?.date,
                )}.`}
              </title>
              <DrawdownArea samples={falls} />
            </svg>
          </figure>
        )
      ) : null}
    </section>
  );
}

/** The area hanging from zero. Split out so the main component stays readable. */
function DrawdownArea({ samples }: { samples: readonly DrawdownSample[] }) {
  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = DRAWDOWN_HEIGHT - MARGIN.top - MARGIN.bottom;
  const worst = Math.min(...samples.map((sample) => sample.drawdown));
  const xScale = scaleLinear<number>({
    domain: [0, Math.max(samples.length - 1, 1)],
    range: [0, innerWidth],
  });
  const yScale = scaleLinear<number>({
    domain: [Math.min(worst, -1), 0],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(3);

  return (
    <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
      {ticks.map((tick) => (
        <g key={tick} transform={`translate(0,${yScale(tick)})`}>
          <line x1={0} x2={innerWidth} className="stroke-border" strokeWidth={0.5} />
          <text x={-8} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[10px]">
            {`${tick.toFixed(0)}%`}
          </text>
        </g>
      ))}
      <AreaClosed<DrawdownSample>
        data={[...samples]}
        x={(sample) => xScale(sample.index)}
        y={(sample) => yScale(sample.drawdown)}
        yScale={yScale}
        curve={curveMonotoneX}
        className="fill-negative/25 stroke-negative"
        strokeWidth={1}
      />
    </g>
  );
}
