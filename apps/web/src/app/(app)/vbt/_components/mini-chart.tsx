/**
 * `05` §2's "130-bar mini chart (SVG: close line, 200-DMA, 21-EMA, the prior 20-day high as a
 * level, the limit and the stop as lines)".
 *
 * Pure SVG on the server, next to the numbers a person acts on. The closes are **adjusted** (the
 * endpoint's contract), so a split reads as a shape rather than a cliff; the levels drawn over
 * them are exchange prices, and each is drawn only when it falls inside the series' range, which
 * is the only place it means anything.
 */
const WIDTH = 160;
const HEIGHT = 40;
const PAD = 2;

export interface ChartBar {
  date: string;
  close: number;
}

function linePath(values: number[], scaleY: (value: number) => number): string {
  const step = values.length > 1 ? (WIDTH - PAD * 2) / (values.length - 1) : 0;
  return values
    .map(
      (value, index) =>
        `${index === 0 ? "M" : "L"}${(PAD + index * step).toFixed(1)} ${scaleY(value).toFixed(1)}`,
    )
    .join(" ");
}

export function MiniChart({
  bars,
  limit,
  stop,
  priorHigh,
  label,
}: {
  bars: ChartBar[];
  limit: number | null;
  stop: number | null;
  priorHigh: number | null;
  label: string;
}) {
  if (bars.length < 2) {
    return (
      <span
        className="text-xs text-muted-foreground"
        data-testid="mini-chart-empty"
      >
        no bars
      </span>
    );
  }
  const closes = bars.map((bar) => bar.close);
  const levels = [limit, stop, priorHigh].filter(
    (value): value is number => value !== null,
  );
  const low = Math.min(...closes, ...levels);
  const high = Math.max(...closes, ...levels);
  const span = high - low || 1;
  const scaleY = (value: number) =>
    HEIGHT - PAD - ((value - low) / span) * (HEIGHT - PAD * 2);

  const level = (value: number | null, className: string, name: string) =>
    value === null || value < low || value > high ? null : (
      <line
        x1={PAD}
        x2={WIDTH - PAD}
        y1={scaleY(value)}
        y2={scaleY(value)}
        className={className}
        strokeWidth={1}
        strokeDasharray="2 2"
        data-level={name}
      />
    );

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      width={WIDTH}
      height={HEIGHT}
      role="img"
      aria-label={label}
      data-testid="mini-chart"
      className="overflow-visible"
    >
      {level(priorHigh, "stroke-muted-foreground/60", "prior-high")}
      {level(limit, "stroke-sky-600/70", "limit")}
      {level(stop, "stroke-rose-600/70", "stop")}
      <path
        d={linePath(closes, scaleY)}
        fill="none"
        className="stroke-foreground"
        strokeWidth={1}
      />
    </svg>
  );
}
