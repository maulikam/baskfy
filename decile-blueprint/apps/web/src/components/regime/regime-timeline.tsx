"use client";

import { curveLinear, curveStepAfter } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId } from "react";

import type { HistoryRow } from "@/components/regime/types";

const WIDTH = 700;
const HEIGHT = 170;
const MARGIN = { top: 12, right: 16, bottom: 22, left: 40 };
const INNER_W = WIDTH - MARGIN.left - MARGIN.right;
const INNER_H = HEIGHT - MARGIN.top - MARGIN.bottom;
const X_LABELS = 6;

interface Point {
  i: number;
  v: number | null;
}

/** Cap (step), actual equity and breadth by week — one percent axis, as on the desk. */
export function RegimeTimeline({ history }: { history: readonly HistoryRow[] }) {
  const titleId = useId();
  const n = history.length;
  const peak = Math.max(
    100,
    ...history.flatMap((h) => [h.cap, h.actual ?? 0, h.breadth ?? 0]),
  );
  const x = scaleLinear<number>({ domain: [0, Math.max(1, n - 1)], range: [0, INNER_W] });
  const y = scaleLinear<number>({ domain: [0, peak], range: [INNER_H, 0] });
  const series = (pick: (h: HistoryRow) => number | null): Point[] =>
    history.map((h, i) => ({ i, v: pick(h) }));
  const step = Math.max(1, Math.ceil(n / X_LABELS));
  const xLabels = history
    .map((h, i) => ({ i, label: h.week.slice(5) }))
    .filter(({ i }) => i % step === 0);

  const line = (data: Point[], cls: string, curve: typeof curveLinear, width: number) => (
    <LinePath
      data={data}
      defined={(p) => p.v !== null}
      x={(p) => x(p.i)}
      y={(p) => y(p.v ?? 0)}
      curve={curve}
      strokeWidth={width}
      strokeLinejoin="round"
      className={cls}
      fill="none"
    />
  );

  return (
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby={titleId} className="w-full">
      <title id={titleId}>Permitted cap, actual equity exposure and breadth by week</title>
      <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
        {[0, 50, 100].map((t) => (
          <g key={t} transform={`translate(0,${y(t)})`}>
            <line x1={0} x2={INNER_W} className="stroke-border" strokeWidth={0.5} />
            <text x={-7} dy="0.32em" textAnchor="end" className="fill-muted-foreground text-[10px]">
              {t}%
            </text>
          </g>
        ))}
        {xLabels.map(({ i, label }) => (
          <text
            key={i}
            x={x(i)}
            y={INNER_H + 16}
            textAnchor="middle"
            className="fill-muted-foreground text-[9.5px]"
          >
            {label}
          </text>
        ))}
        {line(series((h) => h.cap), "stroke-warning", curveStepAfter, 1.8)}
        {line(series((h) => h.breadth), "stroke-info opacity-85", curveLinear, 1.4)}
        {line(series((h) => h.actual), "stroke-positive", curveLinear, 1.8)}
      </g>
    </svg>
  );
}
