import { describe, expect, it } from "vitest";

import { flagsOf } from "@/lib/mb/flags";
import { parseMbScores } from "@/lib/mb/scores";

const base = {
  rank: 7,
  score: "0.1",
  investable: true,
  hi63_trigger: false,
  hi63_trigger_date: null,
  close: "120.00",
  turnover20: "90000000.00",
  sessions_listed: 900,
  ttm_rev_yoy_fraction: "0.1",
  ttm_pat_yoy_fraction: null,
};

describe("the model column's flags", () => {
  it("marks a stock the model did not rank instead of hiding it", () => {
    expect(flagsOf(undefined)).toEqual(["not_scored"]);
  });

  it("names every reason a ranked stock is outside the paper record", () => {
    expect(flagsOf(base)).toEqual(["investable"]);
    expect(
      flagsOf({
        ...base,
        investable: false,
        close: "43.00",
        turnover20: "20000000.00",
        sessions_listed: 300,
        hi63_trigger: true,
        ttm_rev_yoy_fraction: null,
      }),
    ).toEqual(["low_price", "thin", "high63", "new", "no_results"]);
  });

  it("looks symbols up case-blind", () => {
    const s = parseMbScores({
      as_of: "2026-11-09",
      universe_n: 1215,
      names: [{ ...base, symbol: "salsteel" }],
    });
    expect(s.bySymbol.SALSTEEL?.rank).toBe(7);
    expect(s.loaded).toBe(true);
  });
});
