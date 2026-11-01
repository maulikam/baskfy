"use client";

import { LinePath } from "@visx/shape";

import { formatNumber } from "@/lib/format";
import { payoffGeometry, ticks } from "@/lib/opportunities/payoff";
import type { OptionCandidate, PayoffPoint } from "@/lib/opportunities/types";
import { STRATEGY_LABEL, formatInr, label } from "@/lib/opportunities/view";

const WIDTH = 760;
const HEIGHT = 300;
const MARGIN = { top: 16, right: 20, bottom: 32, left: 72 };
export const SERIES_COLORS = ["var(--accent)", "var(--warning)", "var(--info)"] as const;

export function PayoffChart({
  candidates,
  spot,
}: {
  candidates: readonly OptionCandidate[];
  spot: number | null;
}) {
  const series = candidates.map((c) => ({ id: c.id, points: c.payoff, breakevens: c.breakevens }));
  const geo = payoffGeometry(series, spot, WIDTH, HEIGHT, MARGIN);
  if (!geo) {
    return <p className="text-sm text-muted-foreground">Select up to three candidates to compare their expiry payoff.</p>;
  }
  const zeroY = geo.y(0);
  const bottom = MARGIN.top + geo.innerHeight;

  return (
    <figure className="space-y-2" data-testid="payoff-chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full"
        role="img"
        aria-label={`Expiry payoff per lot for ${candidates.map((c) => label(STRATEGY_LABEL, c.strategy)).join(", ")}`}
      >
        {ticks(geo.yDomain, 5).map((t) => (
          <g key={`y${t}`}>
            <line x1={MARGIN.left} x2={WIDTH - MARGIN.right} y1={geo.y(t)} y2={geo.y(t)} stroke="var(--border)" strokeDasharray="2 4" />
            <text x={MARGIN.left - 6} y={geo.y(t)} textAnchor="end" dominantBaseline="middle" className="fill-muted-foreground text-[10px]">
              {formatInr(t, 0)}
            </text>
          </g>
        ))}
        {ticks(geo.xDomain, 6).map((t) => (
          <text key={`x${t}`} x={geo.x(t)} y={bottom + 18} textAnchor="middle" className="fill-muted-foreground text-[10px]">
            {formatNumber(Math.round(t))}
          </text>
        ))}
        <line x1={MARGIN.left} x2={WIDTH - MARGIN.right} y1={zeroY} y2={zeroY} stroke="var(--foreground)" strokeOpacity={0.5} />
        {spot !== null ? (
          <g>
            <line x1={geo.x(spot)} x2={geo.x(spot)} y1={MARGIN.top} y2={bottom} stroke="var(--foreground)" strokeDasharray="4 3" />
            <text x={geo.x(spot) + 4} y={MARGIN.top + 10} className="fill-foreground text-[10px]">
              Spot {formatNumber(spot, { decimals: 2 })}
            </text>
          </g>
        ) : null}
        {candidates.map((candidate, i) => {
          const color = SERIES_COLORS[i % SERIES_COLORS.length];
          return (
            <g key={candidate.id}>
              <LinePath<PayoffPoint>
                data={candidate.payoff}
                x={(p) => geo.x(p.underlying)}
                y={(p) => geo.y(p.pnl_per_lot)}
                stroke={color}
                strokeWidth={2}
              />
              {candidate.breakevens.map((b) => (
                <circle key={b} cx={geo.x(b)} cy={zeroY} r={4} fill="var(--card)" stroke={color} strokeWidth={2}>
                  <title>Breakeven {formatNumber(b, { decimals: 2 })}</title>
                </circle>
              ))}
            </g>
          );
        })}
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {candidates.map((candidate, i) => (
          <span key={candidate.id} className="inline-flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4" style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }} />
            {label(STRATEGY_LABEL, candidate.strategy)} · {candidate.expiry}
          </span>
        ))}
        <span>x: underlying at expiry · y: P&amp;L per lot (₹) · circles: breakevens</span>
      </figcaption>
    </figure>
  );
}
