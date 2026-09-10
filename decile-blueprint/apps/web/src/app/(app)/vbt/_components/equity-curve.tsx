import type { VbtEquityPoint } from "@/lib/vbt/fetch";

/**
 * `05` §2's equity curve, from `vb_backtest_run.stats.full.equity_curve` (VB9).
 *
 * Log-free and unlabelled on purpose: the shape a reader needs from this chart is the drawdowns,
 * not the level, and the level is in the table above it. The deepest trough is marked, because
 * "27.9% at some point" is a very different fact from "27.9%, there".
 *
 * Money arrives as a string of its exact decimal (house rule 9) and is parsed here only to plot
 * a pixel. No displayed number comes from these floats.
 */
const WIDTH = 720;
const HEIGHT = 160;
const PAD = 6;

export function EquityCurve({ data }: { data: VbtEquityPoint[] }) {
  if (data.length < 2) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="equity-curve-empty">
        The equity curve needs at least two sessions.
      </p>
    );
  }
  const values = data.map((point) => Number(point.equity_inr));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;
  const step = (WIDTH - PAD * 2) / (values.length - 1);
  const scaleY = (value: number) => HEIGHT - PAD - ((value - low) / span) * (HEIGHT - PAD * 2);
  const path = values
    .map(
      (value, index) =>
        `${index === 0 ? "M" : "L"}${(PAD + index * step).toFixed(1)} ${scaleY(value).toFixed(1)}`,
    )
    .join(" ");

  // The deepest point below the running peak — the drawdown a reader is looking for.
  let peak = values[0] ?? 0;
  let worstIndex = 0;
  let worst = 0;
  values.forEach((value, index) => {
    peak = Math.max(peak, value);
    const fall = peak === 0 ? 0 : (value - peak) / peak;
    if (fall < worst) {
      worst = fall;
      worstIndex = index;
    }
  });

  return (
    <figure className="space-y-1">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`Equity over ${data.length} sessions, worst drawdown ${(worst * 100).toFixed(1)}%`}
        data-testid="equity-curve"
        className="w-full"
      >
        <path d={path} fill="none" className="stroke-sky-700" strokeWidth={1.25} />
        {worst < 0 ? (
          <circle
            cx={PAD + worstIndex * step}
            cy={scaleY(values[worstIndex] ?? low)}
            r={3}
            className="fill-rose-600"
            data-testid="equity-curve-trough"
          />
        ) : null}
      </svg>
      <figcaption className="text-xs text-muted-foreground">
        {data.length.toLocaleString("en-IN")} sessions, oldest first. The marked point is the
        deepest fall below the running peak, {(worst * 100).toFixed(1)}%, on{" "}
        {data[worstIndex]?.date ?? "—"}.
      </figcaption>
    </figure>
  );
}
