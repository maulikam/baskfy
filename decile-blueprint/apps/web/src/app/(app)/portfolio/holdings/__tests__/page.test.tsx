import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import PortfolioHoldingsPage from "../page";

/**
 * Audit 1.6: the holdings page listed only unallocated rows while the blurb promised every share.
 * Allocated holdings must appear grouped by portfolio.
 */
vi.mock("@/lib/brokers/fetch", () => ({
  fetchBrokerCatalog: vi.fn(async () => ({ brokers: [{ id: "zerodha", connected: true }] })),
}));

vi.mock("@/lib/portfolio/fetch", () => ({
  fetchPortfolioOverview: vi.fn(async () => ({
    sync_summary: "Holdings synced: 2026-09-11",
  live_overlay: false,
    portfolios: [{ portfolio_id: 1, name: "Bonds", kind: "CAPITAL" }],
    monitoring_views: [],
    unallocated: {
      cash: "0",
      holdings_count: 0,
      holdings_value: "0",
      total_value: "0",
      cta: "Organize",
    },
  })),
  fetchPortfolioHoldings: vi.fn(async () => ({
    prices_label: "Prices: close of 2026-09-11",
    holdings_synced_label: "Holdings synced: 2026-09-11",
    rows: [
      {
        instrument: { instrument_id: 1, symbol: "LOOSE", name: "Loose" },
        quantity: "10",
        value: "1000.00",
        allocated: false,
        split_across_portfolios: false,
        allocation: null,
        brokers: [],
        monitoring_views: [],
        pending_reconciliation: false,
      },
      {
        instrument: { instrument_id: 2, symbol: "BOND1", name: "Bond One" },
        quantity: "5",
        value: "5000.00",
        allocated: true,
        split_across_portfolios: false,
        allocation: { portfolio_id: 1, name: "Bonds", kind: "CAPITAL" },
        brokers: [],
        monitoring_views: [],
        pending_reconciliation: false,
      },
    ],
  })),
  fetchGroupingSuggestions: vi.fn(async () => ({
    suggestions: [],
    unavailableReason: null,
    sectors: {},
  })),
}));

vi.mock("@/app/actions/portfolio", () => ({
  createPortfolioAction: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/portfolio/holdings",
}));

describe("holdings by portfolio", () => {
  it("lists allocated holdings grouped by portfolio name", async () => {
    render(await PortfolioHoldingsPage());
    const section = screen.getByTestId("holdings-by-portfolio");
    expect(section).toHaveTextContent("Bonds");
    expect(section).toHaveTextContent("BOND1");
    expect(section).not.toHaveTextContent("LOOSE");
  });
});
