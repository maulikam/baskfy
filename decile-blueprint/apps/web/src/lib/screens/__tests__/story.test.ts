import { describe, expect, it } from "vitest";

import { defaultDefinition } from "@/lib/screens/defaults";
import { buildStorySentence, computeStoryStats, rankingPhrase, universePhrase } from "@/lib/screens/story";

describe("story strip helpers", () => {
  it("phrases nifty-total-market as the whole market", () => {
    expect(universePhrase("nifty-total-market", [])).toBe("the whole market");
  });

  it("phrases nifty-50 as Nifty 50", () => {
    expect(universePhrase("nifty-50", [])).toBe("Nifty 50");
  });

  it("uses a human ranking phrase for the default consistency score", () => {
    expect(rankingPhrase("avg_sharpe_12_6_3_1", [])).toMatch(/steadily/);
  });

  it("builds a plain-language sentence from config + count", () => {
    const definition = { ...defaultDefinition(), index: "nifty-total-market" as const };
    const sentence = buildStorySentence({
      definition,
      resultCount: 268,
      universes: [],
      factors: [],
    });
    expect(sentence).toBe(
      "268 stocks from the whole market, ranked by how steadily they've beaten their own risk over the last year.",
    );
  });

  it("names Nifty 50 in the sentence", () => {
    const definition = { ...defaultDefinition(), index: "nifty-50" as const };
    const sentence = buildStorySentence({
      definition,
      resultCount: 50,
      universes: [],
      factors: [],
    });
    expect(sentence).toBe(
      "50 stocks from Nifty 50, ranked by how steadily they've beaten their own risk over the last year.",
    );
  });

  it("computes tiles from preview rows without a new API", () => {
    const stats = computeStoryStats({
      as_of: "2026-08-18",
      result_count: 2,
      columns: ["symbol", "sorting_factor", "ret_12m", "vol_12m"],
      sorting_factor: { key: "avg_sharpe_12_6_3_1", label: "AVERAGE SHARPE" },
      rows: [
        { rank: 1, symbol: "CUPID", name: "Cupid", sorting_factor: 2.5, ret_12m: 80, vol_12m: 0.4 },
        { rank: 2, symbol: "OTHER", name: "Other", sorting_factor: 1.1, ret_12m: 120, vol_12m: 0.6 },
      ],
      data_version: 1,
    });

    expect(stats.topPickSymbol).toBe("CUPID");
    expect(stats.topPickScore).toBe("2.5");
    expect(stats.bestReturnText).toContain("120");
    expect(stats.medianVolText).not.toBeNull();
    expect(stats.asOfText).toContain("18 Aug");
  });
});
