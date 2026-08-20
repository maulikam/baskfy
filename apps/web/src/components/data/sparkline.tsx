"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId } from "react";

import { cn } from "@/lib/utils";

/**
 * A sparkline — docs/02 locks **visx** for charts; docs/08 §"Instrument factsheet" puts one in
 * every metric card and §Dashboard one per index.
 *
 * Accessibility, because a chart with no text is invisible to half the people using it:
 * `role="img"` plus a `<title>` that states the first value, the last value and the direction, so
 * a screen reader gets the shape in words. docs/11 §Accessibility: colour is never the sole
 * carrier of meaning, so the stroke colour is derived from direction *and* the label says it.
 */
export interface SparklineProps {
  values: readonly number[];
  width?: number;
  height?: number;
  /** What the series is, for the accessible name: "30-day close". */
  label: string;
  className?: string | undefined;
}

const STROKE = 1.5;

export function Sparkline({
  values,
  width = 96,
  height = 24,
  label,
  className,
}: SparklineProps) {
  const titleId = useId();
  const first = values[0];
  const last = values[values.length - 1];

  if (values.length < 2 || first === undefined || last === undefined) {
    return (
      <svg
        role="img"
        aria-labelledby={titleId}
        width={width}
        height={height}
        className={cn("overflow-visible", className)}
      >
        <title id={titleId}>{`${label}: not enough history to chart`}</title>
        <line
          x1={0}
          x2={width}
          y1={height / 2}
          y2={height / 2}
          className="stroke-border"
          strokeWidth={STROKE}
          strokeDasharray="2 3"
        />
      </svg>
    );
  }

  const rising = last >= first;
  const xScale = scaleLinear<number>({
    domain: [0, values.length - 1],
    range: [STROKE, width - STROKE],
  });
  const yScale = scaleLinear<number>({
    domain: [Math.min(...values), Math.max(...values)],
    range: [height - STROKE, STROKE],
  });

  const indexed = values.map((value, index) => ({ index, value }));

  return (
    <svg
      role="img"
      aria-labelledby={titleId}
      width={width}
      height={height}
      className={cn("overflow-visible", className)}
    >
      <title id={titleId}>
        {`${label}: ${rising ? "rising" : "falling"}, from ${first} to ${last}`}
      </title>
      <LinePath
        data={indexed}
        x={(point) => xScale(point.index)}
        y={(point) => yScale(point.value)}
        curve={curveMonotoneX}
        strokeWidth={STROKE}
        className={rising ? "stroke-positive" : "stroke-negative"}
        fill="none"
      />
    </svg>
  );
}
