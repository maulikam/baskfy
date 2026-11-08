"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId, useMemo } from "react";

import type { MarketHealthPointOut } from "@baskfy/api-client";

import { formatNumber, formatTradeDate } from "@/lib/format";

/**
 * docs/08 §"Market Health", the part the reference product does not have:
 *
 *     "Add what the reference lacks: a **history chart** of each breadth series with the Nifty
 *      overlaid, which is the actual analytical use of breadth data."
 *
 * The overlay is the *selected* universe's own index level, not always NIFTY 50 — comparing
 * NIFTY MICROCAP 250's breadth against the large-cap index would be the wrong denominator, and
 * every universe in the selector is itself an index with a snapshot row. `docs/11a` §4.
 *
 * Two y-scales, because the two series have nothing to do with each other numerically: breadth is
 * a percentage bounded at 0–100 and the index level is an unbounded number in the thousands.
 * Drawing them against one axis would flatten the breadth line into the baseline. The axes are
 * labelled and colour-matched to their series, and the tabular readout below carries every value
 * as text — docs/11 §Accessibility, colour is never the only carrier.
 *
 * Axes are drawn as plain SVG lines and `<text>`. visx supplies the scales and the path (docs/02
 * locks it for charts); `@visx/axis` would be another package for two tick labels.
 */
export interface BreadthHistoryProps {
  points: readonly MarketHealthPointOut[];
  series: { key: BreadthKey; label: string };
  universeName: string;
}

export type BreadthKey =
  | "pct_above_200dma"
  | "pct_above_50dma"
  | "pct_within_10pct_ath"
  | "pct_ret_1y_positive";

const WIDTH = 560;
const HEIGHT = 180;
const MARGIN = { top: 12, right: 52, bottom: 22, left: 40 };
const INNER_WIDTH = WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = HEIGHT - MARGIN.top - MARGIN.bottom;

/** A line needs two points. One day of history is a number, not a series. */
const MIN_POINTS = 2;

const BREADTH_TICKS = [0, 25, 50, 75, 100];

interface Sample {
  index: number;
  date: string;
  breadth: number | null;
  level: number | null;
}

function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const numeric = typeof value === "string" ? Number(value) : value;
  return Number.isNaN(numeric) ? null : numeric;
}

function samplesOf(points: readonly MarketHealthPointOut[], key: BreadthKey): Sample[] {
  return points.map((point, index) => ({
    index,
    date: point.date,
    breadth: toNumber(point[key]),
    level: toNumber(point.index_level),
  }));
}

export function BreadthHistory({ points, series, universeName }: BreadthHistoryProps) {
  const titleId = useId();
  const samples = useMemo(() => samplesOf(points, series.key), [points, series.key]);
  const drawable = samples.filter((sample) => sample.breadth !== null);
  const levels = samples.filter((sample) => sample.level !== null);

  if (drawable.length < MIN_POINTS) {
    return (
      <figure className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
        <figcaption className="text-sm font-medium">{series.label}</figcaption>
        <p className="py-8 text-center text-sm text-muted-foreground">
          {drawable.length === 0
            ? "No breadth has been recorded for this universe yet."
            : `Only ${drawable.length} day of history so far — a line needs two.`}
        </p>
      </figure>
    );
  }

  const xScale = scaleLinear<number>({
    domain: [0, samples.length - 1],
    range: [0, INNER_WIDTH],
  });
  const breadthScale = scaleLinear<number>({ domain: [0, 100], range: [INNER_HEIGHT, 0] });

  const levelValues = levels.map((sample) => sample.level ?? 0);
  const levelScale = scaleLinear<number>({
    domain:
      levelValues.length > 0
        ? [Math.min(...levelValues), Math.max(...levelValues)]
        : [0, 1],
    range: [INNER_HEIGHT, 0],
    nice: true,
  });

  const first = drawable[0];
  const last = drawable[drawable.length - 1];

  return (
    <figure className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
      <figcaption className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium">{series.label}</span>
        <span className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <span aria-hidden="true" className="inline-block h-0.5 w-4 bg-accent" />
            breadth %
          </span>
          <span className="flex items-center gap-1">
            <span
              aria-hidden="true"
              className="inline-block h-0.5 w-4 border-t border-dashed border-muted-foreground"
            />
            {universeName} level
          </span>
        </span>
      </figcaption>

      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-labelledby={titleId}
        className="w-full"
      >
        <title id={titleId}>
          {`${series.label} for ${universeName}, ${formatNumber(first?.breadth, { decimals: 1 })}% on ${formatTradeDate(first?.date)} to ${formatNumber(last?.breadth, { decimals: 1 })}% on ${formatTradeDate(last?.date)}, with the index level overlaid.`}
        </title>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {BREADTH_TICKS.map((tick) => (
            <g key={tick} transform={`translate(0,${breadthScale(tick)})`}>
              <line x1={0} x2={INNER_WIDTH} className="stroke-border" strokeWidth={0.5} />
              <text x={-6} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[9px]">
                {tick}%
              </text>
            </g>
          ))}

          {levelValues.length >= MIN_POINTS ? (
            <LinePath
              data={levels}
              x={(sample) => xScale(sample.index)}
              y={(sample) => levelScale(sample.level ?? 0)}
              curve={curveMonotoneX}
              strokeWidth={1.25}
              strokeDasharray="3 3"
              className="stroke-muted-foreground"
              fill="none"
            />
          ) : null}

          <LinePath
            data={drawable}
            x={(sample) => xScale(sample.index)}
            y={(sample) => breadthScale(sample.breadth ?? 0)}
            curve={curveMonotoneX}
            strokeWidth={1.75}
            className="stroke-accent"
            fill="none"
          />

          <text
            x={0}
            y={INNER_HEIGHT + 16}
            className="fill-muted-foreground text-[9px]"
          >
            {formatTradeDate(first?.date)}
          </text>
          <text
            x={INNER_WIDTH}
            y={INNER_HEIGHT + 16}
            textAnchor="end"
            className="fill-muted-foreground text-[9px]"
          >
            {formatTradeDate(last?.date)}
          </text>
        </g>
      </svg>
    </figure>
  );
}
