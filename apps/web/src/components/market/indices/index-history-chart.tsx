"use client";

import { curveMonotoneX } from "@visx/curve";
import { scaleLinear } from "@visx/scale";
import { LinePath } from "@visx/shape";
import { useId, useMemo } from "react";

import type { HistoryPoint } from "@/lib/indices/api";
import { formatNumber, formatTradeDate } from "@/lib/format";

/** One-year index level with 10/20/50-day averages — same visx + plain-SVG-axis stack as `EquityChart`. */

const WIDTH = 900;
const HEIGHT = 260;
const MARGIN = { top: 12, right: 16, bottom: 24, left: 60 };

type SeriesKey = "close" | "dma10" | "dma20" | "dma50";

const SERIES: readonly { key: SeriesKey; label: string; className: string; dash?: string }[] = [
  { key: "close", label: "Level", className: "stroke-foreground" },
  { key: "dma10", label: "10-day avg", className: "stroke-info", dash: "3 3" },
  { key: "dma20", label: "20-day avg", className: "stroke-accent" },
  { key: "dma50", label: "50-day avg", className: "stroke-warning", dash: "6 3" },
];

export function IndexHistoryChart({ points, name }: { points: readonly HistoryPoint[]; name: string }) {
  const titleId = useId();
  const samples = useMemo(
    () => points.filter((p) => p.close !== null).map((p, index) => ({ ...p, index })),
    [points],
  );

  const scales = useMemo(() => {
    const values = samples.flatMap((s) =>
      [s.close, s.dma10, s.dma20, s.dma50].filter((v): v is number => v !== null),
    );
    if (values.length === 0) return null;
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const pad = (hi - lo) * 0.05 || 1;
    return {
      x: scaleLinear<number>({
        domain: [0, Math.max(samples.length - 1, 1)],
        range: [MARGIN.left, WIDTH - MARGIN.right],
      }),
      y: scaleLinear<number>({
        domain: [lo - pad, hi + pad],
        range: [HEIGHT - MARGIN.bottom, MARGIN.top],
      }),
      lo,
      hi,
    };
  }, [samples]);

  if (samples.length < 2 || !scales) {
    return (
      <p className="rounded-md border border-dashed border-border px-4 py-10 text-center text-sm text-muted-foreground">
        Not enough daily history yet to draw {name}&apos;s year.
      </p>
    );
  }

  const first = samples[0];
  const last = samples[samples.length - 1];
  const ticks = [scales.lo, (scales.lo + scales.hi) / 2, scales.hi];

  return (
    <figure className="flex flex-col gap-2">
      <svg
        role="img"
        aria-labelledby={titleId}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full"
      >
        <title id={titleId}>
          {`${name}, one year: from ${formatNumber(first?.close, { decimals: 2 })} on ${formatTradeDate(first?.date)} to ${formatNumber(last?.close, { decimals: 2 })} on ${formatTradeDate(last?.date)}`}
        </title>
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={MARGIN.left}
              x2={WIDTH - MARGIN.right}
              y1={scales.y(t)}
              y2={scales.y(t)}
              className="stroke-border"
              strokeDasharray="2 4"
            />
            <text
              x={MARGIN.left - 6}
              y={scales.y(t)}
              textAnchor="end"
              dominantBaseline="middle"
              className="fill-muted-foreground text-[11px] tabular-nums"
            >
              {formatNumber(t, { decimals: 0 })}
            </text>
          </g>
        ))}
        <text x={MARGIN.left} y={HEIGHT - 6} className="fill-muted-foreground text-[11px]">
          {formatTradeDate(first?.date)}
        </text>
        <text
          x={WIDTH - MARGIN.right}
          y={HEIGHT - 6}
          textAnchor="end"
          className="fill-muted-foreground text-[11px]"
        >
          {formatTradeDate(last?.date)}
        </text>
        {SERIES.map((s) => (
          <LinePath
            key={s.key}
            data={samples.filter((p) => p[s.key] !== null)}
            x={(p) => scales.x(p.index)}
            y={(p) => scales.y(p[s.key] ?? 0)}
            curve={curveMonotoneX}
            strokeWidth={s.key === "close" ? 1.75 : 1.25}
            {...(s.dash ? { strokeDasharray: s.dash } : {})}
            className={s.className}
            fill="none"
          />
        ))}
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {SERIES.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <svg width="18" height="6" aria-hidden="true">
              <line
                x1="0"
                x2="18"
                y1="3"
                y2="3"
                strokeWidth="2"
                className={s.className}
                {...(s.dash ? { strokeDasharray: s.dash } : {})}
              />
            </svg>
            {s.label}
          </span>
        ))}
      </figcaption>
    </figure>
  );
}
