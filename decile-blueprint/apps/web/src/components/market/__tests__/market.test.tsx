import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MarketHealthPointOut } from "@baskfy/api-client";

import { BreadthGauge } from "@/components/market/breadth-gauge";
import { BreadthHistory } from "@/components/market/breadth-history";
import { EMPTY_CELL } from "@/lib/format";
import { HEALTH_UNIVERSES } from "@/lib/market/universes";

/**
 * The market-health primitives.
 *
 * The numbers themselves are the API's, and `services/api/tests/test_api_market_data.py` checks
 * them against a hand computation. What this suite checks is the rendering rules docs/01 §6 and
 * docs/11 §Accessibility impose: an unknown breadth is an em dash and never a zeroed arc, the arc
 * is never the only carrier of the value, and one day of history is not drawn as a line.
 */

function point(date: string, breadth: number | null, level: number | null): MarketHealthPointOut {
  return {
    date,
    pct_above_200dma: breadth,
    pct_above_50dma: breadth,
    pct_within_10pct_ath: breadth,
    pct_ret_1y_positive: breadth,
    constituent_count: 50,
    index_level: level,
  };
}

describe("breadth gauge", () => {
  it("states the value as text, not only as an arc", () => {
    render(<BreadthGauge label="Above 200 DMA" value={57.7} />);
    expect(screen.getByText("57.7%")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Above 200 DMA: 57.7%" })).toBeInTheDocument();
  });

  it("shows an em dash for an unknown breadth, never a zeroed arc", () => {
    render(<BreadthGauge label="Above 50 DMA" value={null} />);
    expect(screen.getByText(EMPTY_CELL)).toBeInTheDocument();
    expect(screen.queryByText("0.0%")).toBeNull();
    expect(screen.getByRole("img", { name: "Above 50 DMA: no data" })).toBeInTheDocument();
  });

  it("renders docs/01 §6's own wording", () => {
    render(<BreadthGauge label="Within 10% of ATH" value={17.6} />);
    expect(screen.getByText("Within 10% of ATH")).toBeInTheDocument();
  });
});

describe("breadth history", () => {
  const series = { key: "pct_above_200dma", label: "Above 200 DMA" } as const;

  it("draws the series with the universe's own index level overlaid", () => {
    render(
      <BreadthHistory
        points={[point("2026-08-17", 55, 1000), point("2026-08-18", 58, 1010)]}
        series={series}
        universeName="NIFTY 500"
      />,
    );
    const figure = screen.getByRole("figure");
    expect(within(figure).getByText(/NIFTY 500 level/)).toBeInTheDocument();
    expect(within(figure).getByRole("img")).toBeInTheDocument();
  });

  it("says so rather than drawing a line through one point", () => {
    render(
      <BreadthHistory
        points={[point("2026-08-18", 58, 1010)]}
        series={series}
        universeName="NIFTY 500"
      />,
    );
    expect(screen.getByText(/only 1 day of history/i)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("says so rather than drawing an empty chart", () => {
    render(<BreadthHistory points={[]} series={series} universeName="NIFTY 500" />);
    expect(screen.getByText(/no breadth has been recorded/i)).toBeInTheDocument();
  });

  it("describes the series in words for a screen reader", () => {
    render(
      <BreadthHistory
        points={[point("2026-08-17", 55, 1000), point("2026-08-18", 58, 1010)]}
        series={series}
        universeName="NIFTY 500"
      />,
    );
    const chart = screen.getByRole("img");
    expect(chart).toHaveAccessibleName(/Above 200 DMA for NIFTY 500/);
    expect(chart).toHaveAccessibleName(/index level overlaid/);
  });
});

describe("the universe selector", () => {
  it("offers docs/01 §6's twelve, in its order", () => {
    // The Python side asserts this against `baskfy_core.universes`; this is the shape check the
    // page depends on.
    expect(HEALTH_UNIVERSES).toHaveLength(12);
    expect(HEALTH_UNIVERSES[0]?.slug).toBe("nifty-allcap");
    expect(HEALTH_UNIVERSES.map((option) => option.slug)).not.toContain("etf");
  });
});
