import { describe, expect, it } from "vitest";

import { describeProvenance, type SyncHoldingsOut } from "@/lib/portfolios/provenance";

/**
 * The money-safety fix, asserted in both directions.
 *
 * Before leaf C1 every non-empty holdings response was captioned "fixture holdings", a live Kite
 * fetch included, so a user looking at their own shares was told the numbers were fake. The fix is
 * only a fix if it works both ways: a **fixture must be marked**, and **live data must not be**.
 */

function result(partial: Partial<SyncHoldingsOut> = {}): SyncHoldingsOut {
  return {
    broker_id: "zerodha",
    // M76 added the persistence fields; a fixture that omits them no longer matches the response.
    persisted: false,
    written: 0,
    portfolio_id: null,
    unresolved: [],
    sync_note: "",
    degraded: false,
    dry_run: true,
    holdings: [
      {
        symbol: "CUPID",
        exchange: "NSE",
        quantity: "10",
        t1_quantity: "2",
        collateral_quantity: "3",
        total_quantity: "15",
        average_price: "284.56",
        product: "CNC",
      },
    ],
    note: "",
    source: "live",
    ...partial,
  };
}

describe("live holdings provenance is not marked as a fixture", () => {
  it("does not mark a clean live read", () => {
    const view = describeProvenance(result({ source: "live" }), "Zerodha");
    expect(view.marked).toBe(false);
    expect(view.mark).toBeNull();
    expect(view.tone).toBe("live");
    expect(view.label).toBe("Live from Zerodha");
    expect(view.detail).toMatch(/your own holdings/);
    expect(view.detail).not.toMatch(/sample/i);
  });

  it("marks a live read the server called degraded, without calling it fake", () => {
    const view = describeProvenance(result({ source: "live", degraded: true }), "Zerodha");
    expect(view.marked).toBe(true);
    expect(view.mark).toBe("Incomplete");
    expect(view.detail).toMatch(/incomplete/);
    expect(view.detail).not.toMatch(/not your holdings/i);
  });
});

describe("fixture holdings are visibly marked as a fixture", () => {
  it("marks a deliberate fixture and says the rows are not the reader's", () => {
    const view = describeProvenance(result({ source: "fixture" }), "Zerodha");
    expect(view.marked).toBe(true);
    expect(view.mark).toBe("Not your holdings");
    expect(view.label).toBe("Sample data");
    expect(view.detail).toMatch(/not your holdings/);
  });

  it("says a degraded fixture stood in for a live read that failed", () => {
    const view = describeProvenance(result({ source: "fixture", degraded: true }), "Zerodha");
    expect(view.marked).toBe(true);
    expect(view.detail).toMatch(/expected to work and did not/);
  });
});

describe("empty and unwired provenance say which kind of nothing it is", () => {
  it("does not mark an honest empty answer", () => {
    const view = describeProvenance(result({ source: "empty", holdings: [] }), "Zerodha");
    expect(view.marked).toBe(false);
    expect(view.detail).toBe("Zerodha reported no holdings.");
  });

  it("marks an empty answer that came from a degraded read", () => {
    const view = describeProvenance(
      result({ source: "empty", holdings: [], degraded: true }),
      "Zerodha",
    );
    expect(view.marked).toBe(true);
    expect(view.detail).toMatch(/does not mean you hold nothing/);
  });

  it("says an unwired broker can report nothing at all", () => {
    const view = describeProvenance(
      result({ broker_id: "kotak", source: "unwired", holdings: [] }),
      "Kotak Neo",
    );
    expect(view.marked).toBe(true);
    expect(view.label).toBe("No adapter yet");
    expect(view.detail).toMatch(/no holdings adapter for Kotak Neo/);
    expect(view.detail).toMatch(/missing from every figure/);
  });
});

describe("provenance carries the facts the caller needs to render it", () => {
  it("titles a broker id when no display name is supplied", () => {
    expect(describeProvenance(result({ broker_id: "angelone" })).brokerName).toBe("Angelone");
    expect(describeProvenance(result({ broker_id: "five-paisa" })).brokerName).toBe("Five Paisa");
  });

  it("counts the rows and carries the dry-run flag through", () => {
    const view = describeProvenance(result());
    expect(view.rowCount).toBe(1);
    expect(view.dryRun).toBe(true);
    expect(view.source).toBe("live");
  });

  it("never collapses the four sources into a boolean", () => {
    const sources: SyncHoldingsOut["source"][] = ["live", "fixture", "empty", "unwired"];
    const labels = sources.map(
      (source) => describeProvenance(result({ source, holdings: [] })).label,
    );
    expect(new Set(labels).size).toBe(4);
  });
});
