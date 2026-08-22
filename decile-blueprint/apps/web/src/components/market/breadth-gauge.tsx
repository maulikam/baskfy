import { TermHint } from "@/components/ui/term";
import { EMPTY_CELL, formatNumber } from "@/lib/format";
import type { TermId } from "@/lib/vocabulary";
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
  /**
   * The vocabulary entry that says what this measures (M36). The caption carries the plain label
   * and this puts the professional name and the explanation one hover away — the gauges are the
   * densest jargon on the site, and "Within 10% of ATH" is unreadable to a first-time visitor.
   */
  term?: TermId;
  /** One line under the caption reading the number back in words. */
  reading?: string;
  className?: string | undefined;
}

const SIZE = 132;
const STROKE = 12;
const RADIUS = (SIZE - STROKE) / 2;
/** 270° of a circle, opening at the bottom — the shape a gauge is expected to have. */
const SWEEP_DEGREES = 270;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
const ARC_LENGTH = (CIRCUMFERENCE * SWEEP_DEGREES) / 360;

/**
 * Green when most of the market is participating, red when little of it is, neutral in between.
 *
 * The middle band used to be the accent. That worked while the accent was a blue; against M36's
 * orange it sat a few degrees of hue from `--negative` and the two arcs became one colour at a
 * glance — a 56% dial and a 19% dial looked like the same reading. A warm grey says "neither"
 * without pretending to be a verdict, which is also the more honest thing for a middling number.
 *
 * Colour is never the only carrier: the figure is printed inside the arc, the accessible name
 * repeats it, and the caption reads it back in words (docs/11 §Accessibility).
 */
function toneFor(value: number | null): string {
  if (value === null) return "stroke-border";
  if (value >= 60) return "stroke-positive";
  if (value >= 40) return "stroke-muted-foreground";
  return "stroke-negative";
}

export function BreadthGauge({ label, value, term, reading, className }: BreadthGaugeProps) {
  const clamped = value === null ? 0 : Math.min(Math.max(value, 0), 100);
  const filled = (ARC_LENGTH * clamped) / 100;
  const display = value === null ? EMPTY_CELL : `${formatNumber(value, { decimals: 1 })}%`;

  return (
    <figure
      className={cn(
        "flex flex-col items-center gap-2 rounded-xl border border-border/70 bg-card p-5",
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
      <figcaption className="flex flex-col items-center gap-1 text-center">
        <span className="flex items-center gap-1.5 text-sm font-medium">
          {label}
          {term ? <TermHint id={term} /> : null}
        </span>
        {reading ? (
          <span className="text-xs leading-relaxed text-muted-foreground">{reading}</span>
        ) : null}
      </figcaption>
    </figure>
  );
}
