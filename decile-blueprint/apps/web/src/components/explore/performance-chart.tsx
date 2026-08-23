"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId, useMemo, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Basket detail performance chart — docs/smallcase/04 §4 / 05-ui-spec.
 *
 * Range pills, SIP mode, and benchmark compare are real controls. Series are passed in
 * as props (catalog/API later); stubs keep the SVG readable when history is thin.
 * visx matches the backtest equity chart stack (docs/02).
 */

export type ChartRange = "1M" | "1Y" | "3Y" | "5Y" | "MAX";

export interface PerformancePoint {
  /** ISO date (YYYY-MM-DD). */
  date: string;
  /** Basket NAV / SIP portfolio value, already in chart units (rebased or ₹). */
  basket: number;
  /** Benchmark level in the same units; omit or null when unavailable. */
  benchmark?: number | null;
  /** Cumulative money put in under SIP mode (optional second series). */
  invested?: number | null;
}

export interface PerformanceChartProps {
  series: readonly PerformancePoint[];
  /** Label for the basket line (defaults to "Basket"). */
  basketLabel?: string;
  /** Label for the benchmark overlay (defaults to "Benchmark"). */
  benchmarkLabel?: string;
  /** Initial range pill; UI is controlled locally until URL wiring lands. */
  defaultRange?: ChartRange;
  /** Show the history-caveat note under the chart when coverage is incomplete. */
  incompleteHistory?: boolean;
  height?: number;
  className?: string;
}

const RANGES: readonly ChartRange[] = ["1M", "1Y", "3Y", "5Y", "MAX"];
const WIDTH = 900;
const DEFAULT_HEIGHT = 280;
const MARGIN = { top: 12, right: 16, bottom: 28, left: 48 };
const MIN_POINTS = 2;

interface Sample {
  index: number;
  date: string;
  basket: number;
  benchmark: number | null;
  invested: number | null;
}

function toSamples(points: readonly PerformancePoint[]): Sample[] {
  return points.map((point, index) => ({
    index,
    date: point.date,
    basket: point.basket,
    benchmark:
      point.benchmark === null || point.benchmark === undefined ? null : point.benchmark,
    invested:
      point.invested === null || point.invested === undefined ? null : point.invested,
  }));
}

/** Stub series for empty API — still draws so controls/labels are reviewable. */
export function stubPerformanceSeries(range: ChartRange): PerformancePoint[] {
  const lengths: Record<ChartRange, number> = {
    "1M": 22,
    "1Y": 52,
    "3Y": 36,
    "5Y": 60,
    MAX: 80,
  };
  const n = lengths[range];
  const start = new Date(Date.UTC(2024, 0, 2));
  const out: PerformancePoint[] = [];
  for (let i = 0; i < n; i += 1) {
    const d = new Date(start);
    d.setUTCDate(start.getUTCDate() + i * (range === "1M" ? 1 : 7));
    const t = i / Math.max(n - 1, 1);
    const basket = 100 * (1 + 0.18 * t + 0.02 * Math.sin(i / 3));
    const benchmark = 100 * (1 + 0.12 * t + 0.015 * Math.sin(i / 4));
    const invested = 100 + t * 40;
    out.push({
      date: d.toISOString().slice(0, 10),
      basket,
      benchmark,
      invested,
    });
  }
  return out;
}

