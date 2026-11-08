import type { VbtBreadthPoint } from "@/lib/vbt/fetch";

/**
 * `05` §2's breadth gauge: the `vb_breadth_daily` series with the gate's line drawn across it.
 *
 * The 40% line is the whole chart. Breadth on its own is a number nobody has intuition for; the
 * useful question is only ever "which side of the line, and for how long", so the line is drawn
 * first and the series is shaded against it. The threshold is served with the series
 * (`threshold_pct`), never hard-coded, so this cannot disagree with the detector.
 */
const WIDTH = 720;
const HEIGHT = 120;
const PAD = 4;

export function BreadthGauge({
  data,
  threshold,
}: {
  data: VbtBreadthPoint[];
  threshold: number;
}) {
  if (data.length < 2) {
    return (
      <p
        className="text-sm text-muted-foreground"
        data-testid="breadth-gauge-empty"
      >
        The breadth series needs at least two sessions before it says anything.
      </p>
    );
  }
  const step = (WIDTH - PAD * 2) / (data.length - 1);
  const scaleY = (value: number) =>
    HEIGHT - PAD - (value / 100) * (HEIGHT - PAD * 2);
  const line = data
    .map(
      (point, index) =>
        `${index === 0 ? "M" : "L"}${(PAD + index * step).toFixed(1)} ${scaleY(point.pct_above_dma).toFixed(1)}`,
    )
    .join(" ");
  const area = `${line} L${(PAD + (data.length - 1) * step).toFixed(1)} ${HEIGHT - PAD} L${PAD} ${HEIGHT - PAD} Z`;
  const latest = data[data.length - 1];

  return (
    <figure className="space-y-1">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`Breadth over ${data.length} sessions, against a ${threshold.toFixed(0)}% gate`}
        data-testid="breadth-gauge"
        className="w-full"
      >
        <path d={area} className="fill-sky-500/10" />
        <line
          x1={PAD}
          x2={WIDTH - PAD}
          y1={scaleY(threshold)}
          y2={scaleY(threshold)}
          className="stroke-amber-600"
          strokeWidth={1}
          strokeDasharray="4 3"
          data-testid="breadth-gate-line"
        />
        <path
          d={line}
          fill="none"
          className="stroke-sky-700"
          strokeWidth={1.25}
        />
      </svg>
      <figcaption className="text-xs text-muted-foreground">
        {data.length.toLocaleString("en-IN")} sessions, oldest first. The dashed
        line is the {threshold.toFixed(0)}% gate; the latest reading is{" "}
        <span className="tabular-nums text-foreground">
          {latest ? latest.pct_above_dma.toFixed(1) : "—"}%
        </span>
        .
      </figcaption>
    </figure>
  );
}
