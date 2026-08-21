import { EMPTY_CELL, formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * One of docs/01 §6's four breadth gauges, rendered as docs/08 §"Market Health" asks:
 * "Universe selector + four **large** gauges."
 *
 * A 270° arc drawn with two SVG paths — the track and the filled portion — rather than a chart
 * library, because a gauge is one arc and `stroke-dasharray` already does the arithmetic. visx is
 * locked for charts (docs/02) and is used for the history chart below; a dependency is not needed
 * to draw a circle.
 *
 * The number is stated as text as well as drawn, and the accessible name carries the value, so
 * docs/11 §Accessibility holds: the arc is never the only carrier of the meaning.
 *
 * `null` renders as an em dash, never as an empty arc that reads as 0%. A breadth series with no
 * factor rows behind it is unknown, and "0% of this universe is above its 200-day average" is a
 * claim about the market rather than about our pipeline.
 */
export interface BreadthGaugeProps {
  label: string;
  /** A percentage, 0–100. `null` when the inputs are absent. */
  value: number | null;
  className?: string | undefined;
}

const SIZE = 132;
const STROKE = 12;
const RADIUS = (SIZE - STROKE) / 2;
/** 270° of a circle, opening at the bottom — the shape a gauge is expected to have. */
const SWEEP_DEGREES = 270;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
const ARC_LENGTH = (CIRCUMFERENCE * SWEEP_DEGREES) / 360;

function toneFor(value: number | null): string {
  if (value === null) return "stroke-border";
  if (value >= 60) return "stroke-positive";
  if (value >= 40) return "stroke-accent";
  return "stroke-negative";
}

export function BreadthGauge({ label, value, className }: BreadthGaugeProps) {
  const clamped = value === null ? 0 : Math.min(Math.max(value, 0), 100);
  const filled = (ARC_LENGTH * clamped) / 100;
  const display = value === null ? EMPTY_CELL : `${formatNumber(value, { decimals: 1 })}%`;

  return (
    <figure
      className={cn(
        "flex flex-col items-center gap-2 rounded-lg border border-border bg-card p-4",
        className,
      )}
    >
      <div className="relative" style={{ width: SIZE, height: SIZE }}>
        <svg
          width={SIZE}
          height={SIZE}
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          role="img"
          aria-label={`${label}: ${value === null ? "no data" : display}`}
          /* Rotated so the 270° gap sits at the bottom. */
          style={{ transform: "rotate(135deg)" }}
        >
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            strokeWidth={STROKE}
            strokeLinecap="round"
            className="stroke-muted"
            strokeDasharray={`${ARC_LENGTH} ${CIRCUMFERENCE}`}
          />
          {value === null ? null : (
            <circle
              cx={SIZE / 2}
              cy={SIZE / 2}
              r={RADIUS}
              fill="none"
              strokeWidth={STROKE}
              strokeLinecap="round"
              className={toneFor(value)}
              strokeDasharray={`${filled} ${CIRCUMFERENCE}`}
            />
          )}
        </svg>
        <p className="absolute inset-0 flex items-center justify-center text-2xl font-semibold tnum">
          {display}
        </p>
      </div>
      <figcaption className="text-center text-sm text-muted-foreground">{label}</figcaption>
    </figure>
  );
}
