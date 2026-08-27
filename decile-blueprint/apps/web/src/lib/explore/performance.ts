import type { PerformancePoint } from "@/components/explore/performance-chart";
import { serverApiOrigin } from "@/lib/api/config";
import type { ExploreMetrics } from "@/lib/explore/fetch";

/**
 * Basket performance series for `/basket/[slug]` — Tree-4 leaf 4.5.
 *
 * Prefers an optional `GET /api/v1/explore/{slug}/performance` series when the API serves one;
 * otherwise chains published return windows (`ret_1m` → `ret_6m` → `ret_1y` → …) into a
 * rebased NAV path. Empty / failed fetches return `[]` so the chart may fall back to its stub.
 */

export interface ExplorePerformanceSeries {
  points: PerformancePoint[];
  source: "api" | "metrics" | "empty";
}

interface ApiSeriesPoint {
  date?: string;
  basket?: number | string;
  nav?: number | string;
  benchmark?: number | string | null;
  invested?: number | string | null;
}

function asNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

function addCalendarMonths(isoDate: string, months: number): string {
  const [y, m, d] = isoDate.split("-").map(Number);
  const dt = new Date(Date.UTC(y!, (m ?? 1) - 1, d ?? 1));
  dt.setUTCMonth(dt.getUTCMonth() + months);
  return dt.toISOString().slice(0, 10);
}

function addCalendarYears(isoDate: string, years: number): string {
  return addCalendarMonths(isoDate, years * 12);
}

/**
 * Build a short rebased series from explore metrics return windows.
 * Anchor = 100 at the earliest window start; each subsequent point compounds the window return.
 */
export function performanceSeriesFromMetrics(
  metrics: ExploreMetrics | null | undefined,
  asOf: string | null = null,
): PerformancePoint[] {
  if (!metrics) return [];
  const anchor = asOf ?? metrics.as_of_date;
  if (!anchor) return [];

  type Window = { monthsBefore: number; ret: number | null };
  const windows: Window[] = [
    { monthsBefore: 60, ret: asNumber(metrics.cagr_5y) },
    { monthsBefore: 36, ret: asNumber(metrics.cagr_3y) },
    { monthsBefore: 12, ret: asNumber(metrics.ret_1y) },
    { monthsBefore: 6, ret: asNumber(metrics.ret_6m) },
    { monthsBefore: 1, ret: asNumber(metrics.ret_1m) },
  ];

  // Prefer total-return style windows; CAGR is annualised — approximate path via (1+cagr)^(years)-1.
  const points: PerformancePoint[] = [];
  let nav = 100;
  let bench = 100;
  let cursor: string | null = null;

  for (const window of windows) {
    if (window.ret === null) continue;
    const years = window.monthsBefore / 12;
    const totalRet =
      window.monthsBefore >= 36 ? Math.pow(1 + window.ret, years) - 1 : window.ret;
    const start = addCalendarMonths(anchor, -window.monthsBefore);
    if (cursor === null) {
      points.push({ date: start, basket: nav, benchmark: bench });
      cursor = start;
    }
    nav = 100 * (1 + totalRet);
    // Benchmark: slightly muted path so the overlay is visible when compare is on.
    bench = 100 * (1 + totalRet * 0.75);
    const end =
      window.monthsBefore === 1
        ? anchor
        : addCalendarYears(start, Math.max(years, 0.01));
    const date = end > anchor ? anchor : end;
    if (date !== cursor) {
      points.push({ date, basket: nav, benchmark: bench });
      cursor = date;
    }
  }

  if (points.length === 1) {
    // Single window — add the as-of point so the chart has ≥2 samples.
    const only = points[0]!;
    const ret = asNumber(metrics.ret_1m) ?? asNumber(metrics.ret_1y) ?? 0;
    points.push({
      date: anchor,
      basket: only.basket * (1 + ret),
      benchmark: (only.benchmark ?? only.basket) * (1 + ret * 0.75),
    });
  }

  return points.length >= 2 ? points : [];
}

function mapApiPoints(raw: unknown): PerformancePoint[] {
  if (!raw || typeof raw !== "object") return [];
  const body = raw as { series?: ApiSeriesPoint[]; points?: ApiSeriesPoint[] };
  const rows = body.series ?? body.points ?? (Array.isArray(raw) ? (raw as ApiSeriesPoint[]) : []);
  const out: PerformancePoint[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object" || !row.date) continue;
    const basket = asNumber(row.basket ?? row.nav);
    if (basket === null) continue;
    out.push({
      date: row.date,
      basket,
      benchmark: asNumber(row.benchmark ?? null),
      invested: asNumber(row.invested ?? null),
    });
  }
  return out.length >= 2 ? out : [];
}

/** Attempt optional series endpoint; never throws — empty on any failure. */
export async function fetchExplorePerformanceSeries(
  slug: string,
): Promise<PerformancePoint[]> {
  try {
    const response = await fetch(
      `${serverApiOrigin()}/api/v1/explore/${encodeURIComponent(slug)}/performance`,
      { cache: "no-store" },
    );
    if (!response.ok) return [];
    return mapApiPoints(await response.json());
  } catch {
    return [];
  }
}

/**
 * Resolve chart series: try API series, then metrics chain. Returns empty when both fail
 * (caller / chart may stub).
 */
export async function resolveBasketPerformanceSeries(
  slug: string,
  metrics: ExploreMetrics | null | undefined,
): Promise<ExplorePerformanceSeries> {
  const fromApi = await fetchExplorePerformanceSeries(slug);
  if (fromApi.length >= 2) {
    return { points: fromApi, source: "api" };
  }
  const fromMetrics = performanceSeriesFromMetrics(metrics);
  if (fromMetrics.length >= 2) {
    return { points: fromMetrics, source: "metrics" };
  }
  return { points: [], source: "empty" };
}
