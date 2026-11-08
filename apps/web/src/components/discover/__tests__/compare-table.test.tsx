import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompareTable } from "@/components/discover/compare-table";
import { UNCOMPUTED_METRIC_KEYS } from "@/lib/discover/metrics";
import type { HoldingSet } from "@/lib/discover/overlap";
import type { ExploreBasketCard, ExploreMetrics } from "@/lib/explore/fetch";

function metrics(over: Partial<ExploreMetrics> = {}): ExploreMetrics {
  return {
    as_of_date: "2026-08-22",
    min_amount: "300000.00",
    volatility_bucket: "MED",
    volatility_value: "0.1820000000",
    ret_1m: "1.10",
    ret_6m: "9.40",
    ret_1y: "22.10",
    cagr_3y: null,
    cagr_5y: null,
    since_inception_pct: null,
    headline_label: "1Y returns",
    headline_pct: "22.10",
    return_convention: "PRICE_RETURN",
    dividends_included: false,
    return_convention_note: "Price return.",
    ...over,
  };
}

function card(slug: string, over: Partial<ExploreBasketCard> = {}): ExploreBasketCard {
  return {
    slug,
    name: slug.replace(/-/g, " "),
    access: "FREE",
    visibility: "PUBLISHED",
    type: "STOCK",
    categories: ["momentum"],
    rebalance_frequency: "QUARTERLY",
    source: "SCAN",
    description_md: null,
    launched_at: "2024-01-01",
    manager: { slug: "baskfy-engine", name: "Baskfy Engine", kind: "ENGINE" },
    metrics: metrics(),
    ...over,
  };
}

const two = [card("alpha"), card("beta", { rebalance_frequency: "MONTHLY" })];

describe("the table is like for like", () => {
  it("says which window every return is measured over", () => {
    render(<CompareTable baskets={two} />);
    expect(screen.getByTestId("window-note").textContent).toMatch(
      /measured over 1Y return, the same window for all/,
    );
  });

  it("names the shortening when one basket has more history than another", () => {
    const older = card("older", { metrics: metrics({ cagr_5y: "18.00" }) });
    render(<CompareTable baskets={[older, card("younger")]} />);
    expect(screen.getByTestId("window-note").textContent).toMatch(/would flatter it/);
  });

  it("puts one column per basket, in the order they were selected", () => {
    render(<CompareTable baskets={two} />);
    const headers = screen.getAllByRole("columnheader").map((node) => node.textContent);
    expect(headers[1]).toContain("alpha");
    expect(headers[2]).toContain("beta");
  });

  it("gives the table an accessible caption naming what is being compared", () => {
    render(<CompareTable baskets={two} />);
    expect(screen.getByRole("table").querySelector("caption")?.textContent).toMatch(
      /alpha, beta compared over 1Y return/,
    );
  });
});

describe("a metric nobody computes reads as absent, not as zero", () => {
  it("keeps a visible row for each one", () => {
    render(<CompareTable baskets={two} />);
    for (const key of UNCOMPUTED_METRIC_KEYS) {
      const row = document.querySelector(`[data-row="${key}"]`);
      expect(row, `${key} still has a row`).not.toBeNull();
      expect(row).toHaveAttribute("data-available", "false");
      expect(within(row as HTMLElement).getAllByText("—").length).toBe(2);
    }
  });

  it("announces the absence to a screen reader rather than reading a bare dash", () => {
    render(<CompareTable baskets={two} />);
    const row = document.querySelector('[data-row="max_drawdown"]') as HTMLElement;
    expect(within(row).getAllByText("— not computed yet", { exact: false }).length).toBe(2);
  });

  it("still groups those rows under the section a reader would look in", () => {
    render(<CompareTable baskets={two} />);
    const sections = screen
      .getAllByTestId("compare-section")
      .map((node) => node.dataset.section);
    expect(sections).toContain("Downside");
    expect(sections).toContain("Operations");
  });
});

describe("what is different", () => {
  it("lists the rows where the baskets actually disagree", () => {
    render(<CompareTable baskets={two} />);
    const panel = screen.getByTestId("differences");
    expect(within(panel).getByText(/Rebalance schedule/)).toBeInTheDocument();
  });

  it("is absent when two identical baskets are compared", () => {
    render(<CompareTable baskets={[card("alpha"), card("alpha")]} />);
    expect(screen.queryByTestId("differences")).not.toBeInTheDocument();
  });
});

describe("portfolio overlap", () => {
  const holdings: HoldingSet[] = [
    { slug: "alpha", name: "alpha", symbols: ["RELIANCE", "TCS", "INFY"] },
    { slug: "beta", name: "beta", symbols: ["TCS", "INFY", "HDFCBANK"] },
  ];

  it("says how much the baskets hold in common", () => {
    render(<CompareTable baskets={two} holdings={holdings} />);
    expect(screen.getByTestId("overlap-line").textContent).toMatch(/share 2 stocks/);
  });

  it("warns when two baskets are largely the same bet", () => {
    const same: HoldingSet[] = [
      { slug: "alpha", name: "alpha", symbols: ["A", "B", "C"] },
      { slug: "beta", name: "beta", symbols: ["A", "B", "C"] },
    ];
    render(<CompareTable baskets={two} holdings={same} />);
    expect(screen.getByTestId("overlap-warning").textContent).toMatch(/concentrates/);
  });

  it("does not warn when they barely overlap", () => {
    const apart: HoldingSet[] = [
      { slug: "alpha", name: "alpha", symbols: ["A", "B", "C"] },
      { slug: "beta", name: "beta", symbols: ["X", "Y", "Z"] },
    ];
    render(<CompareTable baskets={two} holdings={apart} />);
    expect(screen.queryByTestId("overlap-warning")).not.toBeInTheDocument();
  });

  it("marks an unreadable pair rather than reporting no overlap", () => {
    const unreadable: HoldingSet[] = [
      { slug: "alpha", name: "alpha", symbols: ["A"] },
      { slug: "beta", name: "beta", symbols: null },
    ];
    render(<CompareTable baskets={two} holdings={unreadable} />);
    const line = screen.getByTestId("overlap-line");
    expect(line).toHaveAttribute("data-available", "false");
    expect(line.textContent).toMatch(/not the same as "no overlap"/);
  });

  it("omits the panel entirely when no holdings were supplied", () => {
    render(<CompareTable baskets={two} />);
    expect(screen.queryByTestId("overlap-panel")).not.toBeInTheDocument();
  });
});

describe("every measure explains itself", () => {
  it("gives each row a disclosure a reader can open", () => {
    render(<CompareTable baskets={two} />);
    const row = document.querySelector('[data-row="volatility"]') as HTMLElement;
    expect(within(row).getByRole("group")).toBeInTheDocument();
    expect(within(row).getByText(/bigger swings in both directions/)).toBeInTheDocument();
  });
});
