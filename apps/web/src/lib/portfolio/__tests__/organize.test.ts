import { describe, expect, it } from "vitest";

import {
  brokerBreakdown,
  describeHolding,
  filterHoldings,
  formatCoverage,
  holdingKeyId,
  parseHoldingKeyId,
  rankSuggestions,
  rowKeyIds,
  selectionTotal,
  sectorsIn,
  capacityFor,
  type AggregatedHolding,
  type GroupingSuggestion,
} from "@/lib/portfolio/organize";

/**
 * The arithmetic and the vocabulary behind §6.6/§6.7, asserted against the spec rather than
 * against what the code happens to do (CLAUDE.md house rule 2).
 */

function leg(brokerAccountId: number, label: string, quantity: string, value: string | null) {
  return {
    broker: { broker_account_id: brokerAccountId, broker_id: label.toLowerCase(), label },
    quantity,
    // 0035: nothing is filed away in these fixtures, so every share is free. A leg's free
    // quantity is what the picker may offer, and defaulting it to the whole position keeps these
    // tests describing the same pre-split world they were written for.
    unallocated_quantity: quantity,
    value,
    price: null,
    avg_price: null,
    cost_basis: null,
    allocation: null,
    monitoring_views: [],
    first_bought_on: null,
    history_source: "NONE",
    pending_reconciliation: false,
  };
}

function holding(
  instrumentId: number,
  symbol: string,
  name: string,
  legs: ReturnType<typeof leg>[],
  value: string | null,
): AggregatedHolding {
  return {
    instrument: { instrument_id: instrumentId, symbol, name },
    quantity: legs
      .reduce((sum, line) => sum + Number(line.quantity), 0)
      .toString(),
    price: null,
    price_as_of: null,
    value,
    allocation: null,
    allocated: false,
    split_across_portfolios: false,
    monitoring_views: [],
    brokers: legs,
    pending_reconciliation: false,
  };
}

const HDFC = holding(
  1,
  "HDFCBANK",
  "HDFC Bank",
  [leg(7, "Zerodha", "200", "300000.00"), leg(8, "Upstox", "120", "180000.00")],
  "480000.00",
);
const ITC = holding(2, "ITC", "ITC", [leg(7, "Zerodha", "500", "225000.50")], "225000.50");
const UNPRICED = holding(3, "NEWCO", "Newco", [leg(7, "Zerodha", "10", null)], null);

describe("capacityFor — the ceiling depends on where the shares are going", () => {
  /* This shipped wrong for one deploy and it is worth pinning hard. With every share filed into
     "Swing Manual", the add-to-existing flow showed "of 0 free" on every row and turned red on
     any number typed — refusing in the browser an operation the server performs. A control that
     cannot express what the route does is a control that blocks the feature. */

  function line(quantity: string, free: string, allocations: Array<[number, string]> = []) {
    return {
      broker: { broker_account_id: 1, broker_id: "zerodha", label: "Zerodha" },
      quantity,
      unallocated_quantity: free,
      allocations: allocations.map(([portfolio_id, qty]) => ({
        portfolio: {
          portfolio_id,
          name: `P${portfolio_id}`,
          kind: "CAPITAL" as const,
          source: "HOLDING_GROUP" as const,
        },
        quantity: qty,
      })),
      value: null,
      price: null,
      avg_price: null,
      cost_basis: null,
      allocation: null,
      monitoring_views: [],
      first_bought_on: null,
      history_source: "NONE",
      pending_reconciliation: false,
    };
  }

  it("creating a portfolio may only take what is unallocated", () => {
    expect(capacityFor(line("100", "40", [[7, "60"]]), null)).toBe("40");
  });

  it("adding to an existing portfolio may move shares out of another", () => {
    // 100 held, 60 in portfolio 7, none free — and 100 is still available to portfolio 9.
    expect(capacityFor(line("100", "0", [[7, "100"]]), 9)).toBe("100");
  });

  it("never offers a portfolio the shares it already holds", () => {
    // Asking portfolio 7 to take its own 60 would be asking it to take them from itself.
    expect(capacityFor(line("100", "40", [[7, "60"]]), 7)).toBe("40");
  });

  it("offers the whole position when the target holds none of it", () => {
    expect(capacityFor(line("100", "0", [[7, "100"]]), 8)).toBe("100");
  });
});

describe("holding keys", () => {
  it("keys a holding by instrument and broker account, and round-trips", () => {
    const id = holdingKeyId({ instrument_id: 1, broker_account_id: 7 });
    expect(id).toBe("1:7");
    expect(parseHoldingKeyId(id)).toEqual({ instrument_id: 1, broker_account_id: 7 });
  });

  it("refuses to invent a key from something that is not one", () => {
    expect(parseHoldingKeyId("HDFCBANK")).toBeNull();
  });

  it("gives a two-broker holding two keys, because two positions allocate separately", () => {
    expect(rowKeyIds(HDFC)).toEqual(["1:7", "1:8"]);
  });
});

