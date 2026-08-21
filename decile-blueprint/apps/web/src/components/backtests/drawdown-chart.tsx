"use client";

import type { DrawdownPointOut } from "@decile/api-client";
import { scaleLinear } from "@visx/scale";
import { AreaClosed } from "@visx/shape";
import { useId, useMemo } from "react";

import { formatTradeDate } from "@/lib/format";

/**
 * docs/08 §Backtests: "drawdown chart". docs/10 §Artefacts: "drawdown series".
 *
 * Drawn as an area hanging from zero, because that is what a drawdown *is* — distance below the
 * high-water mark — and a line hovering near the top of a chart reads as a return series. The
 * y-axis is inverted for the same reason: down is worse.
 */
export interface DrawdownChartProps {
  points: readonly DrawdownPointOut[];
  height?: number;
}

const WIDTH = 900;
const DEFAULT_HEIGHT = 160;
const MARGIN = { top: 10, right: 16, bottom: 22, left: 52 };
const MIN_POINTS = 2;

interface Sample {
  index: number;
  date: string;
  drawdown: number;
}

export function DrawdownChart({ points, height = DEFAULT_HEIGHT }: DrawdownChartProps) {
  const titleId = useId();
  const samples = useMemo<Sample[]>(
    () => points.map((point, index) => ({ index, date: point.date, drawdown: point.drawdown * 100 })),
    [points],
  );
  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = height - MARGIN.top - MARGIN.bottom;

  if (samples.length < MIN_POINTS) {
    return (
      <figure className="rounded-lg border border-border bg-card p-4">
        <figcaption className="text-sm font-medium">Drawdown</figcaption>
        <p className="py-8 text-center text-sm text-muted-foreground">
          No drawdown series was recorded for this run.
        </p>
      </figure>
    );
  }

  const worst = Math.min(...samples.map((sample) => sample.drawdown));
  const xScale = scaleLinear<number>({ domain: [0, samples.length - 1], range: [0, innerWidth] });
  const yScale = scaleLinear<number>({
    domain: [Math.min(worst, -1), 0],
    range: [innerHeight, 0],
    nice: true,
  });
  const ticks = yScale.ticks(3);
  const last = samples[samples.length - 1];

  return (
    <figure className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
      <figcaption className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium">Drawdown</span>
        <span className="text-xs text-muted-foreground">
          Deepest: {worst.toFixed(2)}%
        </span>
      </figcaption>
      <svg viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-labelledby={titleId} className="w-full">
        <title id={titleId}>
          {`Drawdown from ${formatTradeDate(samples[0]?.date)} to ${formatTradeDate(last?.date)}, deepest ${worst.toFixed(2)} percent.`}
        </title>
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {ticks.map((tick) => (
            <g key={tick} transform={`translate(0,${yScale(tick)})`}>
              <line x1={0} x2={innerWidth} className="stroke-border" strokeWidth={0.5} />
              <text x={-8} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[10px]">
                {tick}%
              </text>
            </g>
          ))}
          <AreaClosed<Sample>
            data={samples}
            x={(sample) => xScale(sample.index)}
            y={(sample) => yScale(sample.drawdown)}
            yScale={yScale}
            className="fill-destructive/25 stroke-destructive"
            strokeWidth={1}
          />
        </g>
      </svg>
    </figure>
  );
}
