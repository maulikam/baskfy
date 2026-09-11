import type { TwtEquityPoint } from "@/lib/twt/fetch";

/**
 * The equity curve, as one inline path — `docs/twt/05` §3.
 *
 * No charting library: it is a polyline over a few hundred points, and a dependency for that is a
 * dependency to keep patched (house rule 1 — nothing outside the locked stack without saying why
 * first).
 *
 * **Money becomes a `number` here and only here, and only to place a pixel.** House rule 9 governs
 * figures a person reads or arithmetic a person relies on; a y-coordinate is neither. Every
 * number printed beside this chart comes from the decimal-string layer, never from these floats.
 */
export function EquityCurve({
  points,
  label,
}: {
  points: readonly TwtEquityPoint[];
  label: string;
}) {
  if (points.length < 2) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="twt-equity-unavailable">
        The curve needs at least two settled sessions before it can be drawn.
      </p>
    );
  }

  const values = points.map((point) => Number(point.equity_inr));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const width = 320;
  const height = 72;
  const path = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width;
      const y = height - ((value - low) / span) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${width} ${height}`}
      className="h-20 w-full max-w-full text-accent"
      data-testid="twt-equity-curve"
      preserveAspectRatio="none"
    >
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
