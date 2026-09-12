import type { PerformancePoint } from "@/components/explore/performance-chart";
import { serverApiOrigin } from "@/lib/api/config";
import type { ExploreMetrics } from "@/lib/explore/fetch";

/**
 * Basket performance series for `/basket/[slug]`.
 *
 * Prefers `GET /api/v1/explore/{slug}/performance` — covered NAV points only, plus a coverage
 * fraction. Does **not** invent a two-point line from return windows: that is what made a +48%
 * basket look like a five-fold climb (audit §1.5).
 */

export interface ExplorePerformanceSeries {
  points: PerformancePoint[];
  source: "api" | "empty";
  /** 0–1 fraction of trading days with full constituent coverage; null when unknown. */
  coverage: number | null;
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

function mapApiPoints(raw: unknown): { points: PerformancePoint[]; coverage: number | null } {
  if (!raw || typeof raw !== "object") return { points: [], coverage: null };
  const body = raw as {
    series?: ApiSeriesPoint[];
    points?: ApiSeriesPoint[];
    coverage?: number | string | null;
  };
  const rows =
    body.series ?? body.points ?? (Array.isArray(raw) ? (raw as ApiSeriesPoint[]) : []);
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
  return {
    points: out.length >= 2 ? out : [],
    coverage: asNumber(body.coverage ?? null),
  };
}

/** Attempt series endpoint; never throws — empty on any failure. */
export async function fetchExplorePerformanceSeries(
  slug: string,
): Promise<{ points: PerformancePoint[]; coverage: number | null }> {
  try {
    const response = await fetch(
      `${serverApiOrigin()}/api/v1/explore/${encodeURIComponent(slug)}/performance`,
      { cache: "no-store" },
    );
    if (!response.ok) return { points: [], coverage: null };
    return mapApiPoints(await response.json());
  } catch {
    return { points: [], coverage: null };
  }
}

/**
 * Resolve chart series from the API only. Metrics-window chaining is retired: it fabricated
 * paths that disagreed with the card's own 1Y return.
 */
export async function resolveBasketPerformanceSeries(
  slug: string,
  _metrics?: ExploreMetrics | null | undefined,
): Promise<ExplorePerformanceSeries> {
  const fromApi = await fetchExplorePerformanceSeries(slug);
  if (fromApi.points.length >= 2) {
    return { points: fromApi.points, source: "api", coverage: fromApi.coverage };
  }
  return { points: [], source: "empty", coverage: fromApi.coverage };
}
