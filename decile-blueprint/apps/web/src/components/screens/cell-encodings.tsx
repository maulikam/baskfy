"use client";

import { EMPTY_CELL, formatFraction } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The score that paints a full track when there is no row set to scale against.
 *
 * A consistency score is a Sharpe-style ratio, so it has no natural ceiling — which is why the
 * scale for the results table is derived from the rows on screen (`scoreBarScale`) rather than
 * fixed here. This constant only covers the surfaces that render one score in isolation, where a
 * "largest on screen" does not exist: the peek drawer and the mobile cards. 3.00 is chosen against
 * the seeded universe, where the 271 rows run -0.39 to 5.13 with a median of 0.55 and a 95th
 * percentile of 1.68 — a score of 3 is exceptional, so it makes a reference a lone bar can be read
 * against. It is a floor, never a clamp: a value above it scales against itself (see `ScoreBar`),
 * so two different scores can never paint the same width.
 */
export const REFERENCE_SCORE_SCALE = 3;

/**
 * The score that should paint a full track, given every score in the visible set.
 *
 * Non-finite and non-numeric entries are ignored rather than poisoning the maximum, and a set with
 * nothing positive in it falls back to the reference scale so the divisor is never zero.
 */
export function scoreBarScale(values: Iterable<unknown>): number {
  let largest = 0;
  for (const value of values) {
    const numeric = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(numeric) && numeric > largest) largest = numeric;
  }
  return largest > 0 ? largest : REFERENCE_SCORE_SCALE;
}

/**
 * Score bar for consistency / Sharpe-style ratios (§2.1) — length ∝ score.
 *
 * The fill is the one accent the brief allows this table, in `--brand`, which is the token for a
 * fill (`--accent` is the same hue kept legible as text; see `no-brand-as-text.test.ts`).
 */
export function ScoreBar({
  value,
  /** The score that paints the full track. Omit it only where no row set exists. */
  scale,
}: {
  value: number;
  scale?: number | undefined;
}) {
  const finite = Number.isFinite(value);
  /* Above the reference the bar scales against the value itself, so a lone 5.13 fills the track
     and a lone 3.5 does not paint the same width as a 3.0. */
  const ceiling =
    scale !== undefined && Number.isFinite(scale) && scale > 0
      ? scale
      : Math.max(value, REFERENCE_SCORE_SCALE);
  const ratio = finite ? Math.min(1, Math.max(0, value / ceiling)) : 0;

  return (
    <div className="flex w-full items-center gap-2">
      <div
        className="h-1.5 min-w-[3rem] flex-1 overflow-hidden rounded-full bg-muted"
        aria-hidden="true"
      >
        <div
          data-testid="score-bar-fill"
          className="h-full w-full origin-left rounded-full bg-brand motion-safe:transition-transform motion-safe:duration-200"
          style={{ transform: `scaleX(${Number(ratio.toFixed(4))})` }}
        />
      </div>
      <span
        className={cn("shrink-0 tabular-nums text-xs", !finite && "text-muted-foreground")}
      >
        {finite ? value.toFixed(2) : EMPTY_CELL}
      </span>
    </div>
  );
}

/** Return chip — signed colour, tabular figure (§2.1). */
export function ReturnChip({ text, value }: { text: string; value: number }) {
  return (
    <span
      className={cn(
        "inline-flex rounded-md px-1.5 py-0.5 text-xs font-medium tabular-nums",
        value > 0 && "bg-positive/15 text-positive",
        value < 0 && "bg-negative/15 text-negative",
        value === 0 && "bg-muted text-muted-foreground",
      )}
    >
      {text}
    </span>
  );
}

/**
 * Annualised volatility bands, as decimal fractions (`vol_12m` is stored as a fraction, not a
 * percent — docs/06a §10).
 *
 * Absolute, not screen-relative: three dots has to mean the same risk on every screen, so unlike
 * `ScoreBar` this encoding cannot take its ceiling from the rows on screen. The cuts are round
 * numbers — 25/35/45/55% — chosen against the real spread on the seeded universe, where the 271
 * rows run 0.179 to 0.618 with quartiles at 0.306 / 0.366 / 0.430. That fills all five bands
 * (the old ceiling of 0.8 could not reach the fifth) and separates the calmest name, at one dot,
 * from the median one, at three.
 */
export const BUMPINESS_THRESHOLDS: readonly number[] = [0.25, 0.35, 0.45, 0.55];

/** 1–5 for a real volatility, `null` for a value nobody knows. */
export function bumpinessBand(value: number): number | null {
  if (!Number.isFinite(value)) return null;
  let band = 1;
  for (const threshold of BUMPINESS_THRESHOLDS) {
    if (value >= threshold) band += 1;
  }
  return band;
}

/** Bumpiness dots — five-step volatility encoding (§2.1). */
export function BumpinessDots({ value }: { value: number }) {
  const filled = bumpinessBand(value);
  /* vol_12m is a decimal fraction; formatFraction is the same ×100 the table uses for the
     visible percent, so hover and the adjacent figure cannot disagree. Non-finite values
     have no percent to show — a title of the em dash would be a lie about a missing reading. */
  return (
    <span
      className="inline-flex items-center gap-0.5"
      title={Number.isFinite(value) ? formatFraction(value) : undefined}
    >
      {/* Deliberately not the accent: this is a risk reading, and painting it in the one colour
          the page uses for emphasis would read as a recommendation of the bumpiest names. */}
      <span className="inline-flex items-center gap-0.5" aria-hidden="true">
        {Array.from({ length: BUMPINESS_THRESHOLDS.length + 1 }, (_, index) => (
          <span
            key={index}
            className={cn(
              "size-1.5 rounded-full",
              filled !== null && index < filled
                ? "bg-muted-foreground"
                : "bg-muted-foreground/25",
            )}
          />
        ))}
      </span>
      {/* Outside the aria-hidden subtree, or the dots would carry no non-visual equivalent.
          The percent is in here too: result-cards renders dots with no adjacent figure. */}
      <span className="sr-only">
        {filled === null
          ? "bumpiness unknown"
          : `bumpiness ${filled} of ${BUMPINESS_THRESHOLDS.length + 1}, ${formatFraction(value)}`}
      </span>
    </span>
  );
}

/** Rank badge for positions 1–3 (§2.1). */
export function RankBadge({ rank }: { rank: number }) {
  if (rank > 3) {
    return <span className="w-full text-right tabular-nums text-muted-foreground">{rank}</span>;
  }
  /* Neutral on purpose. The row already spends its one accent on the score bar, and a second use
     of it a few columns away leaves neither reading as the emphasis. */
  const tone =
    rank === 1
      ? "bg-foreground text-background"
      : rank === 2
        ? "bg-muted-foreground text-background"
        : "bg-muted text-foreground";
  return (
    <span
      className={cn(
        "inline-flex size-6 items-center justify-center rounded-full text-xs font-semibold tabular-nums",
        tone,
      )}
    >
      {rank}
    </span>
  );
}
