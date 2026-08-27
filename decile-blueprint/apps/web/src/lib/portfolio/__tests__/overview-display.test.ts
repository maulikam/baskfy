import { describe, expect, it } from "vitest";

import {
  HOLDING_GROUP_ROW,
  MONITORING_ROW,
  OVERVIEW,
  SUBSCRIBED_ROW,
} from "@/components/portfolio/__tests__/overview-fixture";
import {
  attentionLink,
  describeBrokers,
  describeReturn,
  formatMoney,
  formatMoneyMove,
  formatRate,
  fromFigure,
  pricesLine,
  rowsForTab,
  syncedLine,
  tabExcludedFromTotals,
  totalOfRows,
} from "@/lib/portfolio/overview";

/**
 * The presentation rules `PORTFOLIO_REDESIGN.md` leaves to the browser, asserted against the spec.
 *
 * These are the three the components cannot be trusted to enforce on their own: the units a
 * fraction is displayed in, the arithmetic that must exclude a monitoring view, and the sentence
 * criterion 3 requires behind every rate.
 */

describe("units — a stored ratio becomes a percentage exactly once", () => {
  it("renders a fraction as a signed percentage", () => {
    expect(formatRate("0.1842")).toBe("+18.42%");
    expect(formatRate("-0.0038")).toBe("-0.38%");
    expect(formatRate("0")).toBe("0.00%");
  });

  it("renders nothing rather than a zero when there is no value", () => {
    expect(formatRate(null)).toBe("—");
    expect(formatMoney(null)).toBe("—");
    expect(formatMoneyMove(null)).toBe("—");
  });

  it("masks money when amounts are hidden, and never leaks the digits", () => {
    expect(formatMoney("702500.00")).toBe("₹7,02,500");
    expect(formatMoney("702500.00", false)).toBe("••••••");
    expect(formatMoneyMove("-840", false)).toBe("••••");
  });

  it("signs a money move so colour is never the only cue", () => {
    expect(formatMoneyMove("3120.00")).toBe("+₹3,120");
    expect(formatMoneyMove("-840.00")).toBe("−₹840");
  });
});

describe("§11 criterion 3 — the sentence behind every rate", () => {
  it("names the metric and the date it runs from", () => {
    const text = describeReturn(fromFigure(SUBSCRIBED_ROW.headline_return));
    expect(text).toContain("TWR since you subscribed");
    expect(text).toContain("Measured since 1 Apr 2025");
  });

  it("says why a figure is missing instead of leaving an empty cell", () => {
    const text = describeReturn(fromFigure(HOLDING_GROUP_ROW.headline_return));
    expect(text).toContain("Since grouped");
    expect(text).toContain("Not shown:");
    expect(text).toContain("Import your account statement");
  });

  it("marks a publisher's record as not the reader's own (§11 criterion 5)", () => {
    const model = SUBSCRIBED_ROW.model_return;
    expect(model).toBeTruthy();
    const text = describeReturn(fromFigure(model!));
    expect(text).toContain("not your money and not your return");
  });
});

describe("§4.1 — a monitoring view is excluded from every total", () => {
  it("never appears under All, or under a source tab", () => {
    for (const tab of ["ALL", "SUBSCRIBED", "MY_SCREENS", "MY_STRATEGIES", "HOLDING_GROUPS"] as const) {
      expect(rowsForTab(OVERVIEW, tab)).not.toContainEqual(MONITORING_ROW);
    }
    expect(rowsForTab(OVERVIEW, "MONITORING")).toEqual([MONITORING_ROW]);
  });

  it("sums the capital rows and nothing else", () => {
    expect(totalOfRows(rowsForTab(OVERVIEW, "ALL"))).toBe(700000);
    expect(totalOfRows([MONITORING_ROW])).toBeNull();
    expect(totalOfRows([SUBSCRIBED_ROW, MONITORING_ROW])).toBe(480000);
  });

  it("knows when a list may not be added up at all", () => {
    expect(tabExcludedFromTotals([MONITORING_ROW])).toBe(true);
    expect(tabExcludedFromTotals([SUBSCRIBED_ROW])).toBe(false);
    expect(tabExcludedFromTotals([])).toBe(false);
  });
});

describe("§8 — the renamed wording", () => {
  it("counts brokers instead of saying a portfolio spreads across them", () => {
    expect(describeBrokers(SUBSCRIBED_ROW)).toBe("Connected to 2 brokers");
    expect(describeBrokers(HOLDING_GROUP_ROW)).toBe("Zerodha");
    expect(describeBrokers({ ...HOLDING_GROUP_ROW, brokers: [], broker_count: 0 })).toBe(
      "No broker connected",
    );
  });
});

describe("§6.1 — two timestamps, two sentences", () => {
  it("formats each date and never combines them", () => {
    expect(pricesLine(OVERVIEW)).toBe("Prices: close of 21 Aug 2026");
    expect(syncedLine(OVERVIEW)).toBe("Holdings synced: 25 Aug 2026");
  });

  it("falls back to the API's own sentence when a date is absent", () => {
    expect(pricesLine({ prices_as_of: null, prices_label: "No closing prices yet" })).toBe(
      "No closing prices yet",
    );
    expect(
      syncedLine({ holdings_synced_on: null, holdings_synced_label: "Holdings not synced yet" }),
    ).toBe("Holdings not synced yet");
  });
});

describe("§6.4 — every attention kind resolves somewhere", () => {
  it("maps each of the five v1 kinds to a route and a verb", () => {
    const kinds = [
      "BROKER_CONNECTION_EXPIRED",
      "RECONCILIATION_PENDING",
      "STALE_PRICE_DATA",
      "HOLDINGS_UNALLOCATED",
      "REBALANCE_AVAILABLE",
    ];
    for (const kind of kinds) {
      const link = attentionLink(kind);
      expect(link.href.startsWith("/")).toBe(true);
      expect(link.action).not.toBe("");
    }
  });

  it("still links somewhere for a kind this UI has not been taught", () => {
    expect(attentionLink("SOMETHING_NEW").href).toBe("/portfolio/holdings");
  });
});
