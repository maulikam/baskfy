import type { SwingBar } from "@/lib/swing/fetch";

/**
 * `05` §2's "130-bar mini chart per row (SVG, close line + MA10/MA20 + pivot line + stop line)".
 *
 * Pure SVG, rendered on the server: the shape of the pole, the base and the pivot in one
 * glance, next to the numbers a person acts on. Adjusted closes (the endpoint's contract), so a
 * split is a shape and not a cliff; the trigger and stop lines are the row's exchange prices and
 * are drawn only when they fall inside the series' range, which is where they mean something.
 */
const WIDTH = 160;
const HEIGHT = 40;
const PAD = 2;

function path(points: (number | null)[], scaleY: (value: number) => number): string {
  const step = points.length > 1 ? (WIDTH - PAD * 2) / (points.length - 1) : 0;
  let d = "";
  let open = false;
  points.forEach((value, index) => {
    if (value === null) {
      open = false;
      return;
    }
    const x = (PAD + index * step).toFixed(1);
    const y = scaleY(value).toFixed(1);
    d += `${open ? "L" : "M"}${x} ${y} `;
    open = true;
  });
  return d.trim();
}

export function MiniChart({
  bars,
  trigger,
  stop,
  label,
}: {
  bars: SwingBar[];
  trigger: number | null;
  stop: number | null;
  label: string;
}) {
  if (bars.length < 2) return null;
  const closes = bars.map((bar) => bar.close);
  const low = Math.min(...closes, ...(stop !== null ? [stop] : []));
  const high = Math.max(...closes, ...(trigger !== null ? [trigger] : []));
  const span = high - low || 1;
  const scaleY = (value: number) => HEIGHT - PAD - ((value - low) / span) * (HEIGHT - PAD * 2);
  const level = (value: number | null) =>
    value !== null && value >= low && value <= high ? scaleY(value).toFixed(1) : null;
  const triggerY = level(trigger);
  const stopY = level(stop);
  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      width={WIDTH}
      height={HEIGHT}
      role="img"
      aria-label={label}
      className="block text-foreground"
    >
      <path d={path(bars.map((bar) => bar.ma_slow), scaleY)} fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1" />
      <path d={path(bars.map((bar) => bar.ma_fast), scaleY)} fill="none" stroke="currentColor" strokeOpacity="0.45" strokeWidth="1" />
      <path d={path(closes, scaleY)} fill="none" stroke="currentColor" strokeWidth="1.25" />
      {triggerY !== null ? (
        <line x1={PAD} x2={WIDTH - PAD} y1={triggerY} y2={triggerY} stroke="currentColor" strokeDasharray="3 2" strokeOpacity="0.7" strokeWidth="1" />
      ) : null}
      {stopY !== null ? (
        <line x1={PAD} x2={WIDTH - PAD} y1={stopY} y2={stopY} stroke="#dc2626" strokeDasharray="3 2" strokeOpacity="0.8" strokeWidth="1" />
      ) : null}
    </svg>
  );
}
