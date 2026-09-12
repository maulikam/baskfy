import { describe, expect, it, vi } from "vitest";

import { resolveBasketPerformanceSeries } from "@/lib/explore/performance";
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
  return_convention: "PRICE_RETURN",
  dividends_included: false,
  return_convention_note:
    "Returns are price returns computed from split- and bonus-adjusted closes.",
};

describe("resolveBasketPerformanceSeries", () => {
  it("does not invent a metrics path when the series endpoint is empty (audit §1.5)", async () => {
    /* The old fallback chained ret_1m/6m/1y into two points and drew a straight line that
       disagreed with the card's own 1Y return. Empty API → empty series, never a fabrication. */
    const resolved = await resolveBasketPerformanceSeries("any-slug", METRICS);
    expect(resolved.source).toBe("empty");
    expect(resolved.points).toEqual([]);
  });

  it("maps covered API points and coverage", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            slug: "any-slug",
            coverage: "0.9200",
            points: [
              { date: "2025-09-01", basket: "100.00" },
              { date: "2026-09-01", basket: "148.00" },
            ],
          }),
      }),
    );
    const resolved = await resolveBasketPerformanceSeries("any-slug", METRICS);
    expect(resolved.source).toBe("api");
    expect(resolved.points).toHaveLength(2);
    expect(resolved.points[1]?.basket).toBe(148);
    expect(resolved.coverage).toBeCloseTo(0.92);
    vi.unstubAllGlobals();
  });
});
