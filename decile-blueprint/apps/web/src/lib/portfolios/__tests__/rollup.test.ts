import type { PortfolioRollupOut } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import {
  checkTotals,
  consolidate,
  describeCoverage,
  type BrokerListOut,
} from "@/lib/portfolios/rollup";

/**
 * The consolidated view must never present a total that quietly omits nine brokers.
 *
 * Two separate properties, asserted separately because they fail separately:
 *
 * 1. The arithmetic reconciles, checked rather than assumed, and exactly — the wire invariant is
 *    `total == sum(by_broker) + unattributed` to the last digit.
 * 2. The **coverage** is stated: which accounts are in the figure, and which brokers cannot put
 *    anything into it because they have no holdings adapter.
 */

function line(id: number, label: string, cost: string, quantity = "10", holdings = 1) {
  return {
    broker_account_id: id,
    broker_id: "zerodha",
    label,
    totals: { cost, quantity, holdings },
  };
}

function rollup(partial: Partial<PortfolioRollupOut> = {}): PortfolioRollupOut {
  return {
    portfolio_id: 1,
    by_broker: [line(7, "Zerodha · main", "100000.5000"), line(8, "Zerodha · family", "50000.2500")],
    declaration_conflicts: false,
    declared_broker_account_id: null,
    rows: [],
    spans_brokers: true,
    subtree_portfolio_ids: [1, 2, 3],
    total: { cost: "150000.7500", quantity: "20", holdings: 2 },
    unattributed: { cost: "0", quantity: "0", holdings: 0 },
    ...partial,
  };
}

const CATALOG: BrokerListOut = {
  adapters_wired: 1,
  gate: {
    decision_reference: "D3",
    live_oauth_enabled: false,
    requirement: "counsel sign-off",
    signed_off: false,
  },
  brokers: [
    {
      id: "zerodha",
      name: "Zerodha",
      short_name: "Zerodha",
      mark: "Z",
      color: "#387ed1",
      blurb: "",
      api_name: "Kite Connect",
      docs_url: "",
      sort_order: 1,
      connected: true,
      connection_status: "connected",
      adapter_wired: true,
      capabilities: { oauth: "ready", holdings_sync: "ready", trading: "planned" },
    },
    {
      id: "kotak",
      name: "Kotak Neo",
      short_name: "Kotak",
      mark: "K",
      color: "#ed1c24",
      blurb: "",
      api_name: "Neo API",
      docs_url: "",
      sort_order: 2,
      connected: false,
      connection_status: "not_connected",
      adapter_wired: false,
      capabilities: { oauth: "planned", holdings_sync: "planned", trading: "planned" },
    },
    {
      id: "icici",
      name: "ICICI Direct",
      short_name: "ICICI",
      mark: "I",
      color: "#f26522",
      blurb: "",
      api_name: "Breeze",
      docs_url: "",
      sort_order: 3,
      connected: false,
      connection_status: "not_connected",
      adapter_wired: false,
      capabilities: { oauth: "planned", holdings_sync: "planned", trading: "planned" },
    },
  ],
};

describe("the total is checked, not trusted", () => {
  it("reconciles broker lines plus unattributed against the total, exactly", () => {
    const invariant = checkTotals(rollup());
    expect(invariant.holds).toBe(true);
    expect(invariant.sum).toBe("150000.7500");
  });

  it("reconciles at a size where a float would already have drifted", () => {
    const invariant = checkTotals(
      rollup({
        by_broker: [line(7, "a", "9007199254740992.01"), line(8, "b", "0.02")],
        total: { cost: "9007199254740992.03", quantity: "20", holdings: 2 },
      }),
    );
    expect(invariant.holds).toBe(true);
  });

  it("reports a total that does not add up rather than swallowing it", () => {
    const invariant = checkTotals(
      rollup({ total: { cost: "150001.0000", quantity: "20", holdings: 2 } }),
    );
    expect(invariant.holds).toBe(false);
    expect(invariant.sum).toBe("150000.7500");
    expect(invariant.total).toBe("150001.0000");
  });

  it("counts the unattributed line into the sum rather than ignoring it", () => {
    const invariant = checkTotals(
      rollup({
        unattributed: { cost: "25.2500", quantity: "1", holdings: 1 },
        total: { cost: "150026.0000", quantity: "21", holdings: 3 },
      }),
    );
    expect(invariant.holds).toBe(true);
  });
});

describe("the consolidated view names which brokers it covers", () => {
  it("names every broker that cannot sync, so the total is not read as everything", () => {
    const coverage = describeCoverage(rollup(), CATALOG);
    expect(coverage.canSync.map((broker) => broker.id)).toEqual(["zerodha"]);
    expect(coverage.cannotSync.map((broker) => broker.id)).toEqual(["kotak", "icici"]);
    expect(coverage.sentence).toContain("Kotak Neo");
    expect(coverage.sentence).toContain("ICICI Direct");
    expect(coverage.sentence).toContain("Zerodha");
    expect(coverage.unknown).toBe(false);
  });

  it("names the accounts that did contribute", () => {
    const coverage = describeCoverage(rollup(), CATALOG);
    expect(coverage.contributing.map((entry) => entry.label)).toEqual([
      "Zerodha · main",
      "Zerodha · family",
    ]);
    expect(coverage.sentence).toMatch(/Held at 2 broker accounts/);
  });

  it("says coverage is unknown when the broker catalog could not be read", () => {
    const coverage = describeCoverage(rollup(), null);
    expect(coverage.unknown).toBe(true);
    expect(coverage.sentence).toMatch(/could not be read/);
    // Never a sentence that implies the figure is complete.
    expect(coverage.sentence).not.toMatch(/only broker/);
  });

  it("says so plainly when the portfolio holds nothing yet", () => {
    const coverage = describeCoverage(rollup({ by_broker: [] }), CATALOG);
    expect(coverage.sentence).toMatch(/No broker account has holdings in this portfolio/);
  });
});

describe("consolidate assembles the panel's whole view model", () => {
  it("orders broker lines by money put in, biggest first", () => {
    const view = consolidate({
      rollup: rollup({
        by_broker: [line(7, "small", "1.00"), line(8, "big", "999.00")],
        total: { cost: "1000.00", quantity: "20", holdings: 2 },
      }),
      catalog: CATALOG,
    });
    expect(view.lines.map((entry) => entry.label)).toEqual(["big", "small"]);
  });

  it("adds an unattributed line only when there is something in it", () => {
    expect(consolidate({ rollup: rollup() }).lines.some((entry) => entry.unattributed)).toBe(false);

    const withOrphanRows = consolidate({
      rollup: rollup({
        unattributed: { cost: "25.2500", quantity: "1", holdings: 1 },
        total: { cost: "150026.0000", quantity: "21", holdings: 3 },
      }),
    });
    const unattributed = withOrphanRows.lines.find((entry) => entry.unattributed);
    expect(unattributed?.brokerAccountId).toBeNull();
    expect(unattributed?.label).toBe("Not attributed to a broker");
  });

  it("keeps money as the exact strings the API sent, never as numbers", () => {
    const view = consolidate({ rollup: rollup() });
    for (const entry of view.lines) {
      expect(typeof entry.cost).toBe("string");
      expect(typeof entry.quantity).toBe("string");
    }
    expect(view.lines[0]?.cost).toBe("100000.5000");
  });

  it("carries the subtree size, the span and any declaration conflict through", () => {
    const view = consolidate({ rollup: rollup({ declaration_conflicts: true }) });
    expect(view.subtreeSize).toBe(3);
    expect(view.spansBrokers).toBe(true);
    expect(view.declarationConflicts).toBe(true);
  });
});
