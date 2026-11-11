import { describe, expect, it } from "vitest";

import { DEFAULT_FILTERS, combinedRank, explore } from "@/lib/mb/explorer";
import type { MbExplorerName } from "@/lib/mb/fetch";

function name(symbol: string, d: number | null, s: number | null, extra: Partial<MbExplorerName> = {}): MbExplorerName {
  return {
    instrument_id: symbol.length * 100 + (d ?? 0),
    symbol,
    name: null,
    sector: "Textiles",
    close: "100",
    turnover20: "100000000",
    r252_fraction: "0.2",
    dist_ath_fraction: "-0.1",
    sessions_listed: 1000,
    ttm_rev_yoy_fraction: "0.1",
    ttm_pat_yoy_fraction: "0.1",
    hi63_trigger: false,
    doubling: d === null ? null : { rank: d, score: "0.1", scope: "universe", investable: true },
    swing: s === null ? null : { rank: s, score: "0.1", scope: "universe", investable: true, picked: false },
    ...extra,
  };
}

const ALL = [name("AAA", 1, 4), name("BBB", 2, 1), name("CCC", 3, 2), name("DDD", 4, null)];

describe("the model explorer (Maulik, 12 Nov 2026)", () => {
  it("ranks by either model, best first, with an unscored name last", () => {
    expect(explore(ALL, DEFAULT_FILTERS, "doubling", true).map((n) => n.symbol)).toEqual(["AAA", "BBB", "CCC", "DDD"]);
    expect(explore(ALL, DEFAULT_FILTERS, "swing", true).map((n) => n.symbol)).toEqual(["BBB", "CCC", "AAA", "DDD"]);
    expect(explore(ALL, DEFAULT_FILTERS, "swing", false).map((n) => n.symbol)).toEqual(["AAA", "CCC", "BBB", "DDD"]);
  });

  it("ranks by both models as the mean of their rank shares", () => {
    expect(combinedRank(ALL[1]!, 4, 3)).toBeCloseTo((2 / 4 + 1 / 3) / 2);
    expect(explore(ALL, DEFAULT_FILTERS, "combined", true)[0]!.symbol).toBe("BBB");
  });

  it("applies several filters together", () => {
    const rows = [
      name("CHEAP", 1, 1, { close: "30" }),
      name("THIN", 2, 2, { turnover20: "10000000" }),
      name("OUT", 3, 3, { swing: { rank: 3, score: "0.3", scope: "young", investable: true, picked: false } }),
      name("KEEP", 4, 4),
    ];
    const f = { ...DEFAULT_FILTERS, priceMin: 50, turnoverMinCr: 5, range: "both" as const };
    expect(explore(rows, f, "doubling", true).map((n) => n.symbol)).toEqual(["KEEP"]);
  });
});
