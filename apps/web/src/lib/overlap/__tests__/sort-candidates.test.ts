import { describe, expect, it } from "vitest";

import {
  CANDIDATE_SORT_DEFAULT_DIRECTION,
  CANDIDATE_SORT_KEYS,
  CANDIDATE_SORT_MEANING,
  sortCandidates,
  type OverlapCandidate,
} from "@/lib/overlap/candidates";

/* Four rows shaped as `parseCandidates` returns them, in the server's order (count, actionable,
   symbol), each differing from the others under every key so the tests can name the order. */
function row(overrides: Partial<OverlapCandidate> & Pick<OverlapCandidate, "symbol">): OverlapCandidate {
  return {
    instrument_id: overrides.symbol.length,
    name: `${overrides.symbol} LIMITED`,
    close: null,
    last_price: null,
    strategy_count: 1,
    actionable: true,
    strategies: [],
    screens: [],
    catalyst: null,
    opinion: null,
    ...overrides,
  };
}

const swing = { strategy: "swing", name: "Swing", ref: "/swing", as_of: "2026-09-25", detail: "EP", actionable: true } as const;
const vbt = {
  strategy: "volume_breakout",
  name: "Volume breakout",
  ref: "/vbt",
  as_of: "2026-09-25",
  detail: "signal",
  actionable: true,
} as const;
const screen = (name: string, rank: number | null) => ({
  name,
  public_id: name.toLowerCase().padEnd(12, "0"),
  is_template: false,
  as_of: "2026-09-25",
  rank,
  of: 400,
  definition_changed: false,
});
const tag = (review_priority: "high" | "medium" | "low") => ({
  event_type: "order" as const,
  review_priority,
  matched: [],
  source: "rules" as const,
  confidence: null,
  disagrees_with: null,
  corrected: false,
});
const opinion = (label: "look_first" | "worth_a_look" | "skip", source: "laya" | "rules", confidence: number) => ({
  label,
  confidence,
  source,
  shown: true,
  floor: 0.6,
  labelled: false,
  reason: null,
  laya: null,
});

const BOTH = row({
  symbol: "BOTH",
  strategy_count: 2,
  strategies: [swing, vbt],
  close: 144.75,
  catalyst: {
    headline: "Order win",
    published_at: "2026-09-24T10:00:00+00:00",
    url: "https://x/a.pdf",
    earnings_date: "2026-10-20",
    tag: tag("high"),
  },
  opinion: opinion("worth_a_look", "rules", 1),
  screens: [screen("Alpha", 30), screen("Beta", 5)],
});
const CHEAP = row({
  symbol: "CHEAP",
  strategies: [vbt],
  close: 12.5,
  catalyst: {
    headline: "Investor meet",
    published_at: "2026-09-25T10:00:00+00:00",
    url: "https://x/b.pdf",
    earnings_date: "2026-10-02",
    tag: tag("low"),
  },
  opinion: opinion("look_first", "laya", 0.71),
  screens: [screen("Alpha", 2)],
});
const REJECT = row({
  symbol: "REJECT",
  actionable: false,
  strategies: [{ ...swing, actionable: false }],
  close: 99,
  catalyst: { headline: "Penalty", published_at: null, url: "https://x/c.pdf", earnings_date: null, tag: tag("medium") },
  opinion: opinion("skip", "rules", 1),
  screens: [screen("Alpha", 1), screen("Beta", 1)],
});
const ALONE = row({ symbol: "ALONE", strategies: [swing] });
const ROWS = [BOTH, CHEAP, REJECT, ALONE];
const order = (rows: OverlapCandidate[]) => rows.map((r) => r.symbol);

describe("sortCandidates", () => {
  it("keeps the server's order under no key, as a new array", () => {
    const out = sortCandidates(ROWS, null, 1);
    expect(order(out)).toEqual(["BOTH", "CHEAP", "REJECT", "ALONE"]);
    expect(out).not.toBe(ROWS);
  });

  it("names every header once, with a first direction and a meaning", () => {
    expect(CANDIDATE_SORT_KEYS).toEqual(["name", "on", "strategies", "price", "results", "filing", "laya", "screens"]);
    for (const key of CANDIDATE_SORT_KEYS) {
      expect([1, -1]).toContain(CANDIDATE_SORT_DEFAULT_DIRECTION[key]);
      expect(CANDIDATE_SORT_MEANING[key]).toMatch(/^Sort by /);
    }
  });

  it("sorts by symbol, both ways", () => {
    expect(order(sortCandidates(ROWS, "name", 1))).toEqual(["ALONE", "BOTH", "CHEAP", "REJECT"]);
    expect(order(sortCandidates(ROWS, "name", -1))).toEqual(["REJECT", "CHEAP", "BOTH", "ALONE"]);
  });

  it("sorts by the count, keeping the server's order among equals", () => {
    expect(order(sortCandidates(ROWS, "on", -1))).toEqual(["BOTH", "CHEAP", "ALONE", "REJECT"]);
    expect(order(sortCandidates(ROWS, "on", 1))).toEqual(["REJECT", "CHEAP", "ALONE", "BOTH"]);
  });

  it("sorts by the strategies' names, actionable rows first", () => {
    expect(order(sortCandidates(ROWS, "strategies", 1))).toEqual(["ALONE", "BOTH", "CHEAP", "REJECT"]);
  });

  it("sorts by the close and puts a row with no print last either way", () => {
    expect(order(sortCandidates(ROWS, "price", -1))).toEqual(["BOTH", "REJECT", "CHEAP", "ALONE"]);
    expect(order(sortCandidates(ROWS, "price", 1))).toEqual(["CHEAP", "REJECT", "BOTH", "ALONE"]);
  });

  it("sorts by the result date, soonest first, no date last either way", () => {
    expect(order(sortCandidates(ROWS, "results", 1))).toEqual(["CHEAP", "BOTH", "REJECT", "ALONE"]);
    expect(order(sortCandidates(ROWS, "results", -1))).toEqual(["BOTH", "CHEAP", "REJECT", "ALONE"]);
  });

  it("sorts the filing by priority then date, no filing last", () => {
    expect(order(sortCandidates(ROWS, "filing", -1))).toEqual(["BOTH", "REJECT", "CHEAP", "ALONE"]);
    expect(order(sortCandidates(ROWS, "filing", 1))).toEqual(["CHEAP", "REJECT", "BOTH", "ALONE"]);
  });

  it("sorts the Laya column by the word then the model's percentage, no opinion last", () => {
    expect(order(sortCandidates(ROWS, "laya", -1))).toEqual(["CHEAP", "BOTH", "REJECT", "ALONE"]);
    expect(order(sortCandidates(ROWS, "laya", 1))).toEqual(["REJECT", "BOTH", "CHEAP", "ALONE"]);
  });

  it("sorts screens by how many, then the best rank, none last", () => {
    expect(order(sortCandidates(ROWS, "screens", -1))).toEqual(["REJECT", "BOTH", "CHEAP", "ALONE"]);
    expect(order(sortCandidates(ROWS, "screens", 1))).toEqual(["CHEAP", "BOTH", "REJECT", "ALONE"]);
  });

  it("does not touch the rows it is given", () => {
    const before = ROWS.map((r) => r.symbol);
    sortCandidates(ROWS, "price", 1);
    expect(order(ROWS)).toEqual(before);
  });
});