export function PerformanceChart({
  series,
  basketLabel = "Basket",
  benchmarkLabel = "Benchmark",
  defaultRange = "1Y",
  incompleteHistory = false,
  height = DEFAULT_HEIGHT,
  className,
}: PerformanceChartProps) {
  const titleId = useId();
  const [range, setRange] = useState<ChartRange>(defaultRange);
  const [sipMode, setSipMode] = useState(false);
  const [compareBenchmark, setCompareBenchmark] = useState(true);

  const activeSeries = series.length >= MIN_POINTS ? series : stubPerformanceSeries(range);
  const samples = useMemo(() => toSamples(activeSeries), [activeSeries]);
  const usingStub = series.length < MIN_POINTS;

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = height - MARGIN.top - MARGIN.bottom;

  const values = samples.flatMap((sample) => {
    const list = [sample.basket];
    if (compareBenchmark && sample.benchmark !== null) list.push(sample.benchmark);
    if (sipMode && sample.invested !== null) list.push(sample.invested);
    return list;
  });
  const yMin = Math.min(...values);
  const yMax = Math.max(...values);
  const xScale = scaleLinear<number>({
    domain: [0, Math.max(samples.length - 1, 1)],
    range: [0, innerWidth],
  });
  const yScale = scaleLinear<number>({
    domain: [yMin, yMax],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(4);
  const last = samples[samples.length - 1];
  const withBenchmark = samples.filter((sample) => sample.benchmark !== null);
  const withInvested = samples.filter((sample) => sample.invested !== null);

  const modeLabel = sipMode ? "SIP value vs money put in" : "Absolute return (rebased)";

  return (
    <section
      aria-label="Performance chart"
      className={cn("space-y-3 rounded-xl border border-border/70 bg-card p-4", className)}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Performance</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">{modeLabel}</p>
        </div>
        <div
          role="group"
          aria-label="Chart range"
          className="flex flex-wrap gap-1"
        >
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={range === r}
              onClick={() => setRange(r)}
              className={cn(
                "rounded-md border px-2.5 py-1 text-xs font-medium transition-colors",
                range === r
                  ? "border-accent bg-accent-muted text-accent"
                  : "border-border/70 bg-background text-muted-foreground hover:text-foreground",
              )}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap gap-4 text-xs">
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={sipMode}
            onChange={(event) => setSipMode(event.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          <span>SIP mode</span>
        </label>
        <label className="inline-flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={compareBenchmark}
            onChange={(event) => setCompareBenchmark(event.target.checked)}
            className="size-3.5 accent-[var(--accent)]"
          />
          <span>Compare benchmark</span>
        </label>
      </div>

      <figure className="flex flex-col gap-2">
        <figcaption className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-accent" />
            {basketLabel}
          </span>
          {sipMode ? (
            <span className="flex items-center gap-1">
              <span
                aria-hidden="true"
                className="inline-block h-0.5 w-4 border-t border-dotted border-muted-foreground"
              />
              Money put in
            </span>
          ) : null}
          {compareBenchmark ? (
            <span className="flex items-center gap-1">
              <span
                aria-hidden="true"
                className="inline-block h-0.5 w-4 border-t border-dashed border-muted-foreground"
              />
              {benchmarkLabel}
            </span>
          ) : null}
          {usingStub ? (
            <span className="text-muted-foreground/80">Illustrative series · range {range}</span>
          ) : null}
        </figcaption>

        <svg
          viewBox={`0 0 ${WIDTH} ${height}`}
          role="img"
          aria-labelledby={titleId}
          className="w-full"
        >
          <title id={titleId}>
            {`${basketLabel} performance, range ${range}${sipMode ? ", SIP mode" : ""}${
              compareBenchmark ? `, vs ${benchmarkLabel}` : ""
            }. From ${samples[0]?.date ?? "—"} to ${last?.date ?? "—"}.`}
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
                  {tick.toFixed(0)}
                </text>
              </g>
            ))}
            {compareBenchmark && withBenchmark.length >= MIN_POINTS ? (
              <LinePath<Sample>
                data={withBenchmark}
                x={(sample) => xScale(sample.index) ?? 0}
                y={(sample) => yScale(sample.benchmark ?? 0) ?? 0}
                curve={curveMonotoneX}
                className="stroke-muted-foreground"
                strokeWidth={1.25}
                strokeDasharray="4 3"
                fill="none"
              />
            ) : null}
            {sipMode && withInvested.length >= MIN_POINTS ? (
              <LinePath<Sample>
                data={withInvested}
                x={(sample) => xScale(sample.index) ?? 0}
                y={(sample) => yScale(sample.invested ?? 0) ?? 0}
                curve={curveMonotoneX}
                className="stroke-muted-foreground"
                strokeWidth={1.25}
                strokeDasharray="1 4"
                fill="none"
              />
            ) : null}
            <LinePath<Sample>
              data={samples}
              x={(sample) => xScale(sample.index) ?? 0}
              y={(sample) => yScale(sample.basket) ?? 0}
              curve={curveMonotoneX}
              className="stroke-accent"
              strokeWidth={1.75}
              fill="none"
            />
            <text
              x={0}
              y={innerHeight + 18}
              className="fill-muted-foreground text-[10px]"
              textAnchor="start"
            >
              {samples[0]?.date ?? ""}
            </text>
            <text
              x={innerWidth}
              y={innerHeight + 18}
              className="fill-muted-foreground text-[10px]"
              textAnchor="end"
            >
              {last?.date ?? ""}
            </text>
          </g>
        </svg>
      </figure>

      {incompleteHistory ? (
        <p className="text-xs text-muted-foreground">
          Price coverage is incomplete over this range — treat the segment as indicative.
        </p>
      ) : null}
    </section>
  );
}
