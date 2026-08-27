/**
 * The translation behind §6.7's confirm button.
 *
 * The flow speaks the screen's vocabulary (a start the user picked, a benchmark they named); the
 * API speaks the ledger's (§3's source, an `index_def` id). These assert the mapping between
 * them, because a wrong mapping here is silent: the portfolio is created, it just describes
 * itself as something the user did not choose.
 */

import { describe, expect, it } from "vitest";

import {
  benchmarkIndexId,
  bodyForDraft,
  sourceForStart,
  type BenchmarkOption,
} from "@/lib/portfolio/draft-mapping";
import type { PortfolioDraft } from "@/lib/portfolio/organize";

const UNIVERSES: readonly BenchmarkOption[] = [
  { index_id: 1, slug: "nifty-50", name: "NIFTY 50" },
  { index_id: 5, slug: "nifty-500", name: "NIFTY 500" },
  { index_id: 9, slug: "nifty-midcap-150", name: "NIFTY MIDCAP 150" },
];

function draft(over: Partial<PortfolioDraft> = {}): PortfolioDraft {
  return {
    start: "HOLDINGS",
    kind: "CAPITAL",
    name: "Long term",
    benchmark: "Nifty 500",
    keys: [{ instrument_id: 11, broker_account_id: 3 }],
    sourceId: null,
    ...over,
  };
}

describe("start maps to §3's source", () => {
  it.each([
    ["SUBSCRIBED", "SUBSCRIBED"],
    ["MY_SCREEN", "MY_SCREEN"],
    ["MY_STRATEGY", "MY_STRATEGY"],
    ["HOLDINGS", "HOLDING_GROUP"],
    ["EMPTY", "HOLDING_GROUP"],
  ] as const)("%s becomes %s", (start, expected) => {
    expect(sourceForStart(start)).toBe(expected);
  });

  it("collapses the two starts that carry no rule onto one source", () => {
    // §3's source describes where a portfolio's rule comes from. "From broker holdings" and
    // "Empty" have none — the user assembled them — so both are a holding group, and §5.2 then
    // gives them the most conservative metric.
    expect(sourceForStart("HOLDINGS")).toBe(sourceForStart("EMPTY"));
  });
});

describe("benchmark name resolves to an index id", () => {
  it("matches the name the flow shows against the name the seed stores", () => {
    // The flow shows "Nifty 500"; `index_def` holds "NIFTY 500". Requiring an exact match would
    // leave the benchmark silently unset for every user.
    expect(benchmarkIndexId("Nifty 500", UNIVERSES)).toBe(5);
  });

  it("matches a slug too", () => {
    expect(benchmarkIndexId("nifty-midcap-150", UNIVERSES)).toBe(9);
  });

  it("ignores spacing and punctuation differences", () => {
    expect(benchmarkIndexId("  nifty 50 ", UNIVERSES)).toBe(1);
  });

  it("answers null for a benchmark it cannot resolve, rather than guessing an id", () => {
    // §6.3: no benchmark of its own means the surface falls back to the product default.
    // Guessing would point the chart at whichever index happened to be first.
    expect(benchmarkIndexId("Sensex", UNIVERSES)).toBeNull();
  });

  it("answers null when the universes list could not be fetched", () => {
    expect(benchmarkIndexId("Nifty 500", [])).toBeNull();
  });

  it("answers null for a blank benchmark", () => {
    expect(benchmarkIndexId("   ", UNIVERSES)).toBeNull();
  });
});

describe("the body the API receives", () => {
  it("carries the four facts that make a portfolio, all stated", () => {
    const body = bodyForDraft(draft(), UNIVERSES);
    expect(body).toEqual({
      name: "Long term",
      kind: "CAPITAL",
      source: "HOLDING_GROUP",
      benchmark_index_id: 5,
      holdings: [{ instrument_id: 11, broker_account_id: 3 }],
    });
  });

  it("has no quantity anywhere — §4.2 is structural, not validated away", () => {
    const body = bodyForDraft(draft(), UNIVERSES);
    const serialised = JSON.stringify(body);
    expect(serialised).not.toContain("quantity");
    for (const holding of body.holdings ?? []) {
      expect(Object.keys(holding).sort()).toEqual(["broker_account_id", "instrument_id"]);
    }
  });

  it("trims the name, because a portfolio called ' ' is a naming accident", () => {
    expect(bodyForDraft(draft({ name: "  Momentum  " }), UNIVERSES).name).toBe("Momentum");
  });

  it("sends an empty holdings list for §6.7's Empty start rather than omitting the field", () => {
    const body = bodyForDraft(draft({ start: "EMPTY", keys: [] }), UNIVERSES);
    expect(body.holdings).toEqual([]);
    expect(body.source).toBe("HOLDING_GROUP");
  });

  it("keeps a monitoring view a monitoring view", () => {
    // §4.1: the kind decides arithmetic. If this leaked as CAPITAL the lens would enter every
    // total and criterion 2 would refuse its overlapping holdings.
    expect(bodyForDraft(draft({ kind: "MONITORING" }), UNIVERSES).kind).toBe("MONITORING");
  });

  it("sends a null benchmark rather than dropping the key", () => {
    // The column is nullable and `None` has a meaning (§6.3). Omitting it and sending it as null
    // are different requests.
    const body = bodyForDraft(draft({ benchmark: "Nothing like this" }), UNIVERSES);
    expect(body.benchmark_index_id).toBeNull();
    expect("benchmark_index_id" in body).toBe(true);
  });
});
