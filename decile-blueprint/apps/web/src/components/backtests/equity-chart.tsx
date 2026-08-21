"use client";

import type { EquityPointOut } from "@decile/api-client";
import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId, useMemo } from "react";

import { formatTradeDate } from "@/lib/format";

/**
 * docs/08 §Backtests: "results page: equity curve vs benchmark".
 *
 * Both series are **rebased to 100** at the first day. The portfolio is measured in rupees and the
 * benchmark in index points; drawing them against one axis would put the strategy on the floor
 * next to a five-digit index level, and drawing them against two axes would let the eye compare
 * two arbitrary scalings. Rebasing is the one presentation under which "did it beat the index?" is
 * answered by which line is higher.
 *
 * visx supplies the scale and the path (docs/02 locks it for charts). The axes are plain SVG, as
 * in `market/breadth-history` — `@visx/axis` would be another package for four tick labels.
 */
export interface EquityChartProps {
  points: readonly EquityPointOut[];
  benchmarkLabel: string;
  height?: number;
}

const WIDTH = 900;
const DEFAULT_HEIGHT = 260;
const MARGIN = { top: 12, right: 16, bottom: 24, left: 52 };
const MIN_POINTS = 2;
const BASE = 100;

interface Sample {
  index: number;
  date: string;
  equity: number;
  benchmark: number | null;
}

function rebase(points: readonly EquityPointOut[]): Sample[] {
  const first = points[0];
  if (!first) return [];
  const equityBase = Number(first.equity);
  const benchmarkBase = first.benchmark === null || first.benchmark === undefined
    ? null
    : Number(first.benchmark);
  return points.map((point, index) => ({
    index,
    date: point.date,
    equity: equityBase > 0 ? (Number(point.equity) / equityBase) * BASE : BASE,
    benchmark:
      benchmarkBase && benchmarkBase > 0 && point.benchmark !== null && point.benchmark !== undefined
        ? (Number(point.benchmark) / benchmarkBase) * BASE
        : null,
  }));
}

export function EquityChart({ points, benchmarkLabel, height = DEFAULT_HEIGHT }: EquityChartProps) {
  const titleId = useId();
  const samples = useMemo(() => rebase(points), [points]);
  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = height - MARGIN.top - MARGIN.bottom;

  if (samples.length < MIN_POINTS) {
    return (
      <figure className="rounded-lg border border-border bg-card p-4">
        <figcaption className="text-sm font-medium">Equity curve</figcaption>
        <p className="py-10 text-center text-sm text-muted-foreground">
          A line needs two points. This run covers {samples.length} day.
        </p>
      </figure>
    );
  }

  const values = samples.flatMap((sample) =>
    sample.benchmark === null ? [sample.equity] : [sample.equity, sample.benchmark],
  );
  const xScale = scaleLinear<number>({ domain: [0, samples.length - 1], range: [0, innerWidth] });
  const yScale = scaleLinear<number>({
    domain: [Math.min(...values), Math.max(...values)],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(4);
  const last = samples[samples.length - 1];
  const withBenchmark = samples.filter((sample) => sample.benchmark !== null);

  return (
    <figure className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="text-sm font-medium">Equity curve, rebased to 100</span>
        <span className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-accent" />
            Strategy
          </span>
          <span className="flex items-center gap-1">
            <span
              aria-hidden="true"
              className="inline-block h-0.5 w-4 border-t border-dashed border-muted-foreground"
            />
            {benchmarkLabel}
          </span>
        </span>
      </figcaption>

      <svg viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-labelledby={titleId} className="w-full">
        <title id={titleId}>
          {`Strategy equity from ${formatTradeDate(samples[0]?.date)} to ${formatTradeDate(last?.date)}, ending at ${(last?.equity ?? BASE).toFixed(1)} against ${benchmarkLabel} at ${(last?.benchmark ?? BASE).toFixed(1)}, both rebased to 100.`}
        </title>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {ticks.map((tick) => (
            <g key={tick} transform={`translate(0,${yScale(tick)})`}>
              <line x1={0} x2={innerWidth} className="stroke-border" strokeWidth={0.5} />
              <text x={-8} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[10px]">
                {tick}
              </text>
            </g>
          ))}
          {withBenchmark.length >= MIN_POINTS ? (
            <LinePath<Sample>
              data={withBenchmark}
              x={(sample) => xScale(sample.index)}
              y={(sample) => yScale(sample.benchmark ?? BASE)}
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
            y={(sample) => yScale(sample.equity)}
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
            {formatTradeDate(samples[0]?.date)}
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
  );
}
