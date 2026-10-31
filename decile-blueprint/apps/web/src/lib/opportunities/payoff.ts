import type { PayoffPoint } from "@/lib/opportunities/types";

export interface PayoffSeries {
  id: string;
  points: readonly PayoffPoint[];
  breakevens: readonly number[];
}

export interface Margin {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface PayoffGeometry {
  xDomain: [number, number];
  yDomain: [number, number];
  x: (value: number) => number;
  y: (value: number) => number;
  innerWidth: number;
  innerHeight: number;
}

export function linearScale(
  domain: readonly [number, number],
  range: readonly [number, number],
): (value: number) => number {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0;
  if (span === 0) return () => (r0 + r1) / 2;
  return (value: number) => r0 + ((value - d0) / span) * (r1 - r0);
}

function extent(values: readonly number[]): [number, number] | null {
  const finite = values.filter((value) => Number.isFinite(value));
  if (finite.length === 0) return null;
  return [Math.min(...finite), Math.max(...finite)];
}

export function payoffDomains(
  series: readonly PayoffSeries[],
  spot: number | null,
  pad = 0.08,
): { xDomain: [number, number]; yDomain: [number, number] } | null {
  const xs: number[] = [];
  const ys: number[] = [0];
  for (const s of series) {
    for (const point of s.points) {
      xs.push(point.underlying);
      ys.push(point.pnl_per_lot);
    }
    xs.push(...s.breakevens);
  }
  if (spot !== null) xs.push(spot);
  const xExtent = extent(xs);
  const yExtent = extent(ys);
  if (!xExtent || !yExtent || series.every((s) => s.points.length === 0)) return null;
  const ySpan = yExtent[1] - yExtent[0] || Math.max(1, Math.abs(yExtent[0]));
  return {
    xDomain: xExtent,
    yDomain: [yExtent[0] - ySpan * pad, yExtent[1] + ySpan * pad],
  };
}

export function payoffGeometry(
  series: readonly PayoffSeries[],
  spot: number | null,
  width: number,
  height: number,
  margin: Margin,
): PayoffGeometry | null {
  const domains = payoffDomains(series, spot);
  if (!domains) return null;
  const innerWidth = Math.max(1, width - margin.left - margin.right);
  const innerHeight = Math.max(1, height - margin.top - margin.bottom);
  return {
    ...domains,
    innerWidth,
    innerHeight,
    x: linearScale(domains.xDomain, [margin.left, margin.left + innerWidth]),
    y: linearScale(domains.yDomain, [margin.top + innerHeight, margin.top]),
  };
}

export function ticks(domain: readonly [number, number], count = 5): number[] {
  const [lo, hi] = domain;
  if (count < 2 || hi === lo) return [lo];
  const step = (hi - lo) / (count - 1);
  return Array.from({ length: count }, (_, i) => lo + step * i);
}

export function ladderPositions(
  invalidation: number,
  entry: number,
  targets: readonly number[],
): { invalidation: number; entry: number; targets: number[] } {
  const all = [invalidation, entry, ...targets];
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const scale = linearScale([lo, hi], [0, 100]);
  return {
    invalidation: scale(invalidation),
    entry: scale(entry),
    targets: targets.map((t) => scale(t)),
  };
}
