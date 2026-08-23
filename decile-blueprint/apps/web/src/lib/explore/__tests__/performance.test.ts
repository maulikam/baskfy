import { describe, expect, it } from "vitest";

import {
  performanceSeriesFromMetrics,
  resolveBasketPerformanceSeries,
} from "@/lib/explore/performance";
import type { ExploreMetrics } from "@/lib/explore/fetch";

const METRICS: ExploreMetrics = {
  as_of_date: "2026-08-22",
  min_amount: "5000",
  volatility_bucket: "MED",
  volatility_value: "0.18",
  ret_1m: "0.02",
  ret_6m: "0.08",
  ret_1y: "0.15",
  cagr_3y: "0.12",
  cagr_5y: null,
  since_inception_pct: "0.22",
  headline_label: "1Y",
  headline_pct: "0.15",
  // Every catalog return is a price return (M39.3). Required on the type so a surface
  // cannot render a number without the sentence that qualifies it.
  return_convention: "PRICE_RETURN",
  dividends_included: false,
  return_convention_note:
    "Returns are price returns computed from split- and bonus-adjusted closes.",
};

describe("performanceSeriesFromMetrics", () => {
  it("chains ret windows into ≥2 PerformancePoints", () => {
    const series = performanceSeriesFromMetrics(METRICS);
    expect(series.length).toBeGreaterThanOrEqual(2);
    expect(series[0]?.basket).toBe(100);
    expect(series.every((p) => typeof p.date === "string" && p.date.length === 10)).toBe(true);
  });

  it("returns empty when metrics or as_of missing", () => {
    expect(performanceSeriesFromMetrics(null)).toEqual([]);
    expect(performanceSeriesFromMetrics({ ...METRICS, as_of_date: null })).toEqual([]);
  });
});

describe("resolveBasketPerformanceSeries", () => {
  it("falls back to metrics when series endpoint is empty", async () => {
    const resolved = await resolveBasketPerformanceSeries("any-slug", METRICS);
    expect(resolved.source).toBe("metrics");
    expect(resolved.points.length).toBeGreaterThanOrEqual(2);
  });
});
