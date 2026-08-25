import type { AllocationOut, AllocationRowOut, SleeveAllocationOut } from "@baskfy/api-client";
import { describe, expect, it } from "vitest";

import { NO_FIGURE } from "@/lib/portfolios/decimal";
import { allocationUnits, priceCell, sleeveUnpriced, unitsCell } from "@/lib/portfolios/units";

/**
 * The rule the units feature exists to enforce: an unpriced row is blank **with a reason**, and is
 * never a zero. `units: null` and `units: 0` are different facts and must render differently.
 */

function row(partial: Partial<AllocationRowOut> & { symbol: string }): AllocationRowOut {
  return { amount: "100000", weight_pct: "10.00", ...partial };
}

function sleeve(partial: Partial<SleeveAllocationOut> = {}): SleeveAllocationOut {
  return {
    capital: "500000",
    cash: "0",
    deployed: "500000",
    kind: "screen",
    name: "Momentum",
    rows: [],
    unpriced: [],
    ...partial,
  };
}

describe("a unit count renders only when a price existed", () => {
  it("renders the unit count beside the amount when the row was priced", () => {
    const cell = unitsCell(row({ symbol: "CUPID", units: 1757, price: "284.56" }));
    expect(cell).toEqual({ text: "1,757", unpriced: false, reason: null });
  });

  it("renders a dash and a reason when there was no price, never a zero", () => {
    const cell = unitsCell(row({ symbol: "HFCL", units: null, price: null }));
    expect(cell.text).toBe(NO_FIGURE);
    expect(cell.text).not.toBe("0");
    expect(cell.unpriced).toBe(true);
    expect(cell.reason).toBe("No price for HFCL, so this row has no unit count.");
  });

  it("prefers the server's own units_note as the reason", () => {
    const cell = unitsCell(
      row({ symbol: "HFCL", units: null, price: null }),
      sleeve({ unpriced: ["HFCL"], units_note: "No close for HFCL on 2026-08-18." }),
    );
    expect(cell.reason).toBe("No close for HFCL on 2026-08-18.");
  });

  it("treats a genuine zero as an answer with its own reason, not as a missing price", () => {
    const cell = unitsCell(row({ symbol: "MRF", amount: "1000", units: 0, price: "125000" }));
    expect(cell.text).toBe("0");
    expect(cell.unpriced).toBe(false);
    expect(cell.reason).toBe("₹1,000.00 does not cover one share at ₹1,25,000.00.");
  });

  it("treats a price that is present but empty as no price at all", () => {
    expect(unitsCell(row({ symbol: "X", units: 5, price: "" })).unpriced).toBe(true);
  });
});

describe("the price cell blanks rather than inventing a figure", () => {
  it("formats a known price", () => {
    expect(priceCell(row({ symbol: "CUPID", price: "284.56" }))).toEqual({
      text: "₹284.56",
      unpriced: false,
    });
  });

  it("shows a dash for an unknown price", () => {
    expect(priceCell(row({ symbol: "CUPID", price: null }))).toEqual({
      text: NO_FIGURE,
      unpriced: true,
    });
  });
});

describe("a sleeve says how many of its names could not be priced", () => {
  it("says nothing when every row was priced", () => {
    expect(sleeveUnpriced(sleeve())).toEqual({ symbols: [], count: 0, note: null });
  });

  it("names the unpriced symbols and uses the server's sentence when there is one", () => {
    const summary = sleeveUnpriced(sleeve({ unpriced: ["HFCL", "IDEA"], units_note: "Two names had no close." }));
    expect(summary.count).toBe(2);
    expect(summary.symbols).toEqual(["HFCL", "IDEA"]);
    expect(summary.note).toBe("Two names had no close.");
  });

  it("writes its own sentence when the server sent none", () => {
    const summary = sleeveUnpriced(sleeve({ unpriced: ["HFCL"] }));
    expect(summary.note).toBe(
      "No price for HFCL, so that row has an amount but no unit count.",
    );
  });
});

describe("the whole allocation reports its pricing date and its gaps", () => {
  function allocation(partial: Partial<AllocationOut> = {}): AllocationOut {
    return {
      applied_regime_cap: false,
      capital: "1000000",
      cash: "0",
      deployed: "1000000",
      sleeves: [],
      unpriced: [],
      ...partial,
    };
  }

  it("names the date the unit counts came from", () => {
    const summary = allocationUnits(allocation({ priced_as_of: "2026-08-18" }));
    expect(summary.pricedAsOf).toBe("2026-08-18");
    expect(summary.note).toBeNull();
  });

  it("says how many names have an amount but no unit count", () => {
    const summary = allocationUnits(allocation({ priced_as_of: "2026-08-18", unpriced: ["HFCL", "IDEA"] }));
    expect(summary.note).toMatch(/2 names could not be priced/);
    expect(summary.note).toMatch(/HFCL, IDEA/);
    expect(summary.note).toMatch(/blank rather than zero/);
  });

  it("reports no pricing date at all rather than guessing one", () => {
    expect(allocationUnits(allocation()).pricedAsOf).toBeNull();
  });
});