describe("aggregated display (§6.7)", () => {
  it("prints the spec's own example", () => {
    expect(describeHolding(HDFC)).toBe("HDFC Bank — 320 (Zerodha 200 · Upstox 120)");
  });

  it("drops the parenthetical for a single broker rather than repeating the number", () => {
    expect(brokerBreakdown(ITC)).toBeNull();
    expect(describeHolding(ITC)).toBe("ITC — 500");
  });
});

describe("selectionTotal — the §6.7 live total", () => {
  it("is no figure, not zero, when nothing is picked", () => {
    const total = selectionTotal([HDFC, ITC], new Set());
    expect(total.value).toBeNull();
    expect(total.holdings).toBe(0);
  });

  it("sums the picked legs exactly", () => {
    const total = selectionTotal([HDFC, ITC], new Set(["1:7", "1:8", "2:7"]));
    expect(total.value).toBe("705000.50");
    expect(total.holdings).toBe(3);
    expect(total.instruments).toBe(2);
  });

  it("counts one leg of a two-broker holding as one holding, not the whole stock", () => {
    const total = selectionTotal([HDFC], new Set(["1:7"]));
    expect(total.value).toBe("300000.00");
    expect(total.holdings).toBe(1);
    expect(total.instruments).toBe(1);
  });

  it("excludes an unpriced leg from the total and reports it, rather than adding a zero", () => {
    const total = selectionTotal([ITC, UNPRICED], new Set(["2:7", "3:7"]));
    expect(total.value).toBe("225000.50");
    expect(total.unpriced).toBe(1);
    expect(total.holdings).toBe(2);
  });
});

describe("rankSuggestions — the port of grouping_suggestions._rank_key", () => {
  function suggestion(partial: Partial<GroupingSuggestion>): GroupingSuggestion {
    return {
      basis: "SECTOR",
      proposed_name: "Financials",
      keys: [{ instrument_id: 1, broker_account_id: 7 }],
      value: "1000",
      rationale: "",
      suggested_kind: "CAPITAL",
      basket_id: null,
      basket_coverage: null,
      missing_instrument_ids: [],
      ...partial,
    };
  }

  it("orders by value descending first — the biggest decision available", () => {
    const ranked = rankSuggestions([
      suggestion({ proposed_name: "small", value: "10" }),
      suggestion({ proposed_name: "big", value: "1000000" }),
    ]);
    expect(ranked.map((row) => row.proposed_name)).toEqual(["big", "small"]);
  });

  it("breaks a value tie on holding count, then on basis confidence", () => {
    const ranked = rankSuggestions([
      suggestion({ proposed_name: "era", basis: "PURCHASE_ERA", value: "100" }),
      suggestion({
        proposed_name: "model",
        basis: "BASKET_OVERLAP",
        value: "100",
        basket_id: 4,
        basket_coverage: "0.5000",
      }),
      suggestion({ proposed_name: "sector", basis: "SECTOR", value: "100" }),
    ]);
    expect(ranked.map((row) => row.proposed_name)).toEqual(["model", "sector", "era"]);
  });

  it("does not mutate the list it was handed", () => {
    const input = [suggestion({ proposed_name: "a", value: "1" }), suggestion({ proposed_name: "b", value: "2" })];
    rankSuggestions(input);
    expect(input.map((row) => row.proposed_name)).toEqual(["a", "b"]);
  });
});

describe("coverage", () => {
  it("renders 11-of-15 as a whole percentage", () => {
    expect(formatCoverage("0.7333")).toBe("73%");
  });

  it("renders a missing coverage as no figure, never as NaN", () => {
    expect(formatCoverage(null)).toBe("—");
  });
});

describe("filters (§6.7 left panel)", () => {
  const rows = [HDFC, ITC];
  const sectors = { "1": "Financials", "2": "FMCG" };

  it("filters by broker account", () => {
    expect(filterHoldings(rows, { broker: "8", sector: "ALL", query: "" }).map((r) => r.instrument.symbol)).toEqual([
      "HDFCBANK",
    ]);
  });

  it("filters by sector when sectors are known", () => {
    expect(
      filterHoldings(rows, { broker: "ALL", sector: "FMCG", query: "" }, sectors).map(
        (r) => r.instrument.symbol,
      ),
    ).toEqual(["ITC"]);
  });

  it("filters by symbol or name, case-insensitively", () => {
    expect(
      filterHoldings(rows, { broker: "ALL", sector: "ALL", query: "hdfc bank" }).map(
        (r) => r.instrument.symbol,
      ),
    ).toEqual(["HDFCBANK"]);
  });

  it("knows no sectors when none were supplied, rather than inventing one", () => {
    expect(sectorsIn(rows, {})).toEqual([]);
    expect(sectorsIn(rows, sectors)).toEqual(["FMCG", "Financials"]);
  });
});
